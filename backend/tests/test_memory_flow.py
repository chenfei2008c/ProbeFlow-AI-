import asyncio
import json

from sqlalchemy import select

from app.domain import add_text_revision
from app.interview import mock_response
from app.models import InterviewSession, Job, Memory, Turn
from app.providers import TextResult, Usage
from app.worker import Worker
from test_audio_flow import ready
from test_core import headers


def test_late_decision_cannot_overwrite_newer_correction(admin, app):
    p, sid = ready(admin, app, "text")
    tid = p.post(
        "/api/participant/turns", json={"input_mode": "text", "text": "上次我做了甲事项。"}, headers=headers()
    ).json()["turn_id"]
    p.post(f"/api/participant/turns/{tid}/confirm", json={"text": "上次我做了甲事项。"}, headers=headers())

    class SlowProvider:
        async def text(self, role, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            result = mock_response(payload)
            with app.state.db.transaction() as db:
                turn = db.get(Turn, tid)
                add_text_revision(db, turn, "更正：我做的是乙事项。", "researcher_correction", "admin")
                db.get(InterviewSession, sid).revision += 1
            return TextResult(json.dumps(result, ensure_ascii=False), Usage(source="mock"))

    asyncio.run(Worker(app.state.db, app.state.settings, provider=SlowProvider()).run_once())
    with app.state.db.read() as db:
        assert (
            len(list(db.scalars(select(Turn).where(Turn.session_id == sid, Turn.role == "assistant")))) == 1
        )
        assert db.scalar(select(Memory).where(Memory.session_id == sid)) is None
        assert (
            db.scalar(select(Job).where(Job.session_id == sid, Job.kind == "decide", Job.status == "queued"))
            is not None
        )


def test_late_summary_does_not_overwrite_newer_memory(admin, app):
    _, sid = ready(admin, app, "text")
    with app.state.db.transaction() as db:
        session = db.get(InterviewSession, sid)
        session.memory_version = 1
        db.add(Memory(session_id=sid, version=1, through_seq=1, content={"evidence": []}))
        job = Job(
            session_id=sid,
            kind="summary",
            status="running",
            lease_token="lease",
            dedup_key="summary-race",
            payload={"source_revision": session.revision, "memory_version": 1, "through_seq": 1},
        )
        db.add(job)
        db.flush()
        job_id = job.id

    async def run():
        task = asyncio.create_task(Worker(app.state.db, app.state.settings)._summary(job_id, "lease"))
        await asyncio.sleep(0)
        with app.state.db.transaction() as db:
            session = db.get(InterviewSession, sid)
            session.revision += 1
            session.memory_version = 2
            db.add(Memory(session_id=sid, version=2, through_seq=2, content={"evidence": ["newer"]}))
        await task

    asyncio.run(run())
    with app.state.db.read() as db:
        assert db.get(InterviewSession, sid).memory_version == 2
        assert db.get(Job, job_id).result["applied"] is False
        assert len(list(db.scalars(select(Memory).where(Memory.session_id == sid)))) == 2


def test_oversized_required_context_pauses_without_external_request_or_losing_answer(admin, app):
    participant, sid = ready(admin, app, "text")
    original = "旧轮次完整内容" * 4000
    with app.state.db.transaction() as db:
        turn = db.scalar(select(Turn).where(Turn.session_id == sid))
        add_text_revision(db, turn, original, "model", "system")
        db.add(Memory(session_id=sid, version=1, through_seq=0, content={"through_seq": 0}))
    tid = participant.post(
        "/api/participant/turns", json={"input_mode": "text", "text": "本轮必须保留。"}, headers=headers()
    ).json()["turn_id"]
    participant.post(
        f"/api/participant/turns/{tid}/confirm", json={"text": "本轮必须保留。"}, headers=headers()
    )

    class NoCallProvider:
        async def text(self, *args, **kwargs):
            raise AssertionError("oversized context must not be sent")

    asyncio.run(Worker(app.state.db, app.state.settings, provider=NoCallProvider()).run_once())
    result = participant.get("/api/participant/session").json()
    assert result["session"]["status"] == "paused"
    assert result["jobs"][-1]["error_code"] == "CONTEXT_TOO_LARGE"
    assert result["turns"][0]["text"] == original
    assert result["turns"][-1]["text"] == "本轮必须保留。"
