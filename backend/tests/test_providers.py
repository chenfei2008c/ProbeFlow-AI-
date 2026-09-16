from __future__ import annotations

import io
import json
import sys
import types
import wave
from pathlib import Path

import httpx
import pytest

from app.providers import ProviderError, ProviderSuite, RoleConfig


class _OversizeAudioStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"RIFF" + b"\x00" * (10 * 1024 * 1024)
        yield b"\x00" * (11 * 1024 * 1024)


def _configs() -> dict[str, RoleConfig]:
    compatible = "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
    dashscope = "https://workspace.cn-beijing.maas.aliyuncs.com/api/v1"
    return {
        "asr": RoleConfig(base_url=compatible, model="qwen3-asr-flash", api_key="asr-secret"),
        "interview": RoleConfig(base_url=compatible, model="qwen-plus-interview", api_key="interview-secret"),
        "tts": RoleConfig(base_url=dashscope, model="qwen3-tts-flash", api_key="tts-secret"),
        "report": RoleConfig(base_url=compatible, model="qwen-plus-report", api_key="report-secret"),
    }


async def _public_resolver(host: str) -> list[str]:
    assert host == "dashscope-result-bj.oss-cn-beijing.aliyuncs.com"
    return ["8.8.8.8"]


def test_role_config_repr_does_not_disclose_api_key() -> None:
    config = RoleConfig(base_url="https://example.com/v1", model="m", api_key="top-secret")

    assert "top-secret" not in repr(config)


@pytest.mark.asyncio
async def test_text_uses_role_specific_config_and_normalizes_usage() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "req-text",
                "choices": [{"message": {"content": '{"action":"ask"}'}}],
                "usage": {
                    "prompt_tokens": 101,
                    "completion_tokens": 12,
                    "prompt_tokens_details": {"cached_tokens": 7},
                    "completion_tokens_details": {"reasoning_tokens": 3},
                },
            },
        )

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))
    result = await suite.text("report", [{"role": "user", "content": "{}"}])

    assert result.text == '{"action":"ask"}'
    assert result.usage.input_tokens == 101
    assert result.usage.output_tokens == 12
    assert result.usage.cached_tokens == 7
    assert result.usage.reasoning_tokens == 3
    assert result.usage.source == "actual"
    assert result.usage.request_id == "req-text"
    assert len(seen) == 1
    assert str(seen[0].url).endswith("/compatible-mode/v1/chat/completions")
    body = json.loads(seen[0].content)
    assert body == {
        "model": "qwen-plus-report",
        "messages": [{"role": "user", "content": "{}"}],
        "max_tokens": 2048,
        "stream": False,
        "enable_thinking": False,
        "response_format": {"type": "json_object"},
    }
    assert seen[0].headers["authorization"] == "Bearer report-secret"


@pytest.mark.asyncio
async def test_success_without_usage_stays_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"id": "req-1", "choices": [{"message": {"content": "ok"}}]})

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))

    result = await suite.text("interview", [{"role": "user", "content": "hello"}], json_mode=False)

    assert result.usage.source == "unknown"
    assert result.usage.request_id == "req-1"


@pytest.mark.asyncio
async def test_timeout_is_external_status_unknown_and_error_redacts_secrets() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("request with interview-secret timed out", request=request)

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as caught:
        await suite.text("interview", [{"role": "user", "content": "sensitive answer"}])

    assert caught.value.code == "provider_status_unknown"
    assert caught.value.external_status_unknown is True
    assert caught.value.retryable is False
    assert "secret" not in caught.value.message
    assert "sensitive" not in caught.value.message


@pytest.mark.asyncio
async def test_explicit_http_rejection_is_not_unknown_and_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(429, json={"code": "Throttled", "message": "try later"})

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as caught:
        await suite.text("interview", [{"role": "user", "content": "hello"}])

    assert calls == 1
    assert caught.value.external_status_unknown is False
    assert caught.value.retryable is True
    assert caught.value.code == "provider_http_429"


