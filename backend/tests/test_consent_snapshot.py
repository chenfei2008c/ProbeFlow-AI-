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
