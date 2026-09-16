"""Pure provider adapters for ProbeFlow.

The live adapter intentionally performs one HTTP request per billable operation and
never retries it.  Callers own budget reservation and any user-approved retry.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import io
import ipaddress
import json
import math
import socket
import struct
import wave
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

import httpx


_ROLES = frozenset({"asr", "interview", "tts", "report"})
_MAX_ASR_FILE_BYTES = 7_500_000  # remains below 10 MB after Base64 encoding
_MAX_TTS_DOWNLOAD_BYTES = 20 * 1024 * 1024
_TTS_RESULT_HOSTS = frozenset(
    {
        "dashscope-result-bj.oss-cn-beijing.aliyuncs.com",
        "dashscope-result-hz.oss-cn-hangzhou.aliyuncs.com",
        "dashscope-result-sh.oss-cn-shanghai.aliyuncs.com",
        "dashscope-result-wlcb.oss-cn-wulanchabu.aliyuncs.com",
        "dashscope-result-zjk.oss-cn-zhangjiakou.aliyuncs.com",
        "dashscope-result-sz.oss-cn-shenzhen.aliyuncs.com",
        "dashscope-result-hy.oss-cn-heyuan.aliyuncs.com",
        "dashscope-result-cd.oss-cn-chengdu.aliyuncs.com",
        "dashscope-result-gz.oss-cn-guangzhou.aliyuncs.com",
    }
)


@dataclass(frozen=True, slots=True)
class RoleConfig:
    base_url: str
    model: str
    api_key: str = field(repr=False)
    provider: str = "bailian"
    region: str = "cn-beijing"
    voice: str = "Cherry"


@dataclass(frozen=True, slots=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    reasoning_tokens: int = 0
    audio_seconds: float = 0
    characters: int = 0
    request_id: str | None = None
    source: Literal["actual", "estimated", "unknown"] = "unknown"


@dataclass(frozen=True, slots=True)
class TextResult:
    text: str
    usage: Usage


@dataclass(frozen=True, slots=True)
class ASRResult:
    text: str
    segments: list[dict[str, Any]]
    usage: Usage


@dataclass(frozen=True, slots=True)
class AudioResult:
    content: bytes
    mime_type: str
    usage: Usage


class ProviderError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        external_status_unknown: bool = False,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.external_status_unknown = external_status_unknown
        self.retryable = retryable


Resolver = Callable[[str], Awaitable[Sequence[str]] | Sequence[str]]


class ProviderSuite:
    def __init__(
        self,
        mode: str,
        configs: dict[str, RoleConfig],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolver: Resolver | None = None,
    ) -> None:
        if mode not in {"mock", "live"}:
            raise ValueError("provider mode must be 'mock' or 'live'")
        if mode == "live":
            missing = sorted(_ROLES.difference(configs))
            if missing:
                raise ValueError(f"missing provider role configurations: {', '.join(missing)}")
            for role in sorted(_ROLES):
                config = configs[role]
                if config.provider != "bailian":
                    raise ValueError(f"unsupported provider for role {role}")
                if not config.base_url or not config.model or not config.api_key:
                    raise ValueError(f"incomplete provider configuration for role {role}")
        self.mode = mode
        self.configs = dict(configs)
        self._transport = transport
        self._resolver = resolver or _resolve_public_addresses

    async def text(
        self,
        role: str,
        messages: list[dict[str, Any]],
        *,
        json_mode: bool = True,
        max_tokens: int = 2048,
    ) -> TextResult:
        if self.mode == "mock":
            return _mock_text(messages)
        config = self._config(role)
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "stream": False,
            # The V1 baseline explicitly uses qwen-plus without thinking.
            "enable_thinking": False,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        data, headers = await self._post_json(config, "/chat/completions", payload)
        request_id = _request_id(data, headers)
        text = _choice_text(data)
        if text is None:
            raise ProviderError(
                "provider_status_unknown",
                "供应商返回了无法读取的文本结果；本次调用的计费状态未知。",
                external_status_unknown=True,
            )
        return TextResult(text=text, usage=_usage(data.get("usage"), request_id=request_id))

    async def transcribe(self, path: Path, glossary: list[str]) -> ASRResult:
        if self.mode == "mock":
            return ASRResult(
                text="【模拟转写】这是一段固定的模拟结果，不代表真实语音识别。",
                segments=[],
                usage=Usage(request_id="mock-asr", source="estimated"),
            )
        config = self._config("asr")
        try:
            size = path.stat().st_size
            content = path.read_bytes()
        except OSError as exc:
            raise ProviderError("audio_unreadable", "无法读取待识别音频。") from exc
        if size <= 0:
            raise ProviderError("audio_empty", "待识别音频为空。")
        if size > _MAX_ASR_FILE_BYTES or 4 * ((size + 2) // 3) > 10_000_000:
            raise ProviderError("audio_too_large", "待识别音频超过同步识别的安全体积限制。")
        mime_type = _audio_mime(content)
        if mime_type == "application/octet-stream":
            raise ProviderError("unsupported_audio", "待识别文件不是受支持的音频容器。")
        data_uri = f"data:{mime_type};base64,{base64.b64encode(content).decode('ascii')}"
        messages: list[dict[str, Any]] = []
        clean_glossary = [term.strip() for term in glossary if term.strip()]
        if clean_glossary:
            messages.append({"role": "system", "content": "术语参考：" + "、".join(clean_glossary[:100])})
        messages.append(
            {
                "role": "user",
                "content": [{"type": "input_audio", "input_audio": {"data": data_uri}}],
            }
        )
        payload = {
            "model": config.model,
            "messages": messages,
            "stream": False,
            "asr_options": {"language": "zh", "enable_itn": False},
        }
        data, headers = await self._post_json(config, "/chat/completions", payload)
        request_id = _request_id(data, headers)
        text = _choice_text(data)
        if text is None:
            raise ProviderError(
                "provider_status_unknown",
                "供应商返回了无法读取的转写结果；本次调用的计费状态未知。",
                external_status_unknown=True,
            )
        return ASRResult(
            text=text,
            segments=[],
            usage=_usage(data.get("usage"), request_id=request_id, asr=True),
        )

    async def synthesize(self, text: str) -> AudioResult:
        if self.mode == "mock":
            return AudioResult(
                content=_mock_hint_wav(),
                mime_type="audio/wav",
                usage=Usage(characters=len(text), request_id="mock-tts", source="estimated"),
            )
        if not text or len(text) > 600:
            raise ProviderError("invalid_tts_text", "播报文本必须为 1 至 600 个字符。")
        config = self._config("tts")
        payload = {
            "model": config.model,
            "input": {"text": text, "voice": config.voice, "language_type": "Chinese"},
        }
        data, headers = await self._post_json(
            config, "/services/aigc/multimodal-generation/generation", payload
        )
        request_id = _request_id(data, headers)
        usage = _usage(data.get("usage"), request_id=request_id, tts=True)
        try:
            result_url = data["output"]["audio"]["url"]
        except (KeyError, TypeError):
            raise ProviderError(
                "provider_status_unknown",
                "供应商未返回可下载的合成音频；本次调用的计费状态未知。",
                external_status_unknown=True,
            ) from None
        if not isinstance(result_url, str):
            raise ProviderError(
                "provider_status_unknown",
                "供应商未返回可下载的合成音频；本次调用的计费状态未知。",
                external_status_unknown=True,
            )
        content, mime_type = await self._download_tts(result_url)
        return AudioResult(content=content, mime_type=mime_type, usage=usage)

    def _config(self, role: str) -> RoleConfig:
        if role not in _ROLES or role not in self.configs:
            raise ProviderError("provider_role_missing", f"未配置供应商角色：{role}")
        return self.configs[role]

    async def _post_json(
        self, config: RoleConfig, endpoint: str, payload: dict[str, Any]
    ) -> tuple[dict[str, Any], httpx.Headers]:
        url = _join_url(config.base_url, endpoint)
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=False,
            ) as client:
                response = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {config.api_key}", "Content-Type": "application/json"},
                    json=payload,
                )
        except httpx.RequestError as exc:
            raise ProviderError(
                "provider_status_unknown",
                "供应商请求未能确认结果；本次调用可能已经执行或计费。",
                external_status_unknown=True,
            ) from exc
        if response.status_code >= 500:
            raise ProviderError(
                "provider_status_unknown",
                "供应商发生服务端错误，无法确认请求是否已经执行或计费。",
                external_status_unknown=True,
            )
        if not 200 <= response.status_code < 300:
            raise ProviderError(
                f"provider_http_{response.status_code}",
                f"供应商明确拒绝请求（HTTP {response.status_code}）。",
                retryable=response.status_code == 429,
            )
        try:
            data = response.json()
        except (ValueError, UnicodeError) as exc:
            raise ProviderError(
                "provider_status_unknown",
                "供应商响应无法解析；本次调用的计费状态未知。",
                external_status_unknown=True,
            ) from exc
        if not isinstance(data, dict):
            raise ProviderError(
                "provider_status_unknown",
                "供应商响应格式异常；本次调用的计费状态未知。",
                external_status_unknown=True,
            )
        return data, response.headers

    async def _download_tts(self, url: str) -> tuple[bytes, str]:
        try:
            parsed = urlsplit(url)
            host = (parsed.hostname or "").lower()
            port = parsed.port
        except ValueError as exc:
            raise _unsafe_audio_url() from exc
        valid_port = (parsed.scheme == "https" and port in {None, 443}) or (
            parsed.scheme == "http" and port in {None, 80}
        )
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or not valid_port
            or host not in _TTS_RESULT_HOSTS
            or not parsed.path
            or parsed.fragment
        ):
            raise _unsafe_audio_url()
        # Alibaba Cloud documents that OSS bucket domains support HTTPS
        # natively. Some Bailian TTS examples still return an http URL; for
        # exact allowlisted OSS hosts, upgrade the scheme before any request.
        secure_url = urlunsplit(("https", host, parsed.path, parsed.query, ""))
        try:
            addresses = self._resolver(host)
            if inspect.isawaitable(addresses):
                addresses = await addresses
            parsed_addresses = [ipaddress.ip_address(address) for address in addresses]
        except (OSError, ValueError) as exc:
            raise _unsafe_audio_url() from exc
        if not parsed_addresses or any(not address.is_global for address in parsed_addresses):
            raise _unsafe_audio_url()
        try:
            async with httpx.AsyncClient(
                transport=self._transport,
                timeout=httpx.Timeout(20.0, connect=5.0),
                follow_redirects=False,
            ) as client:
                async with client.stream("GET", secure_url, headers={"Accept": "audio/*"}) as response:
                    if response.is_redirect:
                        raise _unsafe_audio_url()
                    if response.status_code != 200:
                        raise ProviderError(
                            "tts_download_failed",
                            "合成已完成，但音频下载失败；本次调用的计费状态未知。",
                            external_status_unknown=True,
                        )
                    declared_length = response.headers.get("content-length")
                    if declared_length:
                        try:
                            if int(declared_length) > _MAX_TTS_DOWNLOAD_BYTES:
                                raise ProviderError(
                                    "tts_audio_too_large",
                                    "合成音频超过安全下载大小限制；本次调用的计费状态未知。",
                                    external_status_unknown=True,
                                )
                        except ValueError:
                            raise ProviderError(
                                "tts_download_failed",
                                "音频下载响应无效；本次调用的计费状态未知。",
                                external_status_unknown=True,
                            ) from None
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_TTS_DOWNLOAD_BYTES:
                            raise ProviderError(
                                "tts_audio_too_large",
                                "合成音频超过安全下载大小限制；本次调用的计费状态未知。",
                                external_status_unknown=True,
                            )
                        chunks.append(chunk)
                    content = b"".join(chunks)
        except ProviderError:
            raise
        except httpx.RequestError as exc:
            raise ProviderError(
                "tts_download_failed",
                "合成已完成，但音频下载状态未知；本次调用可能已经计费。",
                external_status_unknown=True,
            ) from exc
        mime_type = _audio_mime(content)
        if not content or mime_type == "application/octet-stream":
            raise ProviderError(
                "tts_download_failed",
                "供应商下载结果不是受支持的音频；本次调用的计费状态未知。",
                external_status_unknown=True,
            )
        return content, mime_type


def _join_url(base_url: str, endpoint: str) -> str:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ProviderError("invalid_provider_url", "供应商地址必须是未嵌入凭证的 HTTPS 地址。")
    base = base_url.rstrip("/")
    if base.endswith(endpoint):
        return base
    return base + endpoint


def _request_id(data: dict[str, Any], headers: httpx.Headers) -> str | None:
    value = data.get("request_id") or data.get("id") or headers.get("x-request-id")
    return str(value) if value is not None else None


def _usage(
    raw: Any,
    *,
    request_id: str | None,
    asr: bool = False,
    tts: bool = False,
) -> Usage:
    if not isinstance(raw, Mapping):
        return Usage(request_id=request_id, source="unknown")
    recognized = {
        "prompt_tokens",
        "input_tokens",
        "completion_tokens",
        "output_tokens",
        "seconds",
        "characters",
    }.intersection(raw)
    if not recognized:
        return Usage(request_id=request_id, source="unknown")
    prompt_details = raw.get("prompt_tokens_details", raw.get("input_tokens_details", {}))
    completion_details = raw.get("completion_tokens_details", raw.get("output_tokens_details", {}))
    if not isinstance(prompt_details, Mapping) or not isinstance(completion_details, Mapping):
        return Usage(request_id=request_id, source="unknown")
    input_tokens = _nonnegative_integer(raw.get("prompt_tokens", raw.get("input_tokens", 0)))
    output_tokens = _nonnegative_integer(raw.get("completion_tokens", raw.get("output_tokens", 0)))
    cached_tokens = _nonnegative_integer(prompt_details.get("cached_tokens", 0))
    reasoning_tokens = _nonnegative_integer(completion_details.get("reasoning_tokens", 0))
    audio_seconds = _nonnegative_number(raw.get("seconds", 0)) if asr else 0.0
    characters = _nonnegative_integer(raw.get("characters", 0)) if tts else 0
    if None in {
        input_tokens,
        output_tokens,
        cached_tokens,
        reasoning_tokens,
        audio_seconds,
        characters,
    }:
        return Usage(request_id=request_id, source="unknown")
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cached_tokens=cached_tokens,
        reasoning_tokens=reasoning_tokens,
        audio_seconds=audio_seconds,
        characters=characters,
        request_id=request_id,
        source="actual",
    )


def _nonnegative_integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0 or int(value) != value:
        return None
    return int(value)


def _nonnegative_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _choice_text(data: dict[str, Any]) -> str | None:
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [item.get("text", "") for item in content if isinstance(item, dict)]
        return "".join(part for part in parts if isinstance(part, str)) or None
    return None


def _mock_text(messages: list[dict[str, Any]]) -> TextResult:
    try:
        last_user = next(message for message in reversed(messages) if message.get("role") == "user")
        payload = json.loads(last_user["content"])
        if not isinstance(payload, dict):
            raise ValueError
    except (KeyError, StopIteration, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ProviderError("mock_input_invalid", "模拟文本请求缺少有效的末条 user JSON。") from exc
    from app.interview import mock_response

    result = mock_response(payload)
    return TextResult(
        text=json.dumps(result, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        usage=Usage(request_id="mock-text", source="estimated"),
    )


def _mock_hint_wav() -> bytes:
    """Return a short two-tone cue; this is deliberately not simulated Mandarin."""
    sample_rate = 16_000
    frames = bytearray()
    for index in range(int(sample_rate * 0.24)):
        frequency = 660 if index < sample_rate * 0.12 else 880
        sample = int(4_000 * math.sin(2 * math.pi * frequency * index / sample_rate))
        frames.extend(struct.pack("<h", sample))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(frames)
    return buffer.getvalue()


def _audio_mime(content: bytes) -> str:
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WAVE":
        return "audio/wav"
    if content.startswith(b"OggS"):
        return "audio/ogg"
    if content.startswith(b"fLaC"):
        return "audio/flac"
    if content.startswith(b"ID3") or content[:2] in {b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"}:
        return "audio/mpeg"
    if content.startswith(b"\x1aE\xdf\xa3"):
        return "audio/webm"
    if len(content) >= 12 and content[4:8] == b"ftyp":
        return "audio/mp4"
    return "application/octet-stream"


def _unsafe_audio_url() -> ProviderError:
    return ProviderError(
        "unsafe_audio_url",
        "供应商返回的音频地址未通过安全校验；本次调用的计费状态未知。",
        external_status_unknown=True,
    )


async def _resolve_public_addresses(host: str) -> list[str]:
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    return sorted({record[4][0] for record in records})


__all__ = [
    "ASRResult",
    "AudioResult",
    "ProviderError",
    "ProviderSuite",
    "RoleConfig",
    "TextResult",
    "Usage",
]
