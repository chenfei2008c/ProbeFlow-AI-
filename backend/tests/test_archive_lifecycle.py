"""Exercise real app startup and daily maintenance against aged, complete mock archives."""

import hashlib
import os
import sqlite3
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.storage import Storage
from test_audio_flow import upload, wav_bytes
from test_core import headers, join, new_study
from test_interview_flow import drain


ARCHIVE_TABLES = (
    "studies",
    "study_versions",
    "sessions",
    "consents",
    "turns",
    "transcript_revisions",
    "audio_assets",
    "memory_snapshots",
    "reports",
    "report_citations",
    "jobs",
    "session_events",
)


def archive_rows(path):
    with sqlite3.connect(path) as db:
        return {
            table: db.execute(f"SELECT * FROM {table} ORDER BY id").fetchall() for table in ARCHIVE_TABLES
        }


@pytest.mark.parametrize("age_days", [8, 91, 3650])
def test_startup_and_daily_maintenance_preserve_complete_aged_archive(admin, app, age_days, monkeypatch):
    study = new_study(admin)
    participant, sid, _ = join(admin, app, study)
    version = participant.get("/api/config").json()["consent_version"]
    assert (
        participant.post(
            "/api/participant/consent",
            json={"version": version, "mode": "voice", "processing": True, "permanent": True},
            headers=headers(),
        ).status_code
        == 200
    )
    drain(app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    sound = wav_bytes()
    response, manifest = upload(participant, tid, 0, sound)
    assert response.status_code == 200
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/finalize", json={"chunks": [manifest]}, headers=headers()
        ).status_code
        == 200
    )
    drain(app)
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/confirm",
            json={"text": "虚构材料只补充了一次。"},
            headers=headers(),
        ).status_code
        == 200
    )
    drain(app)
    assert (
        participant.post("/api/participant/control", json={"action": "end"}, headers=headers()).status_code
        == 200
    )
    drain(app)
    assert (
        admin.post(
            f"/api/admin/turns/{tid}/revision",
            json={"text": "勘误：虚构材料补充一次，次日办好。"},
            headers=headers(),
        ).status_code
        == 200
    )
    assert admin.post(f"/api/admin/sessions/{sid}/reports", json={}, headers=headers()).status_code == 200
    drain(app)
    assert (
        admin.post(
            f"/api/admin/studies/{study['id']}/archive", json={"archived": True}, headers=headers()
        ).status_code
        == 200
    )

    settings = app.state.settings
    disposable = settings.data_dir / "temp" / "disposable.tmp"
    disposable.parent.mkdir(exist_ok=True)
    disposable.write_bytes(b"fictional export cache")
    old = time.time() - age_days * 86400
    with sqlite3.connect(app.state.db.path) as db:
        for table in (*ARCHIVE_TABLES, "upload_chunks"):
            db.execute(f"UPDATE {table} SET created_at = created_at - ?", (age_days * 86400,))
        db.execute("UPDATE sessions SET ended_at = ended_at - ?", (age_days * 86400,))
        assets = db.execute(
            "SELECT id, path, sha256, source, retention, expires_at FROM audio_assets"
        ).fetchall()
    for path in settings.data_dir.rglob("*"):
        if path.is_file() and not path.name.startswith("probeflow.sqlite3"):
            os.utime(path, (old, old))
    baseline = archive_rows(app.state.db.path)
    exported = admin.get(f"/api/admin/sessions/{sid}/export").json()
    assert len(exported["reports"]) == 2
    assert len(next(t for t in exported["turns"] if t["id"] == tid)["revisions"]) == 3
    assert len(assets) == 3 and {asset[3] for asset in assets} == {"recording", "tts"}

    cleanup_calls = []
    daily_finished = threading.Event()
    real_cleanup, real_backup = Storage.cleanup, Storage.backup

    def cleanup(storage, *args, **kwargs):
        result = real_cleanup(storage, *args, **kwargs)
        cleanup_calls.append(result)
        return result

    def backup(storage, kind="manual"):
        result = real_backup(storage, kind)
        if kind == "daily":
            daily_finished.set()
        return result

    monkeypatch.setattr(Storage, "cleanup", cleanup)
    monkeypatch.setattr(Storage, "backup", backup)
    restarted = create_app(settings.model_copy(update={"worker_enabled": True}))
    with TestClient(restarted) as client:
        assert daily_finished.wait(5), "the actual daily maintenance loop did not finish its backup"
        assert len(cleanup_calls) >= 2  # Worker.start and its first due daily maintenance both ran.
        assert not disposable.exists()
        assert archive_rows(restarted.state.db.path) == baseline
        assert (
            client.post(
                "/api/admin/login", json={"password": "a-test-password-only"}, headers=headers()
            ).status_code
            == 200
        )
        after = client.get(f"/api/admin/sessions/{sid}/export").json()
        for key in (
            "session",
            "study",
            "study_version",
            "consents",
            "turns",
            "reports",
            "memory",
            "coverage",
        ):
            assert after[key] == exported[key]
        for asset_id, path, digest, _, policy, expiry in assets:
            response = client.get(f"/api/media/{asset_id}")
            assert response.status_code == 200
            assert hashlib.sha256(response.content).hexdigest() == digest
            assert (settings.data_dir / path).is_file()
            assert policy == "permanent" and expiry is None
        for format in ("json", "markdown", "csv"):
            assert client.get(f"/api/admin/sessions/{sid}/export?format={format}").status_code == 200
