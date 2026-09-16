"""Runtime configuration cannot rewrite the origin of permanent archives."""

from sqlalchemy import select

from app.models import Report, Revision
from test_core import headers
from test_interview_flow import consent_and_start, drain


def completed_mock_archive(admin, app):
    participant, sid, _ = consent_and_start(admin, app)
    response = participant.post(
        "/api/participant/turns",
        json={"input_mode": "text", "text": "虚构案例：我提交了一次资料。"},
        headers=headers(),
    )
    tid = response.json()["turn_id"]
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/confirm",
            json={"text": "虚构案例：我提交了一次资料。"},
            headers=headers(),
        ).status_code
        == 200
    )
    assert (
        participant.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code
        == 200
    )
    drain(app)
    return sid, tid


def test_mock_archives_keep_origin_after_switching_to_live(admin, app):
    sid, tid = completed_mock_archive(admin, app)
    old_model = app.state.settings.report_model
    app.state.settings.mode = "live"
    app.state.settings.report_model = "another-model"
    detail = admin.get(f"/api/admin/sessions/{sid}").json()
    assert detail["mode"] == "live"  # Current consent/processing configuration.
    assert detail["archive_mode"] == "mock"
    report = detail["reports"][0]
    assert report["archive_mode"] == "mock"
    assert report["provenance"]["calls"][0]["model"] == old_model
    assert all(r["provenance"]["mode"] == "mock" for t in detail["turns"] for r in t["revisions"])
    exported = admin.get(f"/api/admin/sessions/{sid}/export").json()
    assert exported["mode"] == "mock"
    assert exported["runtime_mode"] == "live"
    assert exported["label"].startswith("模拟")
    assert "providers" not in exported  # Historical providers live in immutable provenance.
    for format in ("markdown", "csv"):
        text = admin.get(f"/api/admin/sessions/{sid}/export?format={format}").text
        assert "模拟" in text and "真实访谈记录" not in text
    response = admin.post(
        f"/api/admin/turns/{tid}/revision", json={"text": "研究者补充勘误。"}, headers=headers()
    )
    assert response.status_code == 200
    changed = admin.get(f"/api/admin/sessions/{sid}").json()
    assert changed["archive_mode"] == "mixed"
    turn = next(t for t in changed["turns"] if t["id"] == tid)
    assert turn["revisions"][-1]["provenance"]["source_modes"] == ["mock"]
    assert changed["reports"][0]["archive_mode"] == "mock"


def test_missing_legacy_provenance_is_not_assumed_live(admin, app):
    sid, _ = completed_mock_archive(admin, app)
    with app.state.db.transaction() as db:
        for revision in db.scalars(select(Revision)):
            revision.provenance = {}
        for report in db.scalars(select(Report)):
            report.provenance = {}
    app.state.settings.mode = "live"
    exported = admin.get(f"/api/admin/sessions/{sid}/export").json()
    assert exported["mode"] == "unknown"
    assert "来源未确认" in exported["label"]
    assert exported["reports"][0]["archive_mode"] == "unknown"


def test_recording_and_asr_provenance_survive_mode_switch(admin, app):
    from test_audio_flow import ready, upload, wav_bytes

    participant, sid = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    _, manifest = upload(participant, tid, 0, wav_bytes())
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers()
        ).status_code
        == 200
    )
    drain(app)
    app.state.settings.mode = "live"
    detail = admin.get(f"/api/admin/sessions/{sid}").json()
    turn = next(t for t in detail["turns"] if t["id"] == tid)
    assert turn["audio_provenance"]["mode"] == "mock"
    assert turn["provenance"]["calls"][0]["role"] == "asr"
    assert turn["provenance"]["calls"][0]["mode"] == "mock"


def test_reused_checkpoint_keeps_original_mode_and_provider(database):
    import asyncio
    from app.models import InterviewSession, Job
    from app.providers import TextResult, Usage
    from app.provenance import job_provenance
    from app.worker import Worker

    with database.transaction() as db:
        session = db.get(InterviewSession, "s0")
        session.processing_consent = session.permanent_consent = True
        session.consent_version = database.settings.consent_version
        session.status = "in_progress"
        job = db.get(Job, "j0")
        job.status, job.lease_token, job.attempt = "running", "lease", 1
    worker = Worker(database, database.settings)
    calls = []

    async def call():
        calls.append(True)
        return TextResult("虚构检查点", Usage(input_tokens=10, output_tokens=10, source="actual"))

    asyncio.run(worker._paid("j0", "lease", "decide", "interview", {}, call))
    database.settings.mode = "live"
    database.settings.interview_model = "another-model"
    with database.transaction() as db:
        db.get(InterviewSession, "s0").consent_version = database.settings.consent_version
    result = asyncio.run(worker._paid("j0", "lease", "decide", "interview", {}, call))
    assert result.text == "虚构检查点" and len(calls) == 1
    with database.read() as db:
        provenance = job_provenance(db.get(Job, "j0"), "decide")
        assert provenance["mode"] == "mock"
        assert provenance["calls"][0]["model"] == "qwen-plus"
