import asyncio
import json

import pytest
from sqlalchemy import select

from app.models import Job, Ledger, Report
from app.providers import TextResult, Usage
from app.worker import Worker
from test_audio_flow import ready
from test_core import headers
from test_interview_flow import drain


def finish_answer(participant, text="我在上周提交过一份虚构材料。"):
    tid = participant.post(
        "/api/participant/turns", json={"input_mode": "text", "text": text}, headers=headers()
    ).json()["turn_id"]
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/confirm", json={"text": text}, headers=headers()
        ).status_code
        == 200
    )
    ended = participant.post("/api/participant/control", json={"action": "end"}, headers=headers())
    assert ended.status_code == 200
    return tid, ended.json()["job_id"]


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_report_repairs_once_and_accounts_for_both_calls_without_publishing_invalid_content(
    admin, app, repair_succeeds
):
    from app.interview import mock_response

    participant, sid = ready(admin, app, "text")
    tid, jid = finish_answer(participant)

    class Provider:
        calls = 0

        async def text(self, role, messages, **kwargs):
            assert role == "report"
            self.calls += 1
            payload = json.loads(messages[-1]["content"])
            value = mock_response(payload)
            if self.calls == 1 or not repair_succeeds:
                value["findings"][0]["citations"][0]["quote"] = "伪造引文必须阻止"
            return TextResult(json.dumps(value, ensure_ascii=False), Usage(source="mock"))

    provider = Provider()
    asyncio.run(Worker(app.state.db, app.state.settings, provider=provider).run_once())
    assert provider.calls == 2
    with app.state.db.read() as db:
        job = db.get(Job, jid)
        assert job.status == ("succeeded" if repair_succeeds else "failed")
        assert len(list(db.scalars(select(Ledger).where(Ledger.job_id == jid)))) == 2
        assert len(list(db.scalars(select(Report).where(Report.session_id == sid)))) == int(repair_succeeds)
        if not repair_succeeds:
            assert job.error_code == "REPORT_INVALID"
    data = admin.get(f"/api/admin/sessions/{sid}").json()
    assert data["session"]["status"] == "completed"
    assert next(t for t in data["turns"] if t["id"] == tid)["text"] == "我在上周提交过一份虚构材料。"
    assert admin.get(f"/api/admin/sessions/{sid}/export").status_code == 200


def test_skip_then_immediate_end_is_retained_in_coverage_and_report(admin, app):
    participant, sid = ready(admin, app, "text")
    assert (
        participant.post("/api/participant/control", json={"action": "skip"}, headers=headers()).status_code
        == 200
    )
    assert (
        participant.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code
        == 200
    )
    drain(app)
    detail = admin.get(f"/api/admin/sessions/{sid}").json()
    topics = {t["id"]: t for t in detail["coverage"]["topics"]}
    assert topics["T1"]["status"] == "skipped"
    assert topics["T2"]["status"] == "not_started"
    report = detail["reports"][0]
    assert any("跳过" in text and "经历" in text for text in report["body"]["unanswered"])
    assert any("未谈及" in text and "改善" in text for text in report["body"]["unanswered"])
    assert report["body"]["background"]["objective"] == "了解真实流程经历"
    assert "提前结束" in report["markdown"]
    assert report["markdown"].index("## 研究局限") < report["markdown"].index("## 研究背景")
    for section in (
        "受访者自述角色",
        "已讨论范围",
        "具体事件",
        "主要陈述",
        "受访者对原因的解释",
        "待验证假设",
        "受访者建议",
        "未回答／拒绝回答事项",
    ):
        assert "## " + section in report["markdown"]
    assert "coverage" not in participant.get("/api/participant/session").json()


def test_manual_retry_replaces_invalid_report_results_and_keeps_frozen_sources(admin, app):
    from app.interview import mock_response

    participant, sid = ready(admin, app, "text")
    tid, jid = finish_answer(participant)

    class Provider:
        calls = 0

        async def text(self, role, messages, **kwargs):
            self.calls += 1
            value = mock_response(json.loads(messages[-1]["content"]))
            if self.calls <= 2:
                value["findings"][0]["citations"][0]["quote"] = "第一次生成及修复均无效"
            return TextResult(json.dumps(value, ensure_ascii=False), Usage(source="mock"))

    provider = Provider()
    worker = Worker(app.state.db, app.state.settings, provider=provider)
    asyncio.run(worker.run_once())
    with app.state.db.read() as db:
        assert db.get(Job, jid).error_code == "REPORT_INVALID"
    admin.post(f"/api/admin/turns/{tid}/revision", json={"text": "后来勘误"}, headers=headers())
    assert (
        participant.post(
            "/api/participant/control", json={"action": "retry", "job_id": jid}, headers=headers()
        ).status_code
        == 200
    )
    asyncio.run(worker.run_once())
    assert provider.calls == 3
    report = admin.get(f"/api/admin/sessions/{sid}").json()["reports"][0]
    assert report["source_updated"] is True
    assert report["citations"][0]["quote"] == "我在上周提交过一份虚构材料。"
    with app.state.db.read() as db:
        assert len(list(db.scalars(select(Ledger).where(Ledger.job_id == jid)))) == 3


