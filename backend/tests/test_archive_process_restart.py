"""Actual process interruption at archive and deletion commit boundaries."""

import hashlib
import os
from pathlib import Path
import sqlite3
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Asset, Job, Ledger, Turn, UploadChunk
from app.providers import ASRResult, Usage
from app.storage import Storage
from app.worker import Worker
from process_helpers import kill_at_marker
from test_audio_flow import ready, upload, wav_bytes
from test_core import headers
from test_interview_flow import drain


ARCHIVE_SCRIPT = r"""
import asyncio, sys, time
from pathlib import Path
from app.config import Settings
from app.db import Database
from app.storage import Storage
from app.worker import Worker
import app.audio as audio

data, marker, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
settings = Settings(_env_file=None, data_dir=data, mode="mock", worker_enabled=False)
database = Database(settings)
def pause():
    marker.write_text(stage)
    while True:
        time.sleep(0.1)
if stage in {"original_written", "original_registered"}:
    if stage == "original_written":
        original = audio.assemble_chunks
        def assemble(*args, **kwargs):
            original(*args, **kwargs)
            pause()
        audio.assemble_chunks = assemble
    else:
        audio.probe_audio = lambda *args, **kwargs: pause()
    asyncio.run(Worker(database, settings).run_once())
else:
    storage = Storage(database, settings)
    if stage == "tombstone_written":
        original = storage._write_ledger
        def ledger(*args, **kwargs):
            original(*args, **kwargs)
            pause()
        storage._write_ledger = ledger
    else:
        storage._remove_unreferenced_paths = lambda *args, **kwargs: pause()
    storage.delete_session(sys.argv[4], withdrawn=True)
"""


def pending_voice(admin, app):
    participant, sid = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    sound = wav_bytes()
    # Two transport blocks intentionally cut across the media payload.
    manifests = [
        upload(participant, tid, seq, chunk)[1] for seq, chunk in enumerate((sound[:1001], sound[1001:]))
    ]
    response = participant.post(
        f"/api/participant/turns/{tid}/finalize", json={"chunks": manifests}, headers=headers()
    )
    assert response.status_code == 200
    return participant, sid, tid, response.json()["job_id"], sound


def login(client):
    assert (
        client.post(
            "/api/admin/login", json={"password": "a-test-password-only"}, headers=headers()
        ).status_code
        == 200
    )


@pytest.mark.parametrize("stage", ["original_written", "original_registered"])
def test_killed_audio_writer_recovers_original_from_durable_archive_or_uploads(
    admin, app, tmp_path, monkeypatch, stage
):
    _, sid, tid, jid, sound = pending_voice(admin, app)
    data_dir = app.state.settings.data_dir
    kill_at_marker(ARCHIVE_SCRIPT, data_dir, tmp_path / "archive-ready", stage)
    original = data_dir / f"audio/{sid}/{tid}.original"
    assert original.read_bytes() == sound
    with app.state.db.transaction() as db:
        turn = db.get(Turn, tid)
        old_asset_id = turn.audio_asset_id
        assert bool(old_asset_id) == (stage == "original_registered")
        assert db.get(Job, jid).status == "running"
        assert not db.get(Job, jid).call_started
        chunks = list(db.scalars(select(UploadChunk).where(UploadChunk.turn_id == tid)))
        assert len(chunks) == 2
        old = time.time() - 10 * 86400
        for chunk in chunks:
            chunk.created_at = old
            os.utime(data_dir / chunk.path, (old, old))

    class Provider:
        calls = 0

        async def transcribe(self, *args, **kwargs):
            self.calls += 1
            return ASRResult("进程恢复后的虚构转写。", [], Usage(audio_seconds=1, source="estimated"))

    provider = Provider()
    monkeypatch.setattr(Worker, "provider", lambda self: provider)
    restarted = create_app(app.state.settings.model_copy(update={"worker_enabled": True}))
    with TestClient(restarted) as client:
        login(client)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/api/admin/jobs/{jid}").json()
            if job["status"] == "succeeded":
                break
            time.sleep(0.02)
        assert job["status"] == "succeeded", job
        assert provider.calls == 1
        detail = client.get(f"/api/admin/sessions/{sid}").json()
        turn = next(t for t in detail["turns"] if t["id"] == tid)
        assert turn["status"] == "confirming" and len(turn["revisions"]) == 1
        assert turn["text"] == "进程恢复后的虚构转写。"
        if old_asset_id:
            assert turn["audio_asset_id"] == old_asset_id
        assert client.get(f"/api/media/{turn['audio_asset_id']}").content == sound
        assert client.get(f"/api/admin/sessions/{sid}/export").status_code == 200
        with restarted.state.db.read() as db:
            assets = list(db.scalars(select(Asset).where(Asset.turn_id == tid)))
            assert len(assets) == 1
            assert assets[0].sha256 == hashlib.sha256(sound).hexdigest()
            assert assets[0].retention == "permanent" and assets[0].expires_at is None
            assert len(list(db.scalars(select(Ledger).where(Ledger.job_id == jid)))) == 1
        # Cleanup may now remove old transport chunks; the complete original remains readable.
        Storage(restarted.state.db, restarted.state.settings).cleanup()
        with restarted.state.db.read() as db:
            assert not list(db.scalars(select(UploadChunk).where(UploadChunk.turn_id == tid)))
        assert client.get(f"/api/media/{turn['audio_asset_id']}").content == sound


