import asyncio

from sqlalchemy import select

from app.models import Consent, InterviewSession, Job
from app.worker import Worker
from test_core import headers, join, new_study


def test_provider_or_mode_change_requires_new_consent_before_any_external_call(admin, app):
    participant, sid, _ = join(admin, app, new_study(admin))
    original_version = participant.get("/api/config").json()["consent_version"]
    consent = {"version": original_version, "mode": "text", "processing": True, "permanent": True}
    assert participant.post("/api/participant/consent", json=consent, headers=headers()).status_code == 200
    app.state.settings.mode = "live"
    new_version = participant.get("/api/config").json()["consent_version"]
    assert new_version != original_version
    assert (
        participant.post(
            "/api/participant/turns", json={"input_mode": "text", "text": "尚未授权外发。"}, headers=headers()
        ).status_code
        == 403
    )

    class ForbiddenProvider:
        calls = 0

        async def text(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("No external processing before renewed consent")

    provider = ForbiddenProvider()
    asyncio.run(Worker(app.state.db, app.state.settings, provider=provider).run_once())
    assert provider.calls == 0
    with app.state.db.read() as db:
        assert db.scalar(select(Job).where(Job.session_id == sid)).error_code == "CONSENT_REQUIRED"
    assert participant.post("/api/participant/consent", json=consent, headers=headers()).status_code == 403
    renewed = {**consent, "version": new_version}
    assert participant.post("/api/participant/consent", json=renewed, headers=headers()).status_code == 200
    with app.state.db.read() as db:
        records = list(
            db.scalars(select(Consent).where(Consent.session_id == sid).order_by(Consent.created_at))
        )
        assert len(records) == 2
        assert records[0].snapshot["mode"] == "mock"
        assert records[1].snapshot["mode"] == "live"
        assert records[1].snapshot["retention"] == "permanent"
        assert db.get(InterviewSession, sid).consent_snapshot == records[1].snapshot
        assert db.scalar(select(Job).where(Job.session_id == sid)).status == "queued"
    app.state.settings.interview_base_url = "https://another-recipient.invalid/v1"
    assert participant.get("/api/config").json()["consent_version"] != new_version


def test_completed_session_can_renew_processing_consent_without_reopening_interview(admin, app):
    from test_interview_flow import consent_and_start, drain

    participant, sid, _ = consent_and_start(admin, app)
    assert (
        participant.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code
        == 200
    )
    drain(app)
    before = admin.get(f"/api/admin/sessions/{sid}").json()
    app.state.settings.report_base_url = "https://changed-recipient.invalid/v1"
    assert admin.post(f"/api/admin/sessions/{sid}/reports", headers=headers()).status_code == 403
    assert admin.get(f"/api/admin/sessions/{sid}/export").status_code == 200
    version = participant.get("/api/config").json()["consent_version"]
    response = participant.post(
        "/api/participant/consent",
        json={"version": version, "mode": "text", "processing": True, "permanent": True},
        headers=headers(),
    )
    assert response.status_code == 200, response.text
    assert response.json().get("job_id") is None
    after = admin.get(f"/api/admin/sessions/{sid}").json()
    assert after["session"]["status"] == "completed"
    assert after["turns"] == before["turns"] and after["reports"] == before["reports"]
    assert len(after["jobs"]) == len(before["jobs"])
    assert (
        participant.post(
            "/api/participant/turns", json={"input_mode": "text", "text": "不能重新开场"}, headers=headers()
        ).status_code
        == 409
    )
    assert admin.post(f"/api/admin/sessions/{sid}/reports", headers=headers()).status_code == 200
    drain(app)
    assert len(admin.get(f"/api/admin/sessions/{sid}").json()["reports"]) == 2


def test_mode_upgrade_requires_consent_and_exports_original_history(admin, app):
    from app.models import StudyVersion
    from test_core import STUDY
    from test_interview_flow import consent_and_start

    participant, sid, study = consent_and_start(admin, app)
    original_snapshot = app.state.settings.consent_snapshot()
    version = participant.get("/api/config").json()["consent_version"]
    before = admin.get(f"/api/admin/sessions/{sid}").json()
    voice = {"input_mode": "voice", "mime_type": "audio/wav"}
    assert participant.post("/api/participant/turns", json=voice, headers=headers()).status_code == 403
    for processing, permanent, notice in [
        (True, False, version),
        (False, True, version),
        (True, True, "stale"),
    ]:
        assert (
            participant.post(
                "/api/participant/consent",
                json={"version": notice, "mode": "voice", "processing": processing, "permanent": permanent},
                headers=headers(),
            ).status_code
            == 403
        )
    unchanged = admin.get(f"/api/admin/sessions/{sid}").json()
    assert unchanged["turns"] == before["turns"] and unchanged["jobs"] == before["jobs"]
    assert unchanged["session"]["mode"] == "text"
    assert (
        participant.post(
            "/api/participant/consent",
            json={"version": version, "mode": "voice", "processing": True, "permanent": True},
            headers=headers(),
        ).status_code
        == 200
    )
    assert participant.post("/api/participant/turns", json=voice, headers=headers()).status_code == 200
    app.state.settings.report_base_url = "https://future-recipient.invalid/v1"
    admin.post(
        f"/api/admin/studies/{study['id']}/versions",
        json={**STUDY, "objective": "后来的研究目标"},
        headers=headers(),
    )
    with app.state.db.transaction() as db:
        db.get(StudyVersion, study["current_version_id"]).prompt_version = "historical-fixture"
    data = admin.get(f"/api/admin/sessions/{sid}").json()
    records = data["consents"]
    assert [record["mode"] for record in records] == ["text", "voice"]
    assert all(record["version"] == version and record["created_at"] for record in records)
    assert all(record["processing"] and record["permanent"] for record in records)
    assert all(
        record["retention"] == "permanent" and record["snapshot"] == original_snapshot for record in records
    )
    assert data["session"]["expires_at"] is None
    assert data["study_version"]["id"] == study["current_version_id"]
    assert data["study_version"]["number"] == 1
    assert data["study"]["objective"] == STUDY["objective"]
    exported = admin.get(f"/api/admin/sessions/{sid}/export").json()
    assert exported["consents"] == records
    assert exported["study_version"] == data["study_version"]
    assert exported["prompt_version"] == "historical-fixture"
    assert "future-recipient.invalid" not in str(exported["consents"])
    assert "jobs" not in exported and "providers" not in exported
    assert "consents" not in participant.get("/api/participant/session").json()
