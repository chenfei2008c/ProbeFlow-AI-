import csv
import io
import json

from app.models import InterviewSession
from test_audio_flow import ready, upload, wav_bytes
from test_core import headers
from test_interview_flow import drain


def test_request_limit_applies_to_chunked_json_without_content_length(admin):
    response = admin.post(
        "/api/admin/studies",
        content=(b"x" * (1024 * 1024) for _ in range(13)),
        headers={**headers(), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["code"] == "REQUEST_TOO_LARGE"


def test_cli_check_reads_local_env_credentials_without_printing_them(tmp_path, monkeypatch, capsys):
    from app.cli import main

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        f'DATA_DIR="{tmp_path / "data"}"\nMODE=mock\nDASHSCOPE_API_KEY=fictional-test-only\n'
    )
    monkeypatch.setattr("sys.argv", ["probeflow", "check"])
    assert main() == 0
    output = capsys.readouterr().out
    assert "fictional-test-only" not in output
    assert all(value["credential_configured"] for value in json.loads(output)["roles"].values())


def test_target_time_requires_explicit_extension_before_new_answer(admin, app):
    participant, sid = ready(admin, app, "text")
    with app.state.db.transaction() as db:
        db.get(InterviewSession, sid).active_seconds = 3600
    answer = {"input_mode": "text", "text": "延长后回答。"}
    response = participant.post("/api/participant/turns", json=answer, headers=headers())
    assert response.status_code == 409
    assert response.json()["code"] == "TARGET_TIME_REACHED"
    assert (
        participant.post("/api/participant/control", json={"action": "extend"}, headers=headers()).status_code
        == 200
    )
    assert participant.post("/api/participant/turns", json=answer, headers=headers()).status_code == 200


def test_end_cannot_leave_an_unfinalized_recording_behind(admin, app):
    participant, _ = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    _, manifest = upload(participant, tid, 0, wav_bytes())
    response = participant.post("/api/participant/control", json={"action": "end"}, headers=headers())
    assert response.status_code == 409
    assert response.json()["code"] == "UPLOAD_INCOMPLETE"
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers()
        ).status_code
        == 200
    )
    drain(app)
    assert (
        participant.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code
        == 200
    )


def test_exports_preserve_text_but_escape_active_markdown_and_csv_formulas(admin, app):
    participant, sid = ready(admin, app, "text")
    original = (
        "=1+1 [点此](javascript:alert(1)) <script>alert(1)</script> ![图片](https://example.invalid/tracker)"
    )
    tid = participant.post(
        "/api/participant/turns", json={"input_mode": "text", "text": original}, headers=headers()
    ).json()["turn_id"]
    response = admin.get(f"/api/admin/sessions/{sid}/export?format=markdown")
    assert "[点此](javascript:" not in response.text
    assert "![图片](https:" not in response.text
    assert "<script>" not in response.text
    assert "\\[点此\\]" in response.text
    exported = admin.get(f"/api/admin/sessions/{sid}/export?format=csv")
    rows = list(csv.reader(io.StringIO(exported.text.removeprefix("\ufeff"))))
    assert next(row for row in rows if row[2] == tid)[6] == "'" + original
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/confirm", json={"text": original}, headers=headers()
        ).status_code
        == 200
    )
    drain(app)
    participant.post("/api/participant/control", json={"action": "end"}, headers=headers())
    drain(app)
    markdown = admin.get(f"/api/admin/sessions/{sid}/export?format=markdown").text
    assert "[点此](javascript:" not in markdown and "<script>" not in markdown
    assert "逐字稿" in markdown and "文本版本 1" in markdown
    assert admin.get(f"/api/admin/sessions/{sid}/export").json()["turns"][1]["text"] == original