@pytest.mark.asyncio
async def test_server_error_is_unknown_because_processing_may_have_started() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(503, json={"code": "ServiceUnavailable"})

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as caught:
        await suite.text("interview", [{"role": "user", "content": "hello"}])

    assert calls == 1
    assert caught.value.code == "provider_status_unknown"
    assert caught.value.external_status_unknown is True
    assert caught.value.retryable is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 2, "completion_tokens": 1, "prompt_tokens_details": []},
        {"prompt_tokens": float("nan"), "completion_tokens": 1},
        {"prompt_tokens": 2, "completion_tokens": float("inf")},
    ],
)
async def test_malformed_usage_is_controlled_and_kept_unknown(usage: dict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {"id": "req-malformed", "choices": [{"message": {"content": "ok"}}], "usage": usage}
            ).encode(),
            headers={"content-type": "application/json"},
        )

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))

    result = await suite.text("interview", [{"role": "user", "content": "hello"}], json_mode=False)

    assert result.usage.source == "unknown"
    assert result.usage.request_id == "req-malformed"


@pytest.mark.asyncio
async def test_asr_sends_base64_audio_and_glossary_and_records_seconds(tmp_path: Path) -> None:
    audio = tmp_path / "answer.wav"
    with wave.open(str(audio), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 160)
    request_body: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        request_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "req-asr",
                "choices": [{"message": {"content": "这是转写。"}}],
                "usage": {
                    "prompt_tokens": 75,
                    "completion_tokens": 5,
                    "prompt_tokens_details": {"audio_tokens": 75, "text_tokens": 0},
                    "seconds": 3,
                },
            },
        )

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))
    result = await suite.transcribe(audio, ["ProbeFlow", "授信"])

    assert result.text == "这是转写。"
    assert result.segments == []
    assert result.usage.audio_seconds == 3
    assert result.usage.source == "actual"
    assert request_body["model"] == "qwen3-asr-flash"
    assert request_body["messages"][0] == {"role": "system", "content": "术语参考：ProbeFlow、授信"}
    data = request_body["messages"][1]["content"][0]["input_audio"]["data"]
    assert data.startswith("data:audio/wav;base64,")
    assert request_body["asr_options"] == {"language": "zh", "enable_itn": False}


@pytest.mark.asyncio
async def test_asr_rejects_unknown_content_despite_audio_extension(tmp_path: Path) -> None:
    audio = tmp_path / "not-really.wav"
    audio.write_bytes(b"this is not a wav file")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("invalid audio must not be sent to the paid endpoint")

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as caught:
        await suite.transcribe(audio, [])

    assert caught.value.code == "unsupported_audio"


@pytest.mark.asyncio
async def test_tts_download_is_https_allowlisted_bounded_and_usage_is_preserved() -> None:
    result_url = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/output.wav?signature=x"
    wav = io.BytesIO()
    with wave.open(wav, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b"\x00\x00" * 80)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.method == "POST":
            assert json.loads(request.content) == {
                "model": "qwen3-tts-flash",
                "input": {"text": "你好", "voice": "Cherry", "language_type": "Chinese"},
            }
            return httpx.Response(
                200,
                json={
                    "request_id": "req-tts",
                    "output": {"audio": {"url": result_url}},
                    "usage": {"input_tokens": 0, "output_tokens": 0, "characters": 2},
                },
            )
        assert "authorization" not in request.headers
        return httpx.Response(200, content=wav.getvalue(), headers={"content-type": "audio/wav"})

    suite = ProviderSuite(
        "live", _configs(), transport=httpx.MockTransport(handler), resolver=_public_resolver
    )
    result = await suite.synthesize("你好")

    assert result.content == wav.getvalue()
    assert result.mime_type == "audio/wav"
    assert result.usage.characters == 2
    assert result.usage.source == "actual"
    assert result.usage.request_id == "req-tts"
    assert seen[0].endswith("/api/v1/services/aigc/multimodal-generation/generation")
    assert seen[1] == result_url


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/a.wav",
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com.evil.test/a.wav",
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com:notaport/a.wav",
    ],
)
async def test_tts_rejects_unsafe_result_urls(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"request_id": "req", "output": {"audio": {"url": url}}, "usage": {"characters": 2}},
        )

    suite = ProviderSuite("live", _configs(), transport=httpx.MockTransport(handler))

    with pytest.raises(ProviderError) as caught:
        await suite.synthesize("你好")

    assert caught.value.code == "unsafe_audio_url"
    assert caught.value.external_status_unknown is True


