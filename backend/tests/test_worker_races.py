import asyncio

import pytest
from sqlalchemy import select

from app.errors import AppError
from app.models import InterviewSession, Job, Ledger, Reservation
from app.providers import ProviderError, TextResult, Usage
from app.storage import Storage
from app.worker import Worker


def test_restart_marks_deleted_inflight_reservation_unknown(database):
    from app.billing import reserve

    with database.transaction() as db:
        job = db.get(Job, "j0")
        job.status, job.call_started, job.attempt = "running", True, 2
        reserve(db, database.settings, job, 400000)
    Storage(database, database.settings).delete_session("s0")
    Worker(database, database.settings).recover(startup=True)
    with database.read() as db:
        reservation = db.scalar(select(Reservation).where(Reservation.job_id == "j0"))
        ledger = db.scalar(select(Ledger).where(Ledger.job_id == "j0"))
        assert reservation.state == "unknown"
        assert ledger.amount_micro is None and ledger.attempt == 2
        assert db.get(Job, "j0") is None


@pytest.mark.parametrize("outcome", ["success", "unknown", "failed"])
def test_withdraw_during_paid_call_settles_bill_without_restoring_content(database, outcome):
    with database.transaction() as db:
        session = db.get(InterviewSession, "s0")
        session.processing_consent = session.permanent_consent = True
        session.consent_version = database.settings.consent_version
        session.status = "in_progress"
        job = db.get(Job, "j0")
        job.status, job.lease_token, job.attempt = "running", "lease", 1
    worker = Worker(database, database.settings)

    async def call():
        Storage(database, database.settings).delete_session("s0", withdrawn=True)
        if outcome != "success":
            raise ProviderError("TEST_ERROR", "虚构服务错误", outcome == "unknown")
        return TextResult("已经撤回的内容不得保存", Usage(input_tokens=10, output_tokens=10, source="actual"))

    async def run():
        with pytest.raises((AppError, ProviderError)):
            await worker._paid("j0", "lease", "decide", "interview", {"input_tokens": 10}, call)

    asyncio.run(run())
    with database.read() as db:
        assert db.get(InterviewSession, "s0") is None
        assert db.get(Job, "j0") is None
        reservation = db.scalar(select(Reservation).where(Reservation.job_id == "j0"))
        entry = db.scalar(select(Ledger).where(Ledger.job_id == "j0"))
        assert entry is not None
        assert (
            reservation.state == {"success": "settled", "unknown": "unknown", "failed": "released"}[outcome]
        )
        assert entry.amount_micro is None if outcome == "unknown" else entry.amount_micro == 0
        assert "已经撤回" not in str(entry.usage)
