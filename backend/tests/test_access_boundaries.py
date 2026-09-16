"""Exercise known, existing resources so denial cannot pass by accident on missing IDs."""

import hashlib
import time

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Auth, Job
from app.security import COOKIES, digest
from test_audio_flow import ready, upload, wav_bytes
from test_core import STUDY, headers
from test_interview_flow import drain


def owned_archive(admin, app):
    participant, sid = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    sound = wav_bytes()
    _, manifest = upload(participant, tid, 0, sound)
    response = participant.post(
        f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers()
    )
    assert response.status_code == 200
    drain(app)
    detail = admin.get(f"/api/admin/sessions/{sid}").json()
    turn = next(t for t in detail["turns"] if t["id"] == tid)
    return participant, detail, turn, response.json()["job_id"], sound, manifest


def test_participant_cannot_retry_researcher_or_internal_tasks_even_in_own_session(admin, app):
    participant, sid = ready(admin, app, "text")
    jobs = []
    with app.state.db.transaction() as db:
        for kind in ("report", "summary"):
            job = Job(kind=kind, session_id=sid, status="failed", payload={}, dedup_key=f"private:{kind}")
            db.add(job)
            db.flush()
            jobs.append(job.id)
    before = admin.get(f"/api/admin/sessions/{sid}").json()
    for jid in jobs:
        response = participant.post(
            "/api/participant/control",
            json={"action": "retry", "job_id": jid, "accept_possible_charge": True},
            headers=headers(),
        )
        assert response.status_code == 409 and response.json()["code"] == "JOB_NOT_RETRYABLE"
    assert admin.get(f"/api/admin/sessions/{sid}").json() == before


def test_own_interview_tasks_remain_visible_and_retryable_with_idempotency(admin, app):
    participant, sid = ready(admin, app, "text")
    jobs = []
    with app.state.db.transaction() as db:
        for kind in ("decide", "asr", "tts"):
            job = Job(kind=kind, session_id=sid, status="failed", payload={}, dedup_key=f"retry:{kind}")
            db.add(job)
            db.flush()
            jobs.append(job.id)
    visible = participant.get("/api/participant/session").json()["jobs"]
    assert set(jobs) <= {job["id"] for job in visible}
    for jid in jobs:
        key = headers()
        body = {"action": "retry", "job_id": jid}
        response = participant.post("/api/participant/control", json=body, headers=key)
        assert response.status_code == 200 and response.json()["job_id"] == jid
        assert participant.post("/api/participant/control", json=body, headers=key).json() == response.json()
    with app.state.db.read() as db:
        assert all(db.get(Job, jid).status == "queued" for jid in jobs)


def test_existing_media_turns_events_and_tasks_are_scoped_and_failed_attacks_do_not_mutate(admin, app):
    owner, before, turn, jid, sound, manifest = owned_archive(admin, app)
    other, other_sid = ready(admin, app, "text")
    sid, tid, aid = before["session"]["id"], turn["id"], turn["audio_asset_id"]
    question_id = next(t["id"] for t in before["turns"] if t["role"] == "assistant")
    with TestClient(app) as anonymous:
        # Both ordinary and range reads authenticate before exposing any media bytes.
        for client, status in ((anonymous, 401), (other, 404)):
            for extra in ({}, {"Range": "bytes=0-63"}):
                response = client.get(f"/api/media/{aid}", headers=extra)
                assert response.status_code == status
                assert sound[:32] not in response.content
        assert anonymous.get("/api/participant/session").status_code == 401
        assert anonymous.get("/api/participant/events?after=0").status_code == 401
    assert owner.get(f"/api/media/{aid}").content == sound
    assert admin.get(f"/api/media/{aid}", headers={"Range": "bytes=0-63"}).content == sound[:64]
    for action in (
        {"action": "playback_done", "turn_id": question_id, "played_complete": True},
        {"action": "rerecord", "turn_id": tid},
    ):
        assert other.post("/api/participant/control", json=action, headers=headers()).status_code == 404
    assert (
        other.post(
            "/api/participant/control", json={"action": "retry", "job_id": jid}, headers=headers()
        ).status_code
        == 409
    )
    assert (
        other.post(
            f"/api/participant/turns/{tid}/confirm", json={"text": "不应覆盖他人的回答"}, headers=headers()
        ).status_code
        == 404
    )
    assert (
        other.post(
            f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers()
        ).status_code
        == 404
    )
    assert (
        other.put(
            f"/api/participant/turns/{tid}/chunks/1",
            content=sound,
            headers={**headers(), "X-Chunk-SHA256": hashlib.sha256(sound).hexdigest()},
        ).status_code
        == 404
    )
    events = other.get(f"/api/participant/events?after=0&session_id={sid}")
    assert events.status_code == 200 and events.json()["events"]
    for identifier in (sid, tid, question_id, aid, jid):
        assert identifier not in events.text
    own = other.get(f"/api/participant/session?session_id={sid}").json()
    assert own["session"]["id"] == other_sid
    assert not {"reports", "consents", "coverage", "memory"} & own.keys()
    assert not {"budget_cny", "spent_cny", "reserved_cny"} & own["session"].keys()
    assert not {"exclusions", "glossary", "topics", "participant_description"} & own["study"].keys()
    assert admin.get(f"/api/admin/sessions/{sid}").json() == before
    assert owner.get(f"/api/media/{aid}").content == sound


