from __future__ import annotations

import hashlib
import subprocess
import wave
from pathlib import Path

import pytest

from app.audio import AudioError, assemble_chunks, prepare_asr_segments, probe_audio


def _ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
    )


def test_probe_uses_media_content_instead_of_extension(tmp_path: Path) -> None:
    disguised = tmp_path / "recording.wav"
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.5",
        "-c:a",
        "libopus",
        "-f",
        "ogg",
        str(disguised),
    )

    info = probe_audio(disguised)

    assert info.mime_type == "audio/ogg"
    assert 0.45 <= info.duration_seconds <= 0.55
    assert info.byte_size == disguised.stat().st_size
    assert info.sha256 == hashlib.sha256(disguised.read_bytes()).hexdigest()


def test_probe_rejects_non_audio_bytes(tmp_path: Path) -> None:
    source = tmp_path / "fake.webm"
    source.write_text("not audio", encoding="utf-8")

    with pytest.raises(AudioError) as caught:
        probe_audio(source)

    assert caught.value.code == "unsupported_audio_container"
    assert str(source) not in caught.value.message


def test_probe_blocks_playlist_before_ffprobe_can_follow_external_or_local_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    playlist = tmp_path / "recording.wav"
    playlist.write_text(
        "#EXTM3U\nhttps://127.0.0.1/private.wav\nfile:///etc/passwd\n",
        encoding="utf-8",
    )
    process_started = False

    def forbidden_run(*args, **kwargs):
        nonlocal process_started
        process_started = True
        raise AssertionError("FFprobe must not receive an unrecognized container")

    monkeypatch.setattr(subprocess, "run", forbidden_run)

    with pytest.raises(AudioError) as caught:
        probe_audio(playlist)

    assert caught.value.code == "unsupported_audio_container"
    assert process_started is False


def test_probe_derives_duration_for_streamed_mediarecorder_webm(tmp_path: Path) -> None:
    source = tmp_path / "streamed.webm"
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=1.25",
            "-c:a",
            "libopus",
            "-f",
            "webm",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    source.write_bytes(completed.stdout)

    info = probe_audio(source)

    assert info.mime_type == "audio/webm"
    assert info.duration_seconds == pytest.approx(1.25, abs=0.08)


def test_probe_rejects_audio_over_ten_minutes(tmp_path: Path) -> None:
    source = tmp_path / "too-long.webm"
    completed = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=8000:duration=601",
            "-c:a",
            "libopus",
            "-b:a",
            "8k",
            "-f",
            "webm",
            "pipe:1",
        ],
        check=True,
        capture_output=True,
    )
    source.write_bytes(completed.stdout)

    with pytest.raises(AudioError) as caught:
        probe_audio(source)

    assert caught.value.code == "audio_too_long"


def test_prepare_rejects_silent_recording(tmp_path: Path) -> None:
    source = tmp_path / "silent.wav"
    _ffmpeg("-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo", "-t", "1", str(source))

    with pytest.raises(AudioError) as caught:
        prepare_asr_segments(source, tmp_path / "segments")

    assert caught.value.code == "audio_silent"


def test_prepare_rejects_obvious_stationary_white_noise(tmp_path: Path) -> None:
    source = tmp_path / "noise.wav"
    _ffmpeg("-f", "lavfi", "-i", "anoisesrc=color=white:amplitude=0.15:duration=2", str(source))

    with pytest.raises(AudioError) as caught:
        prepare_asr_segments(source, tmp_path / "segments")

    assert caught.value.code == "audio_noise"


def test_prepare_creates_independently_decodable_pcm_segments_at_silence(tmp_path: Path) -> None:
    source = tmp_path / "long.webm"
    # A pause around 1.6 s gives the splitter a better boundary than the hard 2 s limit.
    _ffmpeg(
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=1.4",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=mono:d=0.4",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=660:duration=1.5",
        "-filter_complex",
        "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
        "-map",
        "[out]",
        "-c:a",
        "libopus",
        str(source),
    )

    segments = prepare_asr_segments(source, tmp_path / "segments", max_seconds=2)

    assert len(segments) == 2
    assert 1.5 <= segments[0].end_seconds <= 1.75
    assert segments[1].start_seconds == segments[0].end_seconds
    assert segments[-1].end_seconds == pytest.approx(probe_audio(source).duration_seconds, abs=0.06)
    for segment in segments:
        info = probe_audio(segment.path)
        assert info.mime_type == "audio/wav"
        assert info.duration_seconds <= 2.05
        assert info.byte_size * 4 // 3 + 4 <= 10_000_000
        with wave.open(str(segment.path), "rb") as wav:
            assert wav.getnchannels() == 1
            assert wav.getframerate() == 16000
            assert wav.getsampwidth() == 2


def test_prepare_validates_maximum_segment_duration(tmp_path: Path) -> None:
    source = tmp_path / "tone.wav"
    _ffmpeg("-f", "lavfi", "-i", "sine=frequency=440:duration=1", str(source))

    with pytest.raises(AudioError) as caught:
        prepare_asr_segments(source, tmp_path / "segments", max_seconds=181)

    assert caught.value.code == "invalid_segment_limit"


def test_assemble_chunks_preserves_order_and_replaces_existing_target(tmp_path: Path) -> None:
    chunks = []
    for index, content in enumerate((b"first", b"second", b"third")):
        path = tmp_path / f"chunk-{index}"
        path.write_bytes(content)
        chunks.append(path)
    target = tmp_path / "assembled.webm"
    target.write_bytes(b"old")

    result = assemble_chunks(chunks, target)

    assert result == target
    assert target.read_bytes() == b"firstsecondthird"
    assert not list(tmp_path.glob(".assembled.webm.*.tmp"))


def test_assemble_chunks_rejects_missing_chunk_without_touching_target(tmp_path: Path) -> None:
    present = tmp_path / "present"
    present.write_bytes(b"present")
    target = tmp_path / "assembled.webm"
    target.write_bytes(b"existing")

    with pytest.raises(AudioError) as caught:
        assemble_chunks([present, tmp_path / "missing"], target)

    assert caught.value.code == "missing_chunk"
    assert target.read_bytes() == b"existing"
