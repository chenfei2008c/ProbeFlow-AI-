import asyncio
import hashlib
import io
import math
import struct
import wave

from sqlalchemy import select

from app.domain import add_text_revision, new_turn
from app.models import InterviewSession, Job, Turn, Asset
from app.worker import Worker
from test_core import headers, join, new_study


def ready(admin, app, mode="voice"):
    p, sid, _ = join(admin, app, new_study(admin))
    version = p.get("/api/config").json()["consent_version"]
    assert (
        p.post(
            "/api/participant/consent",
            json={"version": version, "mode": mode, "processing": True, "permanent": True},
            headers=headers(),
        ).status_code
        == 200
    )
    # This fixture tests transport independently of generation quality.
    with app.state.db.transaction() as db:
        job = db.scalar(select(Job).where(Job.session_id == sid))
        job.status = "succeeded"
        session = db.get(InterviewSession, sid)
        session.status = "in_progress"
        turn = new_turn(db, session, "assistant", "text", "ready_to_record", confirmed=True, topic_id="T1")
        add_text_revision(db, turn, "请描述一次具体经历。", "model", "system")
    return p, sid


def wav_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(16000)
        out.writeframes(
            b"".join(
                struct.pack("<h", int(5000 * math.sin(i * 2 * math.pi * 440 / 16000))) for i in range(16000)
            )
        )
    return stream.getvalue()


def upload(p, tid, seq, content):
    digest = hashlib.sha256(content).hexdigest()
    response = p.put(
        f"/api/participant/turns/{tid}/chunks/{seq}",
        content=content,
        headers={**headers(), "X-Chunk-SHA256": digest, "Content-Type": "application/octet-stream"},
    )
    return response, {"seq": seq, "sha256": digest}


def test_same_size_corrupted_archive_is_not_served_as_valid_audio(admin, app):
    from test_interview_flow import drain

    participant, sid = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    original = wav_bytes()
    _, manifest = upload(participant, tid, 0, original)
    participant.post(f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers())
    drain(app)
    with app.state.db.read() as db:
        asset = db.scalar(select(Asset).where(Asset.turn_id == tid))
        asset_id = asset.id
        path = app.state.settings.data_dir / asset.path
    path.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
    response = participant.get(f"/api/media/{asset_id}")
    assert response.status_code == 409
    assert response.json()["code"] == "ARCHIVE_CORRUPTED"
    assert admin.get(f"/api/admin/sessions/{sid}/export").status_code == 200


def test_voice_manifest_idempotency_original_and_machine_revision_survive(admin, app):
    p, sid = ready(admin, app)
    key = headers()
    body = {"input_mode": "voice", "mime_type": "audio/wav"}
    created = p.post("/api/participant/turns", json=body, headers=key)
    assert created.status_code == 200, created.text
    tid = created.json()["turn_id"]
    assert p.post("/api/participant/turns", json=body, headers=key).json()["turn_id"] == tid
    content = wav_bytes()
    one, item = upload(p, tid, 0, content)
    assert one.status_code == 200
    assert upload(p, tid, 0, content)[0].status_code == 200
    assert upload(p, tid, 0, content + b"x")[0].status_code == 409
    invalid = p.post(
        f"/api/participant/turns/{tid}/finalize", json={"chunks": [{**item, "seq": 1}]}, headers=headers()
    )
    assert invalid.status_code == 409
    with app.state.db.read() as db:
        assert not db.scalar(select(Job).where(Job.kind == "asr"))
    finalized = p.post(f"/api/participant/turns/{tid}/finalize", json={"chunks": [item]}, headers=headers())
    assert finalized.status_code == 200
    assert (
        p.post(f"/api/participant/turns/{tid}/finalize", json={"chunks": [item]}, headers=headers()).json()
        == finalized.json()
    )
    assert asyncio.run(Worker(app.state.db, app.state.settings).run_once())
    with app.state.db.read() as db:
        job = db.get(Job, finalized.json()["job_id"])
        assert job.status == "succeeded", job.error_message
        turn = db.get(Turn, tid)
        assert turn.status == "confirming" and not turn.confirmed
        asset = db.get(Asset, turn.audio_asset_id)
        assert asset.retention == "permanent" and asset.expires_at is None
        asset_id = asset.id
    assert p.get(f"/api/media/{asset_id}").content == content
    assert admin.get(f"/api/media/{asset_id}").content == content
    assert (
        p.post(
            f"/api/participant/turns/{tid}/confirm",
            json={"text": "虚构录音的手工确认内容。"},
            headers=headers(),
        ).status_code
        == 200
    )
    records = p.get("/api/participant/session").json()["turns"][-1]["revisions"]
    assert len(records) == 2


def test_text_consent_cannot_upload_voice_and_safe_time_limit(admin, app):
    p, sid = ready(admin, app, "text")
    assert (
        p.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).status_code == 403
    )
    with app.state.db.transaction() as db:
        db.get(InterviewSession, sid).active_seconds = 5400
    response = p.post(
        "/api/participant/turns", json={"input_mode": "text", "text": "不应开始新的一轮"}, headers=headers()
    )
    assert response.status_code == 409
    assert response.json()["code"] == "TIME_LIMIT"


def test_failed_asr_can_be_manually_confirmed_without_losing_original(admin, app):
    p, sid = ready(admin, app)
    tid = p.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()["turn_id"]
    _, item = upload(p, tid, 0, wav_bytes())
    jid = p.post(f"/api/participant/turns/{tid}/finalize", json={"chunks": [item]}, headers=headers()).json()[
        "job_id"
    ]
    from app.providers import ProviderError

    class FailingASR:
        async def transcribe(self, *args):
            raise ProviderError("TIMEOUT", "虚构超时", True)

    asyncio.run(Worker(app.state.db, app.state.settings, provider=FailingASR()).run_once())
    response = p.post(
        f"/api/participant/turns/{tid}/confirm", json={"text": "手动录入的虚构回答"}, headers=headers()
    )
    assert response.status_code == 200, response.text
    with app.state.db.read() as db:
        assert db.get(Turn, tid).confirmed
        assert db.get(Turn, tid).audio_asset_id is not None
        assert db.get(Job, jid).status == "cancelled"
        assert db.get(InterviewSession, sid).status == "paused"
    for format in ("csv", "markdown"):
        exported = admin.get(f"/api/admin/sessions/{sid}/export?format={format}").text
        assert "识别失败后受访者手动输入" in exported


def test_unreadable_recording_is_archived_before_asr_failure(admin, app):
    p, sid = ready(admin, app)
    tid = p.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()["turn_id"]
    content = b"fictional invalid recording container"
    _, item = upload(p, tid, 0, content)
    jid = p.post(f"/api/participant/turns/{tid}/finalize", json={"chunks": [item]}, headers=headers()).json()[
        "job_id"
    ]
    asyncio.run(Worker(app.state.db, app.state.settings).run_once())
    with app.state.db.read() as db:
        assert db.get(Job, jid).status == "failed"
        turn = db.get(Turn, tid)
        asset = db.get(Asset, turn.audio_asset_id) if turn.audio_asset_id else None
        assert asset is not None
        assert (app.state.settings.data_dir / asset.path).read_bytes() == content
        assert asset.duration_seconds is None
    row = p.get("/api/participant/session").json()["turns"][-1]
    assert row["audio_status"] == "unplayable"