@pytest.mark.asyncio
async def test_tts_upgrades_exact_allowlisted_http_result_url_to_https() -> None:
    supplied = "http://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav?Expires=1&Signature=x"
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "request_id": "req",
                    "output": {"audio": {"url": supplied}},
                    "usage": {"characters": 2},
                },
            )
        requested.append(str(request.url))
        wav = io.BytesIO()
        with wave.open(wav, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x00\x00" * 80)
        return httpx.Response(200, content=wav.getvalue())

    suite = ProviderSuite(
        "live", _configs(), transport=httpx.MockTransport(handler), resolver=_public_resolver
    )

    await suite.synthesize("你好")

    assert requested == [
        "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav?Expires=1&Signature=x"
    ]


@pytest.mark.asyncio
async def test_tts_rejects_private_dns_resolution() -> None:
    async def private_resolver(host: str) -> list[str]:
        return ["10.0.0.3"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "request_id": "req",
                "output": {"audio": {"url": "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav"}},
                "usage": {"characters": 2},
            },
        )

    suite = ProviderSuite(
        "live", _configs(), transport=httpx.MockTransport(handler), resolver=private_resolver
    )

    with pytest.raises(ProviderError) as caught:
        await suite.synthesize("你好")

    assert caught.value.code == "unsafe_audio_url"


@pytest.mark.asyncio
@pytest.mark.parametrize("download_failure", ["redirect", "oversize", "timeout"])
async def test_tts_download_never_follows_redirects_and_enforces_bounds(download_failure: str) -> None:
    result_url = "https://dashscope-result-bj.oss-cn-beijing.aliyuncs.com/a.wav"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "request_id": "req",
                    "output": {"audio": {"url": result_url}},
                    "usage": {"characters": 2},
                },
            )
        if download_failure == "redirect":
            return httpx.Response(302, headers={"location": "https://example.com/a.wav"})
        if download_failure == "oversize":
            # No Content-Length: the downloader must enforce the bound while streaming.
            return httpx.Response(200, stream=_OversizeAudioStream())
        raise httpx.ReadTimeout("read timed out", request=request)

    suite = ProviderSuite(
        "live", _configs(), transport=httpx.MockTransport(handler), resolver=_public_resolver
    )

    with pytest.raises(ProviderError) as caught:
        await suite.synthesize("你好")

    assert caught.value.external_status_unknown is True
    expected_codes = {
        "redirect": "unsafe_audio_url",
        "oversize": "tts_audio_too_large",
        "timeout": "tts_download_failed",
    }
    assert caught.value.code == expected_codes[download_failure]


@pytest.mark.asyncio
async def test_mock_mode_is_deterministic_and_audio_is_only_a_hint_sound(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    interview = types.ModuleType("app.interview")
    interview.mock_response = lambda payload: {"task": payload["task"], "simulated": True}
    monkeypatch.setitem(sys.modules, "app.interview", interview)
    suite = ProviderSuite("mock", {})

    first = await suite.text("interview", [{"role": "user", "content": '{"task":"decide"}'}])
    second = await suite.text("interview", [{"role": "user", "content": '{"task":"decide"}'}])
    transcription = await suite.transcribe(Path("unused.wav"), [])
    audio = await suite.synthesize("这段文字不会被模拟语音朗读")

    assert first == second
    assert json.loads(first.text) == {"task": "decide", "simulated": True}
    assert "模拟" in transcription.text
    assert audio.mime_type == "audio/wav"
    with wave.open(io.BytesIO(audio.content), "rb") as hint:
        assert hint.getnframes() > 0
        assert hint.getnchannels() == 1


def test_live_mode_requires_four_independent_role_configs() -> None:
    configs = _configs()
    del configs["report"]

    with pytest.raises(ValueError, match="report"):
        ProviderSuite("live", configs)
