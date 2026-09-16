import asyncio

from sqlalchemy import select

from test_core import headers, join, new_study


def drain(app):
    from app.worker import Worker

    worker = Worker(app.state.db, app.state.settings)

    async def run():
        for _ in range(30):
            if not await worker.run_once():
                return
        raise AssertionError("jobs did not settle")

    asyncio.run(run())


def consent_and_start(admin, app):
    study = new_study(admin)
    p, sid, _ = join(admin, app, study)
    version = p.get("/api/config").json()["consent_version"]
    assert (
        p.post(
            "/api/participant/consent",
            json={"version": version, "mode": "text", "processing": True, "permanent": True},
            headers=headers(),
        ).status_code
        == 200
    )
    drain(app)
    return p, sid, study


def test_complete_text_interview_retains_versions_reports_and_exports(admin, app):
    p, sid, study = consent_and_start(admin, app)
    d = p.get("/api/participant/session").json()
    assert d["turns"][0]["role"] == "assistant"
    key = headers()
    answer = {"input_mode": "text", "text": "上周一我准备了资料，后来补充一次，第二天办完。"}
    r = p.post("/api/participant/turns", json=answer, headers=key)
    assert r.status_code == 200, r.text
    turn_id = r.json()["turn_id"]
    assert p.post("/api/participant/turns", json=answer, headers=key).json()["turn_id"] == turn_id
    before = p.get("/api/participant/session").json()
    assert len(before["turns"]) == 2
    assert before["turns"][-1]["status"] == "confirming"
    assert (
        p.post(
            f"/api/participant/turns/{turn_id}/confirm",
            json={"text": "上周一我准备资料，只补充了一次，第二天办完。"},
            headers=headers(),
        ).status_code
        == 200
    )
    drain(app)
    after = p.get("/api/participant/session").json()
    assert len(after["turns"]) == 3
    assert len(after["turns"][1]["revisions"]) == 2
    assert after["turns"][1]["confirmed"] is True
    assert p.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code == 200
    drain(app)
    final = admin.get(f"/api/admin/sessions/{sid}").json()
    assert final["session"]["status"] == "completed"
    assert final["reports"] and final["reports"][0]["citations"]
    for format in ("json", "csv", "markdown"):
        r = admin.get(f"/api/admin/sessions/{sid}/export?format={format}")
        assert r.status_code == 200, r.text
        assert "模拟" in r.text or "mock" in r.text
    assert (
        admin.post(
            f"/api/admin/turns/{turn_id}/revision",
            json={"text": "事后核实：只补充了一次。"},
            headers=headers(),
        ).status_code
        == 200
    )
    revised = admin.get(f"/api/admin/sessions/{sid}").json()
    assert revised["reports"][0]["source_updated"] is True
    assert len(revised["turns"][1]["revisions"]) == 3


def test_foreign_turn_confirmation_and_media_are_rejected(admin, app):
    p, sid, study = consent_and_start(admin, app)
    q, qid, _ = join(admin, app, study)
    r = p.post(
        "/api/participant/turns", json={"input_mode": "text", "text": "我有一个例子。"}, headers=headers()
    )
    tid = r.json()["turn_id"]
    r = q.post(f"/api/participant/turns/{tid}/confirm", json={"text": "注入别人的记录"}, headers=headers())
    assert r.status_code in (403, 404)
    assert q.get("/api/media/nonexistent").status_code == 404


def test_end_keeps_data_but_withdraw_deletes_all_and_revokes_credentials(admin, app):
    from app.models import Turn, Report

    p, sid, _ = consent_and_start(admin, app)
    assert p.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code == 200
    drain(app)
    assert admin.get(f"/api/admin/sessions/{sid}").status_code == 200
    assert (
        p.post("/api/participant/control", json={"action": "withdraw"}, headers=headers()).status_code == 200
    )
    assert p.get("/api/participant/session").status_code == 401
    assert admin.get(f"/api/admin/sessions/{sid}").status_code == 404
    with app.state.db.read() as db:
        assert not list(db.scalars(select(Turn).where(Turn.session_id == sid)))
        assert not list(db.scalars(select(Report).where(Report.session_id == sid)))


def test_skipping_does_not_fabricate_participant_answer(admin, app):
    p, sid, _ = consent_and_start(admin, app)
    assert p.post("/api/participant/control", json={"action": "skip"}, headers=headers()).status_code == 200
    drain(app)
    d = p.get("/api/participant/session").json()
    assert not [t for t in d["turns"] if t["role"] == "participant" and t["confirmed"]]


def test_study_deletion_removes_outline_jobs_and_filters_old_backups(admin, app, tmp_path):
    import sqlite3
    from pathlib import Path

    from app.models import Job, RequestRecord
    from app.storage import Storage
    from test_core import STUDY

    study = new_study(admin)
    other = new_study(admin)
    jobs = []
    for item in (study, other):
        response = admin.post(f"/api/admin/studies/{item['id']}/outline", json=STUDY, headers=headers())
        assert response.status_code == 200
        jobs.append(response.json()["job_id"])
    storage = Storage(app.state.db, app.state.settings)
    backup = storage.backup()
    assert admin.delete(f"/api/admin/studies/{study['id']}", headers=headers()).status_code == 200
    with app.state.db.read() as db:
        assert db.get(Job, jobs[0]) is None
        assert db.get(Job, jobs[1]) is not None
        assert all(jobs[0] not in str(record.response) for record in db.scalars(select(RequestRecord)))
    target = tmp_path / "study-outline-recovery"
    storage.restore(Path(backup["backup_path"]), target)
    with sqlite3.connect(target / "probeflow.sqlite3") as db:
        assert db.execute("SELECT id FROM jobs WHERE id = ?", (jobs[0],)).fetchone() is None
        assert db.execute("SELECT id FROM jobs WHERE id = ?", (jobs[1],)).fetchone() is not None
        assert all(
            jobs[0] not in str(row[0]) for row in db.execute("SELECT response FROM idempotency_requests")
        )
