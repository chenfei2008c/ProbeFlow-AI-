"""FFmpeg-backed audio validation, segmentation, and chunk assembly."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import tempfile
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any


_MAX_SEGMENT_SECONDS = 180.0
_MAX_AUDIO_SECONDS = 600.0
_MAX_ASR_BASE64_BYTES = 10_000_000
_SILENCE_PATTERN = re.compile(r"silence_(start|end):\s*(-?\d+(?:\.\d+)?)")
_BLOCKED_PROTOCOLS = "concat,crypto,data,ftp,http,https,tcp,tls,udp"


@dataclass(frozen=True, slots=True)
class AudioInfo:
    mime_type: str
    duration_seconds: float
    byte_size: int
    sha256: str


@dataclass(frozen=True, slots=True)
class AudioSegment:
    path: Path
    start_seconds: float
    end_seconds: float


class AudioError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def probe_audio(path: Path) -> AudioInfo:
    path = Path(path)
    try:
        stat = path.stat()
    except OSError as exc:
        raise AudioError("audio_unreadable", "无法读取音频文件。") from exc
    if not path.is_file() or stat.st_size <= 0:
        raise AudioError("invalid_audio", "文件不是有效的非空音频。")
    demuxer = _sniff_demuxer(path)
    command = [
        "ffprobe",
        "-v",
        "error",
        *_restricted_input_options(demuxer),
        "-show_entries",
        "format=format_name,duration:stream=codec_type,codec_name,duration",
        "-of",
        "json",
        str(path),
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=15)
    except FileNotFoundError as exc:
        raise AudioError("ffmpeg_unavailable", "未安装 FFprobe，无法验证音频。") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("audio_probe_timeout", "音频探测超时。") from exc
    if completed.returncode != 0:
        raise AudioError("invalid_audio", "文件内容不是可解码的音频。")
    try:
        metadata = json.loads(completed.stdout)
        streams = [stream for stream in metadata.get("streams", []) if stream.get("codec_type") == "audio"]
        if not streams:
            raise ValueError
        duration = _duration(metadata, streams)
        format_name = str(metadata["format"]["format_name"])
        codec_name = str(streams[0].get("codec_name", ""))
        mime_type = _mime_type(format_name, codec_name)
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise AudioError("invalid_audio", "音频缺少可验证的格式或时长。") from exc
    if duration is None:
        duration = _decoded_duration(path, demuxer)
    if not math.isfinite(duration) or duration <= 0:
        raise AudioError("invalid_audio", "音频时长无效。")
    if duration > _MAX_AUDIO_SECONDS + 0.01:
        raise AudioError("audio_too_long", "单轮录音超过 10 分钟安全上限。")
    digest = hashlib.sha256()
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise AudioError("audio_unreadable", "读取音频时失败。") from exc
    return AudioInfo(
        mime_type=mime_type,
        duration_seconds=duration,
        byte_size=stat.st_size,
        sha256=digest.hexdigest(),
    )


def prepare_asr_segments(source: Path, output_dir: Path, max_seconds: float = 180) -> list[AudioSegment]:
    if not 0 < max_seconds <= _MAX_SEGMENT_SECONDS:
        raise AudioError("invalid_segment_limit", "识别分段时长必须大于 0 且不超过 180 秒。")
    source = Path(source)
    output_dir = Path(output_dir)
    info = probe_audio(source)
    _validate_signal(source, min(info.duration_seconds, 30.0))
    silence_points = _silence_midpoints(source)
    boundaries = _segment_boundaries(info.duration_seconds, float(max_seconds), silence_points)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AudioError("segment_write_failed", "无法创建识别分段目录。") from exc
    segments: list[AudioSegment] = []
    for index, (start, end) in enumerate(zip(boundaries, boundaries[1:])):
        final_path = output_dir / f"segment-{index:04d}.wav"
        temporary = output_dir / f".segment-{index:04d}.{os.getpid()}.tmp.wav"
        command = [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{start:.6f}",
            *_restricted_input_options(_sniff_demuxer(source)),
            "-i",
            str(source),
            "-t",
            f"{end - start:.6f}",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(temporary),
        ]
        try:
            completed = subprocess.run(command, check=False, capture_output=True, timeout=60)
        except FileNotFoundError as exc:
            raise AudioError("ffmpeg_unavailable", "未安装 FFmpeg，无法转换音频。") from exc
        except subprocess.TimeoutExpired as exc:
            raise AudioError("audio_conversion_timeout", "音频转换超时。") from exc
        if completed.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise AudioError("audio_conversion_failed", "无法生成可独立解码的识别音频分段。")
        try:
            converted = probe_audio(temporary)
            encoded_size = 4 * ((converted.byte_size + 2) // 3)
            if converted.duration_seconds > max_seconds + 0.05:
                raise AudioError("segment_too_long", "识别音频分段超过时长限制。")
            if encoded_size > _MAX_ASR_BASE64_BYTES:
                raise AudioError("segment_too_large", "识别音频分段超过 Base64 请求体安全限制。")
            os.replace(temporary, final_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        segments.append(AudioSegment(path=final_path, start_seconds=start, end_seconds=end))
    if not segments:
        raise AudioError("audio_conversion_failed", "没有生成识别音频分段。")
    return segments


def assemble_chunks(paths: list[Path], target: Path) -> Path:
    if not paths:
        raise AudioError("missing_chunk", "没有可组装的录音分块。")
    normalized = [Path(path) for path in paths]
    for path in normalized:
        if not path.is_file():
            raise AudioError("missing_chunk", "录音分块缺失，无法组装。")
    target = Path(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
    except OSError as exc:
        raise AudioError("chunk_assembly_failed", "无法创建录音组装临时文件。") from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as output:
            for path in normalized:
                with path.open("rb") as chunk:
                    for block in iter(lambda: chunk.read(1024 * 1024), b""):
                        output.write(block)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise AudioError("chunk_assembly_failed", "组装录音分块失败。") from exc
    return target


def _duration(metadata: dict[str, Any], streams: list[dict[str, Any]]) -> float | None:
    raw = metadata.get("format", {}).get("duration")
    if raw in {None, "N/A"}:
        raw = streams[0].get("duration")
    if raw in {None, "N/A"}:
        return None
    value = float(raw)
    return value if math.isfinite(value) and value > 0 else None


def _mime_type(format_name: str, codec_name: str) -> str:
    formats = set(format_name.split(","))
    if "wav" in formats:
        return "audio/wav"
    if formats.intersection({"matroska", "webm"}):
        return "audio/webm"
    if formats.intersection({"ogg", "oga"}):
        return "audio/ogg"
    if formats.intersection({"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}):
        return "audio/mp4"
    if formats.intersection({"mp3", "mpeg"}) or codec_name == "mp3":
        return "audio/mpeg"
    if "flac" in formats or codec_name == "flac":
        return "audio/flac"
    if "aac" in formats or codec_name == "aac":
        return "audio/aac"
    if formats.intersection({"amr", "aiff", "avi"}):
        return f"audio/{next(iter(formats))}"
    raise ValueError("unsupported audio container")


def _validate_signal(source: Path, seconds: float) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *_restricted_input_options(_sniff_demuxer(source)),
        "-i",
        str(source),
        "-t",
        f"{seconds:.3f}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "s16le",
        "pipe:1",
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, timeout=45)
    except FileNotFoundError as exc:
        raise AudioError("ffmpeg_unavailable", "未安装 FFmpeg，无法检查录音信号。") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("audio_analysis_timeout", "录音信号检查超时。") from exc
    if completed.returncode != 0 or len(completed.stdout) < 640:
        raise AudioError("invalid_audio", "无法解码足够的录音信号。")
    samples = array("h")
    samples.frombytes(completed.stdout)
    if os.sys.byteorder != "little":
        samples.byteswap()
    normalized = [sample / 32768.0 for sample in samples]
    rms = math.sqrt(sum(sample * sample for sample in normalized) / len(normalized))
    if rms < 0.0032:  # approximately -50 dBFS
        raise AudioError("audio_silent", "录音几乎无声，请重新录制。")
    crossings = sum(
        1 for left, right in zip(normalized, normalized[1:], strict=False) if (left < 0) != (right < 0)
    )
    zero_crossing_rate = crossings / max(1, len(normalized) - 1)
    window = 320
    window_rms = []
    for start in range(0, len(normalized) - window + 1, window):
        values = normalized[start : start + window]
        window_rms.append(math.sqrt(sum(value * value for value in values) / window))
    mean_window = sum(window_rms) / len(window_rms)
    variation = math.sqrt(sum((value - mean_window) ** 2 for value in window_rms) / len(window_rms)) / max(
        mean_window, 1e-9
    )
    if zero_crossing_rate > 0.35 and variation < 0.25:
        raise AudioError("audio_noise", "录音看起来只有持续噪声，请重新录制。")


def _silence_midpoints(source: Path) -> list[float]:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        *_restricted_input_options(_sniff_demuxer(source)),
        "-i",
        str(source),
        "-af",
        "silencedetect=noise=-35dB:d=0.3",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if completed.returncode != 0:
        return []
    starts: list[float] = []
    midpoints: list[float] = []
    for kind, raw_value in _SILENCE_PATTERN.findall(completed.stderr):
        value = float(raw_value)
        if kind == "start":
            starts.append(value)
        elif starts:
            start = starts.pop(0)
            if value > start:
                midpoints.append((start + value) / 2)
    return midpoints


def _segment_boundaries(duration: float, maximum: float, silence_points: list[float]) -> list[float]:
    boundaries = [0.0]
    start = 0.0
    while duration - start > maximum + 0.001:
        hard_end = start + maximum
        candidates = [point for point in silence_points if start + maximum * 0.5 <= point <= hard_end - 0.01]
        end = max(candidates) if candidates else hard_end
        if end <= start + 0.01:
            end = hard_end
        boundaries.append(round(end, 6))
        start = end
    boundaries.append(duration)
    return boundaries


def _sniff_demuxer(path: Path) -> str:
    try:
        with path.open("rb") as source:
            header = source.read(64)
    except OSError as exc:
        raise AudioError("audio_unreadable", "无法读取音频文件头。") from exc
    if len(header) >= 12 and header.startswith(b"RIFF"):
        if header[8:12] == b"WAVE":
            return "wav"
        if header[8:12] == b"AVI ":
            return "avi"
    if len(header) >= 12 and header.startswith(b"FORM") and header[8:12] in {b"AIFF", b"AIFC"}:
        return "aiff"
    if header.startswith(b"\x1aE\xdf\xa3"):
        return "matroska"
    if header.startswith(b"OggS"):
        return "ogg"
    if header.startswith(b"fLaC"):
        return "flac"
    if header.startswith(b"ID3") or (
        len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0 and header[1] & 0xF6 != 0xF0
    ):
        return "mp3"
    if len(header) >= 12 and header[4:8] == b"ftyp":
        return "mov"
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0:
        return "aac"
    if header.startswith((b"#!AMR\n", b"#!AMR-WB\n")):
        return "amr"
    raise AudioError("unsupported_audio_container", "文件头不是受支持的音频容器。")


def _restricted_input_options(demuxer: str) -> list[str]:
    options = [
        "-protocol_whitelist",
        "file,pipe",
        "-protocol_blacklist",
        _BLOCKED_PROTOCOLS,
        "-f",
        demuxer,
    ]
    if demuxer == "mov":
        options.extend(["-enable_drefs", "0", "-use_absolute_path", "0"])
    return options


def _decoded_duration(path: Path, demuxer: str) -> float:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *_restricted_input_options(demuxer),
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-t",
        f"{_MAX_AUDIO_SECONDS + 0.1:.3f}",
        "-f",
        "null",
        "-",
        "-progress",
        "pipe:1",
        "-nostats",
    ]
    try:
        completed = subprocess.run(command, check=False, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise AudioError("ffmpeg_unavailable", "未安装 FFmpeg，无法计算音频时长。") from exc
    except subprocess.TimeoutExpired as exc:
        raise AudioError("audio_analysis_timeout", "音频时长计算超时。") from exc
    if completed.returncode != 0:
        raise AudioError("invalid_audio", "无法通过受限解码计算音频时长。")
    durations = []
    for line in completed.stdout.splitlines():
        if line.startswith("out_time_us="):
            try:
                durations.append(int(line.partition("=")[2]) / 1_000_000)
            except ValueError:
                continue
    if not durations or max(durations) <= 0:
        raise AudioError("invalid_audio", "音频缺少可验证的解码时长。")
    return max(durations)


__all__ = [
    "AudioError",
    "AudioInfo",
    "AudioSegment",
    "assemble_chunks",
    "prepare_asr_segments",
    "probe_audio",
]
