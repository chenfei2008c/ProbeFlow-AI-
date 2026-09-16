"""Integration breaks: unauthorized input/access, mutable versions, loss of durable state."""

from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient


def headers():
    return {"X-ProbeFlow-Client": "web", "Idempotency-Key": str(uuid4())}


@pytest.fixture
def settings(tmp_path):
    from app.config import Settings

    return Settings(data_dir=tmp_path / "data", admin_password="a-test-password-only", worker_enabled=False)


@pytest.fixture
def app(settings):
    from app.main import create_app

    return create_app(settings)


@pytest.fixture
def admin(app):
    with TestClient(app) as client:
        assert (
            client.post(
                "/api/admin/login", json={"password": "a-test-password-only"}, headers=headers()
            ).status_code
            == 200
        )
        yield client


STUDY = {
    "title": "流程访谈",
    "objective": "了解真实流程经历",
    "participant_description": "参与者",
    "target_minutes": 60,
    "topics": [
        {
            "id": "T1",
            "title": "经历",
            "research_question": "一次具体事件",
            "priority": 1,
            "evidence_type": "事件",
            "minutes": 30,
        },
        {
            "id": "T2",
            "title": "改善",
            "research_question": "顺利与困难",
            "priority": 2,
            "evidence_type": "对比",
            "minutes": 30,
        },
    ],
    "exclusions": "不要提供姓名",
    "glossary": [],
    "budget_cny": "5.00",
    "confirm_transcript": True,
    "tone": "中性",
}


def new_study(admin):
    response = admin.post("/api/admin/studies", json=STUDY, headers=headers())
    assert response.status_code == 200, response.text
    return response.json()


def join(admin, app, study):
    from urllib.parse import urlsplit, parse_qs

    result = admin.post(f"/api/admin/studies/{study['id']}/invites", json={}, headers=headers())
    assert result.status_code == 200, result.text
    token = parse_qs(urlsplit(result.json()["url"]).fragment)["token"][0]
    participant = TestClient(app)
    result = participant.post("/api/participant/exchange", json={"token": token}, headers=headers())
    assert result.status_code == 200, result.text
    return participant, result.json()["session_id"], token


def test_retention_and_data_location_reject_unsafe_configuration(tmp_path):
    from app.config import Settings
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(data_dir=tmp_path, retention_policy="90_days")
    with pytest.raises(ValidationError):
        Settings(data_dir=Path(__file__).resolve().parents[2] / "data")


def test_database_rejects_formal_archive_expiry(app):
    from sqlalchemy.exc import IntegrityError
    from app.models import Asset

    with pytest.raises(IntegrityError):
        with app.state.db.transaction() as db:
            db.add(
                Asset(
                    path="audio/test",
                    mime_type="audio/wav",
                    sha256="0" * 64,
                    byte_size=1,
                    duration_seconds=1,
                    source="tts",
                    expires_at=99999999,
                )
            )


def test_study_versions_are_immutable_and_idempotent(admin):
    key = headers()
    first = admin.post("/api/admin/studies", json=STUDY, headers=key).json()
    repeated = admin.post("/api/admin/studies", json=STUDY, headers=key).json()
    assert first["id"] == repeated["id"]
    updated = admin.post(
        f"/api/admin/studies/{first['id']}/versions", json={**STUDY, "objective": "新目标"}, headers=headers()
    ).json()
    assert first["current_version_id"] != updated["current_version_id"]
    assert updated["version"]["objective"] == "新目标"
    assert first["version"]["objective"] == "了解真实流程经历"


def test_consent_required_invite_single_use_and_scope(admin, app):
    study = new_study(admin)
    p, sid, token = join(admin, app, study)
    assert (
        p.post(
            "/api/participant/turns", json={"input_mode": "text", "text": "回答"}, headers=headers()
        ).status_code
        == 403
    )
    version = p.get("/api/config").json()["consent_version"]
    for processing, permanent in [(True, False), (False, True)]:
        r = p.post(
            "/api/participant/consent",
            json={"version": version, "mode": "text", "processing": processing, "permanent": permanent},
            headers=headers(),
        )
        assert r.status_code in (400, 403, 422)
    r = p.post(
        "/api/participant/consent",
        json={"version": version, "mode": "text", "processing": True, "permanent": True},
        headers=headers(),
    )
    assert r.status_code == 200, r.text
    assert p.get(f"/api/admin/sessions/{sid}").status_code == 401
    stranger = TestClient(app)
    assert (
        stranger.post("/api/participant/exchange", json={"token": token}, headers=headers()).status_code
        == 409
    )
    detail = p.get("/api/participant/session").json()
    assert detail["session"]["permanent_consent"] is True
    assert "reports" not in detail
    assert "budget_cny" not in detail["session"]
    assert "memory" not in detail


def test_origin_and_client_header_prevent_cross_site_mutation(admin):
    assert admin.post("/api/admin/studies", json=STUDY).status_code == 403
    bad = {**headers(), "Origin": "https://attacker.invalid"}
    assert admin.post("/api/admin/studies", json=STUDY, headers=bad).status_code == 403


def test_restart_preserves_study_and_archive_does_not_delete(settings, admin):
    from app.main import create_app

    study = new_study(admin)
    assert (
        admin.post(
            f"/api/admin/studies/{study['id']}/archive", json={"archived": True}, headers=headers()
        ).status_code
        == 200
    )
    with TestClient(create_app(settings)) as reopened:
        assert (
            reopened.post(
                "/api/admin/login", json={"password": "a-test-password-only"}, headers=headers()
            ).status_code
            == 200
        )
        detail = reopened.get(f"/api/admin/studies/{study['id']}").json()
        assert detail["archived"] is True
        assert detail["version"]["objective"] == STUDY["objective"]
