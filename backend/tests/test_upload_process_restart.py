"""Kill the app while an upload response is unacknowledged, then retry the request."""

import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Asset, Job, Ledger, UploadChunk
from app.providers import ASRResult, Usage
from app.worker import Worker
from process_helpers import kill_at_marker
from test_audio_flow import ready, upload, wav_bytes
from test_core import headers


UPLOAD_SCRIPT = r"""
import asyncio, hashlib, json, sys, time
from pathlib import Path
from fastapi.testclient import TestClient
from app.config import Settings
from app.main import create_app
from app.storage import Storage

data, marker, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
request = json.loads(Path(sys.argv[4]).read_text())
sound = Path(sys.argv[5]).read_bytes()
app = create_app(Settings(_env_file=None, data_dir=data, mode="mock", worker_enabled=False))
if stage == "chunk_written":
    original = Storage.write_file
    def write(storage, relative, content):
        path = original(storage, relative, content)
        if relative.startswith("chunks/"):
            marker.write_text(stage)
            while True:
                time.sleep(0.1)
        return path
    Storage.write_file = write
else:
    @app.middleware("http")
    async def withhold_response(request, call_next):
        response = await call_next(request)
        assert response.status_code == 200
        marker.write_text(stage)
        await asyncio.Event().wait()
        return response
with TestClient(app) as client:
    client.cookies.set("probeflow_participant", request["cookie"])
    headers = {"X-ProbeFlow-Client": "web", "Idempotency-Key": request["key"]}
    if stage == "finalized":
        client.post(f"/api/participant/turns/{request['tid']}/finalize",
                    json={"chunks": [{"seq": 0, "sha256": hashlib.sha256(sound).hexdigest()}]},
                    headers=headers)
    else:
        client.put(f"/api/participant/turns/{request['tid']}/chunks/0", content=sound,
                   headers={**headers, "X-Chunk-SHA256": hashlib.sha256(sound).hexdigest()})
"""


@pytest.mark.parametrize("stage", ["chunk_written", "chunk_registered", "finalized"])
def test_retry_after_app_dies_before_upload_ack_preserves_original_and_single_asr(
    admin, app, tmp_path, monkeypatch, stage
):
    participant, sid = ready(admin, app)
    tid = participant.post("/api/participant/turns", json={"input_mode": "voice"}, headers=headers()).json()[
        "turn_id"
    ]
    sound = wav_bytes()
    if stage == "finalized":
        assert upload(participant, tid, 0, sound)[0].status_code == 200
    request_file, sound_file = tmp_path / "fictional-request.json", tmp_path / "fictional.wav"
    request_headers = headers()
    request_file.write_text(
        json.dumps(
            {
                "tid": tid,
                "cookie": participant.cookies.get("probeflow_participant"),
                "key": request_headers["Idempotency-Key"],
            }
        )
    )
    sound_file.write_bytes(sound)
    kill_at_marker(
        UPLOAD_SCRIPT,
        app.state.settings.data_dir,
        tmp_path / "upload-ready",
        stage,
        str(request_file),
        str(sound_file),
    )
    chunk_path = app.state.settings.data_dir / f"chunks/{sid}/{tid}/00000.chunk"
    assert chunk_path.read_bytes() == sound
    # Completed originals may legitimately release old chunks during maintenance.
    # Age the incomplete uploads here; queued ASR protection is covered separately.
    old = time.time() - (10 * 86400 if stage != "finalized" else 0)
    os.utime(chunk_path, (old, old))
    with app.state.db.transaction() as db:
        chunks = list(db.scalars(select(UploadChunk).where(UploadChunk.turn_id == tid)))
        jobs = list(db.scalars(select(Job).where(Job.session_id == sid, Job.kind == "asr")))
        assert len(chunks) == (0 if stage == "chunk_written" else 1)
        assert len(jobs) == (1 if stage == "finalized" else 0)
        previous_job = jobs[0].id if jobs else None
        for chunk in chunks:
            chunk.created_at = old

    class Provider:
        calls = 0

        async def transcribe(self, *args, **kwargs):
            self.calls += 1
            return ASRResult("上传恢复的虚构转写。", [], Usage(audio_seconds=1, source="estimated"))

    provider = Provider()
    monkeypatch.setattr(Worker, "provider", lambda self: provider)
    restarted = create_app(app.state.settings.model_copy(update={"worker_enabled": True}))
    with TestClient(restarted) as client:
        client.cookies.update(participant.cookies)
        response, manifest = upload(client, tid, 0, sound)
        assert response.status_code == 200
        assert upload(client, tid, 0, sound + b"conflict")[0].status_code == 409
        path = f"/api/participant/turns/{tid}/finalize"
        response = client.post(path, json={"chunks": [manifest]}, headers=request_headers)
        assert response.status_code == 200, response.text
        jid = response.json()["job_id"]
        if previous_job:
            assert jid == previous_job
        # Both the same idempotency key and a new request key return the original task.
        for retry_headers in (request_headers, headers()):
            assert (
                client.post(path, json={"chunks": [manifest]}, headers=retry_headers).json()
                == response.json()
            )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            detail = client.get("/api/participant/session").json()
            turn = next(t for t in detail["turns"] if t["id"] == tid)
            if turn["status"] == "confirming":
                break
            time.sleep(0.02)
        assert turn["status"] == "confirming", detail
        assert turn["text"] == "上传恢复的虚构转写。" and len(turn["revisions"]) == 1
        assert client.get(f"/api/media/{turn['audio_asset_id']}").content == sound
        assert provider.calls == 1
        with restarted.state.db.read() as db:
            assert len(list(db.scalars(select(Job).where(Job.session_id == sid, Job.kind == "asr")))) == 1
            assert len(list(db.scalars(select(Asset).where(Asset.turn_id == tid)))) == 1
            assert len(list(db.scalars(select(Ledger).where(Ledger.job_id == jid)))) == 1
