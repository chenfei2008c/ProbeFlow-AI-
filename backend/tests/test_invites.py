import time
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models import Auth, Invite
from test_core import headers, new_study
from test_archive_provenance import completed_mock_archive


def create_invite(admin, study):
    response = admin.post(f"/api/admin/studies/{study['id']}/invites", json={}, headers=headers())
    assert response.status_code == 200
    item = response.json()
    return item, parse_qs(urlsplit(item["url"]).fragment)["token"][0]


def test_invites_list_and_revoke_are_scoped_and_do_not_expose_tokens(admin, app):
    study, other = new_study(admin), new_study(admin)
    invite, token = create_invite(admin, study)
    participant = TestClient(app)
    path = f"/api/admin/studies/{study['id']}/invites"
    assert participant.get(path).status_code == 401
    listed = admin.get(path)
    assert listed.status_code == 200
    assert token not in listed.text and "token_hash" not in listed.text
    assert listed.json()[0]["status"] == "available"
    iid = listed.json()[0]["id"]
    assert invite["id"] == iid
    assert (
        admin.post(f"/api/admin/studies/{other['id']}/invites/{iid}/revoke", headers=headers()).status_code
        == 404
    )
    key = headers()
    revoke = path + f"/{iid}/revoke"
    assert admin.post(revoke, headers=key).json()["status"] == "revoked"
    assert admin.post(revoke, headers=key).json()["status"] == "revoked"
    assert (
        participant.post("/api/participant/exchange", json={"token": token}, headers=headers()).status_code
        == 403
    )


def test_expired_invite_cannot_be_redeemed_and_never_creates_a_session(admin, app):
    study = new_study(admin)
    _, token = create_invite(admin, study)
    with app.state.db.transaction() as db:
        db.scalar(select(Invite)).expires_at = time.time() - 1
    assert admin.get(f"/api/admin/studies/{study['id']}/invites").json()[0]["status"] == "expired"
    participant = TestClient(app)
    assert (
        participant.post("/api/participant/exchange", json={"token": token}, headers=headers()).status_code
        == 403
    )
    assert admin.get(f"/api/admin/studies/{study['id']}/sessions").json() == []


def test_redeemed_invite_requires_recovery_flow_to_replace_credentials(admin, app):
    study = new_study(admin)
    invite, token = create_invite(admin, study)
    participant = TestClient(app)
    assert (
        participant.post("/api/participant/exchange", json={"token": token}, headers=headers()).status_code
        == 200
    )
    response = admin.post(
        f"/api/admin/studies/{study['id']}/invites/{invite['id']}/revoke", headers=headers()
    )
    assert response.status_code == 409 and response.json()["code"] == "INVITE_USED"
    assert participant.get("/api/participant/session").status_code == 200


def test_recovery_invite_replaces_expired_access_without_deleting_completed_archive(admin, app):
    sid, _ = completed_mock_archive(admin, app)
    before = admin.get(f"/api/admin/sessions/{sid}").json()
    with app.state.db.transaction() as db:
        for auth in db.scalars(select(Auth).where(Auth.role == "participant", Auth.subject_id == sid)):
            auth.expires_at = time.time() - 1
    response = admin.post(f"/api/admin/sessions/{sid}/recovery-invite", headers=headers())
    assert response.status_code == 200
    token = parse_qs(urlsplit(response.json()["url"]).fragment)["token"][0]
    participant = TestClient(app)
    exchange = participant.post("/api/participant/exchange", json={"token": token}, headers=headers())
    assert exchange.json()["session_id"] == sid
    recovered = participant.get("/api/participant/session").json()
    assert recovered["session"]["status"] == "completed"
    assert recovered["turns"] == before["turns"]
    assert admin.get(f"/api/admin/sessions/{sid}/export").status_code == 200
    new_invite = admin.post(f"/api/admin/sessions/{sid}/recovery-invite", headers=headers())
    assert new_invite.status_code == 200
    assert participant.get("/api/participant/session").status_code == 401
    assert admin.get(f"/api/admin/sessions/{sid}").json()["reports"] == before["reports"]
