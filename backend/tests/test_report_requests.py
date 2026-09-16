import asyncio

import pytest
from sqlalchemy import select

from app.models import Job, Ledger
from app.providers import ProviderError
from app.worker import Worker
from test_audio_flow import ready
from test_core import headers
from test_report_flow import finish_answer


@pytest.mark.parametrize("action", ["new", "retry"])
def test_unknown_report_requires_persisted_charge_confirmation_and_idempotency(admin, app, action):
    participant, sid = ready(admin, app, "text")
    _, jid = finish_answer(participant)

    class TimeoutProvider:
        async def text(self, *args, **kwargs):
            raise ProviderError("EXTERNAL_STATUS_UNKNOWN", "虚构超时", True)

    asyncio.run(Worker(app.state.db, app.state.settings, provider=TimeoutProvider()).run_once())
    with app.state.db.read() as db:
        job = db.get(Job, jid)
        assert job.status == "external_status_unknown"
        snapshot = job.payload["_report_snapshot"]
        count = len(list(db.scalars(select(Job).where(Job.session_id == sid))))
    body = {"retry_job_id": jid} if action == "retry" else {}
    path = f"/api/admin/sessions/{sid}/reports"
    refused = admin.post(path, json=body, headers=headers())
    assert refused.status_code == 409 and refused.json()["code"] == "CHARGE_CONFIRMATION_REQUIRED"
    with app.state.db.read() as db:
        assert len(list(db.scalars(select(Job).where(Job.session_id == sid)))) == count
        assert db.get(Job, jid).status == "external_status_unknown"
    key = headers()
    accepted = {**body, "accept_possible_charge": True}
    result = admin.post(path, json=accepted, headers=key)
    assert result.status_code == 200
    assert admin.post(path, json=accepted, headers=key).json() == result.json()
    if action == "retry":
        assert result.json()["job_id"] == jid
    else:
        assert result.json()["job_id"] != jid
    with app.state.db.read() as db:
        assert db.get(Job, jid).payload["_report_snapshot"] == snapshot
        assert db.scalar(select(Ledger).where(Ledger.job_id == jid)).status == "external_status_unknown"
        assert len(list(db.scalars(select(Job).where(Job.session_id == sid)))) == count + (action == "new")
    duplicate = admin.post(path, json={"accept_possible_charge": True}, headers=headers())
    assert duplicate.status_code == 409 and duplicate.json()["code"] == "REPORT_BUSY"


def test_admin_retry_is_scoped_to_report_session_and_current_consent(admin, app):
    participant, sid = ready(admin, app, "text")
    _, jid = finish_answer(participant)
    other, other_sid = ready(admin, app, "text")
    _, other_jid = finish_answer(other)
    with app.state.db.transaction() as db:
        db.get(Job, jid).status = "failed"
        db.get(Job, other_jid).status = "failed"
    path = f"/api/admin/sessions/{sid}/reports"
    assert participant.post(path, json={"retry_job_id": jid}, headers=headers()).status_code == 401
    assert admin.post(path, json={"retry_job_id": other_jid}, headers=headers()).status_code == 409
    with app.state.db.read() as db:
        assert db.get(Job, other_jid).status == "failed"
        unrelated = db.scalar(select(Job).where(Job.session_id == sid, Job.kind != "report"))
        unrelated_id = unrelated.id
    assert admin.post(path, json={"retry_job_id": unrelated_id}, headers=headers()).status_code == 409
    app.state.settings.report_model = "new-report-model"
    assert admin.post(path, json={"retry_job_id": jid}, headers=headers()).status_code == 403
    assert admin.get(f"/api/admin/sessions/{other_sid}/export").status_code == 200
