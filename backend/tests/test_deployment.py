import pytest


@pytest.mark.parametrize("arguments, trusted", [([], ""), (["--trusted-proxy", "127.0.0.1"], "127.0.0.1")])
def test_start_only_trusts_explicit_proxy_addresses(tmp_path, monkeypatch, arguments, trusted):
    from app.cli import main
    import uvicorn

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda *args, **kwargs: calls.append(kwargs))
    assert main(["start", *arguments]) == 0
    assert calls[0]["host"] == "127.0.0.1"
    assert calls[0]["workers"] == 1
    assert calls[0]["proxy_headers"] is bool(trusted)
    assert calls[0]["forwarded_allow_ips"] == trusted
    assert calls[0]["access_log"] is False


def test_start_rejects_trusting_arbitrary_forwarded_headers(monkeypatch, tmp_path):
    from app.cli import main

    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as error:
        main(["start", "--trusted-proxy", "*"])
    assert error.value.code == 2