def test_report_marks_pending_transcript_and_early_end_on_first_page(admin, app):
    participant, sid = ready(admin, app, "text")
    participant.post(
        "/api/participant/turns", json={"input_mode": "text", "text": "待确认的虚构输入"}, headers=headers()
    )
    participant.post("/api/participant/control", json={"action": "end"}, headers=headers())
    drain(app)
    report = admin.get(f"/api/admin/sessions/{sid}").json()["reports"][0]
    assert report["citations"] == []
    limits = "\n".join(report["body"]["limitations"])
    assert "尚未确认" in limits and "提前结束" in limits and "文字输入" in limits
    assert "尚缺乏依据" in report["markdown"]


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_report_discloses_real_audio_failure_and_keeps_confirmed_text(admin, app, damage):
    from app.models import Asset
    from test_audio_flow import upload, wav_bytes

    participant, sid = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    _, manifest = upload(participant, tid, 0, wav_bytes())
    participant.post(f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers())
    drain(app)
    participant.post(
        f"/api/participant/turns/{tid}/confirm", json={"text": "虚构的确认文字仍须保留。"}, headers=headers()
    )
    with app.state.db.read() as db:
        asset = db.scalar(select(Asset).where(Asset.turn_id == tid))
        path = app.state.settings.data_dir / asset.path
    if damage == "missing":
        path.unlink()
    else:
        raw = path.read_bytes()
        path.write_bytes(raw[:-1] + bytes([raw[-1] ^ 1]))
    participant.post("/api/participant/control", json={"action": "end"}, headers=headers())
    drain(app)
    report = admin.get(f"/api/admin/sessions/{sid}").json()["reports"][0]
    assert ("档案文件缺失" if damage == "missing" else "哈希不匹配") in report["markdown"]
    assert report["citations"][0]["quote"] == "虚构的确认文字仍须保留。"
    assert "到期" not in report["markdown"]


def test_long_report_reads_every_source_and_aggregates_with_frozen_retry_snapshot(admin, app):
    from app.domain import add_text_revision, new_turn
    from app.interview import mock_response
    from app.models import InterviewSession
    from app.providers import ProviderError

    participant, sid = ready(admin, app, "text")
    turn_ids = []
    with app.state.db.transaction() as db:
        session = db.get(InterviewSession, sid)
        for index in range(3):
            turn = new_turn(db, session, "participant", "text", "confirmed", confirmed=True, topic_id="T1")
            add_text_revision(
                db, turn, f"虚构片段{index}。" + "长回答" * 5000, "participant_confirmed", "participant"
            )
            turn_ids.append(turn.id)
            session.revision += 1
    jid = participant.post("/api/participant/control", json={"action": "end"}, headers=headers()).json()[
        "job_id"
    ]

    class Provider:
        tasks = []
        source_ids = []
        interrupted = False

        async def text(self, role, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            self.tasks.append(payload["task"])
            if payload["task"] == "report":
                self.source_ids.extend(t["id"] for t in payload["turns"] if t["role"] == "participant")
            elif not self.interrupted:
                self.interrupted = True
                raise ProviderError("EXTERNAL_STATUS_UNKNOWN", "虚构汇总超时", True)
            return TextResult(json.dumps(mock_response(payload), ensure_ascii=False), Usage(source="mock"))

    provider = Provider()
    worker = Worker(app.state.db, app.state.settings, provider=provider)
    asyncio.run(worker.run_once())
    with app.state.db.read() as db:
        assert db.get(Job, jid).status == "external_status_unknown"
    assert provider.source_ids == turn_ids
    assert (
        admin.post(
            f"/api/admin/turns/{turn_ids[0]}/revision", json={"text": "事后更新的来源"}, headers=headers()
        ).status_code
        == 200
    )
    assert (
        participant.post(
            "/api/participant/control",
            json={"action": "retry", "job_id": jid, "accept_possible_charge": True},
            headers=headers(),
        ).status_code
        == 200
    )
    asyncio.run(worker.run_once())
    report = admin.get(f"/api/admin/sessions/{sid}").json()["reports"][0]
    assert report["source_updated"] is True
    assert {c["turn_id"] for c in report["citations"]} == set(turn_ids)
    assert all("事后更新" not in c["quote"] for c in report["citations"])
    assert provider.tasks.count("report") == 3  # Completed extraction checkpoints are not paid twice.
    assert provider.tasks.count("report_merge") == 3  # One unknown attempt, then two pairwise merges.
    with app.state.db.read() as db:
        assert len(list(db.scalars(select(Ledger).where(Ledger.job_id == jid)))) == 6