@pytest.mark.parametrize("stage", ["tombstone_written", "rows_deleted"])
def test_killed_withdrawal_finishes_before_requests_or_jobs_and_filters_old_backup(
    admin, app, tmp_path, monkeypatch, stage
):
    participant, sid, tid, _, _ = pending_voice(admin, app)
    drain(app)
    assert (
        participant.post(
            f"/api/participant/turns/{tid}/confirm", json={"text": "撤回测试的虚构回答。"}, headers=headers()
        ).status_code
        == 200
    )
    participant.post("/api/participant/control", json={"action": "end"}, headers=headers())
    drain(app)
    assert admin.post(f"/api/admin/sessions/{sid}/reports", headers=headers()).status_code == 200
    # A different session must survive both cleanup and restore unchanged.
    _, survivor = ready(admin, app, "text")
    baseline = admin.get(f"/api/admin/sessions/{survivor}").json()
    data_dir = app.state.settings.data_dir
    storage = Storage(app.state.db, app.state.settings)
    backup = storage.backup()
    before = admin.get(f"/api/admin/sessions/{sid}").json()
    asset_ids = [t["audio_asset_id"] for t in before["turns"] if t["audio_asset_id"]]
    assert asset_ids and before["reports"][0]["citations"]
    kill_at_marker(ARCHIVE_SCRIPT, data_dir, tmp_path / "deletion-ready", stage, sid)
    assert storage.is_deleted(sid)
    assert (data_dir / "audio" / sid).exists()
    with sqlite3.connect(app.state.db.path) as db:
        assert bool(db.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone()) == (
            stage == "tombstone_written"
        )

    class ForbiddenProvider:
        calls = 0

        async def text(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("withdrawn content must never be sent to a provider")

    forbidden = ForbiddenProvider()
    monkeypatch.setattr(Worker, "provider", lambda self: forbidden)
    restarted = create_app(app.state.settings.model_copy(update={"worker_enabled": True}))
    with TestClient(restarted) as client:
        login(client)
        assert client.get(f"/api/admin/sessions/{sid}").status_code == 404
        assert client.get(f"/api/admin/sessions/{sid}/export").status_code == 404
        for aid in asset_ids:
            assert client.get(f"/api/media/{aid}").status_code == 404
        client.cookies.update(participant.cookies)
        assert client.get("/api/participant/session").status_code == 401
        assert client.get(f"/api/admin/sessions/{survivor}").json() == baseline
        assert not (data_dir / "audio" / sid).exists()
        assert not (data_dir / "chunks" / sid).exists()
        assert forbidden.calls == 0
        # Verify row removal, not only authentication failures.
        with sqlite3.connect(restarted.state.db.path) as db:
            for table in (
                "turns",
                "reports",
                "audio_assets",
                "upload_chunks",
                "jobs",
                "consents",
                "memory_snapshots",
                "session_events",
            ):
                assert (
                    db.execute(f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (sid,)).fetchone()[0]
                    == 0
                )
            assert (
                db.execute("SELECT COUNT(*) FROM transcript_revisions WHERE turn_id = ?", (tid,)).fetchone()[
                    0
                ]
                == 0
            )
            assert (
                db.execute("SELECT COUNT(*) FROM report_citations WHERE turn_id = ?", (tid,)).fetchone()[0]
                == 0
            )
    restored = tmp_path / "restored"
    storage.restore(Path(backup["backup_path"]), restored)
    with sqlite3.connect(restored / "probeflow.sqlite3") as db:
        assert db.execute("SELECT id FROM sessions WHERE id = ?", (sid,)).fetchone() is None
        assert db.execute("SELECT id FROM sessions WHERE id = ?", (survivor,)).fetchone()
        assert not db.execute("SELECT id FROM jobs WHERE session_id = ?", (sid,)).fetchall()
    assert not (restored / "audio" / sid).exists()
