from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Database
from app.errors import AppError
from app.models import InterviewSession, Job, Study, StudyVersion


@pytest.fixture
def database(tmp_path):
    database = Database(
        Settings(data_dir=tmp_path / "budget", monthly_limit_cny="1.00", worker_enabled=False)
    )
    database.initialize()
    with database.transaction() as db:
        study = Study(title="预算")
        db.add(study)
        db.flush()
        version = StudyVersion(study_id=study.id, number=1, config={})
        db.add(version)
        db.flush()
        for i in range(2):
            session = InterviewSession(
                id=f"s{i}",
                study_id=study.id,
                study_version_id=version.id,
                participant_code=f"P{i}",
                budget_micro=1_000_000,
            )
            db.add(session)
            db.flush()
            db.add(Job(id=f"j{i}", session_id=session.id, kind="decide", dedup_key=f"job{i}", payload={}))
    return database


def test_two_sessions_cannot_reserve_same_monthly_funds(database):
    from app.billing import reserve

    def attempt(i):
        try:
            with database.transaction() as db:
                reserve(db, database.settings, db.get(Job, f"j{i}"), 700_000)
            return "held"
        except AppError as exc:
            return exc.code

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(attempt, [0, 1]))
    assert sorted(outcomes) == ["BUDGET_EXCEEDED", "held"]


def test_unknown_bill_keeps_reservation_and_not_zero_cost(database):
    from app.billing import reserve, settle
    from app.models import Ledger, Reservation

    with database.transaction() as db:
        job = db.get(Job, "j0")
        reservation = reserve(db, database.settings, job, 700_000)
        settle(db, database.settings, job, reservation, "interview", {}, "external_status_unknown")
    with database.read() as db:
        reservation = db.scalar(select(Reservation))
        ledger = db.scalar(select(Ledger))
        assert reservation.state == "unknown"
        assert ledger.amount_micro is None
        assert ledger.amount_source == "unknown"
        assert db.get(InterviewSession, "s0").reserved_micro == 700_000


def test_money_units_and_chinese_tts_characters():
    from app.billing import calculate, billable_characters

    assert billable_characters("你好AI") == 6
    assert calculate("tts", {"characters": 10000}) == 800_000
    assert calculate("asr", {"audio_seconds": 2700}) == 594_000
    assert calculate("interview", {"input_tokens": 400000, "output_tokens": 30000}) == 380_000