def test_admin_api_and_role_cookies_cannot_be_used_by_participants_or_expired_credentials(admin, app):
    owner, before, turn, jid, sound, _ = owned_archive(admin, app)
    sid, study_id = before["session"]["id"], before["session"]["study_id"]
    endpoints = [
        ("GET", "/api/admin/me", None),
        ("GET", "/api/admin/studies", None),
        ("GET", f"/api/admin/studies/{study_id}", None),
        ("GET", f"/api/admin/studies/{study_id}/sessions", None),
        ("GET", f"/api/admin/studies/{study_id}/invites", None),
        ("GET", f"/api/admin/sessions/{sid}", None),
        ("GET", f"/api/admin/sessions/{sid}/export", None),
        ("GET", f"/api/admin/jobs/{jid}", None),
        ("GET", "/api/admin/usage", None),
        ("GET", "/api/admin/diagnostics", None),
        ("POST", "/api/admin/studies", STUDY),
        ("POST", f"/api/admin/studies/{study_id}/versions", STUDY),
        ("POST", f"/api/admin/studies/{study_id}/outline", STUDY),
        ("POST", f"/api/admin/studies/{study_id}/archive", {"archived": True}),
        ("POST", f"/api/admin/studies/{study_id}/invites", {}),
        ("POST", f"/api/admin/sessions/{sid}/recovery-invite", {}),
        ("POST", f"/api/admin/sessions/{sid}/reports", {}),
        ("POST", f"/api/admin/sessions/{sid}/budget", {"budget_cny": "99.00"}),
        ("POST", f"/api/admin/turns/{turn['id']}/revision", {"text": "越权勘误"}),
        ("POST", "/api/admin/diagnostics/tts", {"text": "虚构试听"}),
        ("DELETE", f"/api/admin/sessions/{sid}", None),
        ("DELETE", f"/api/admin/studies/{study_id}", None),
    ]
    with TestClient(app) as anonymous:
        for client in (anonymous, owner):
            for method, path, body in endpoints:
                response = client.request(method, path, json=body, headers=headers())
                assert response.status_code == 401, (method, path, response.text)
    # Merely putting a valid token in the other role's cookie never grants that role.
    with TestClient(app) as swapped:
        swapped.cookies.set(COOKIES["admin"], owner.cookies.get(COOKIES["participant"]))
        swapped.cookies.set(COOKIES["participant"], admin.cookies.get(COOKIES["admin"]))
        assert swapped.get(f"/api/admin/sessions/{sid}").status_code == 401
        assert swapped.get("/api/participant/session").status_code == 401
        assert swapped.get(f"/api/media/{turn['audio_asset_id']}").status_code == 401
    with app.state.db.transaction() as db:
        auth = db.scalar(
            select(Auth).where(Auth.token_hash == digest(owner.cookies.get(COOKIES["participant"])))
        )
        auth.expires_at = time.time() - 1
    assert owner.get("/api/participant/session").status_code == 401
    assert owner.get(f"/api/media/{turn['audio_asset_id']}").status_code == 401
    assert admin.get(f"/api/admin/sessions/{sid}").json() == before
    assert admin.get(f"/api/media/{turn['audio_asset_id']}").content == sound
