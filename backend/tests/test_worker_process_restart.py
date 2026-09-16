"""Kill only a test-owned worker subprocess, then start the real app lifecycle."""

import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import create_app
from app.models import Job, Ledger, Reservation
from app.worker import Worker
from test_audio_flow import ready
from test_core import headers
from test_report_flow import finish_answer


WORKER_SCRIPT = r"""
import asyncio, json, sys
from pathlib import Path
from app.config import Settings
from app.db import Database
from app.interview import mock_response
from app.providers import TextResult, Usage
from app.worker import Worker

data, marker, stage = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
settings = Settings(_env_file=None, data_dir=data, mode="mock", worker_enabled=False)
database = Database(settings)
class Provider:
    async def text(self, role, messages, **kwargs):
        if stage == "inflight":
            marker.write_text("request began")
            await asyncio.Event().wait()
        return TextResult(json.dumps(mock_response(json.loads(messages[-1]["content"])), ensure_ascii=False), Usage(source="mock"))
class PausedWorker(Worker):
    async def _text(self, *args, **kwargs):
        result = await super()._text(*args, **kwargs)
        marker.write_text("checkpoint committed")
        await asyncio.Event().wait()
        return result
asyncio.run(PausedWorker(database, settings, provider=Provider()).run_once())
"""


@pytest.mark.parametrize("stage", ["inflight", "checkpoint"])
def test_killed_report_worker_recovers_without_blind_paid_repetition(
    admin, app, tmp_path, monkeypatch, stage
):
    participant, sid = ready(admin, app, "text")
    tid, jid = finish_answer(participant)
    before = admin.get(f"/api/admin/sessions/{sid}").json()
    marker = tmp_path / "worker-ready"
    child = subprocess.Popen(
        [sys.executable, "-c", WORKER_SCRIPT, str(app.state.settings.data_dir), str(marker), stage],
        cwd=tmp_path,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(Path(__file__).parents[1])},
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), (
            child.communicate(timeout=1) if child.poll() is not None else "worker did not reach checkpoint"
        )
        assert child.poll() is None
        child.kill()
        child.communicate(timeout=5)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=5)

    class ForbiddenProvider:
        calls = 0

        async def text(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("restart must not repeat a paid request")

    forbidden = ForbiddenProvider()
    monkeypatch.setattr(Worker, "provider", lambda self: forbidden)
    restarted = create_app(app.state.settings.model_copy(update={"worker_enabled": True}))
    with TestClient(restarted) as client:
        assert (
            client.post(
                "/api/admin/login", json={"password": "a-test-password-only"}, headers=headers()
            ).status_code
            == 200
        )
        expected = "external_status_unknown" if stage == "inflight" else "succeeded"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            job = client.get(f"/api/admin/jobs/{jid}").json()
            if job["status"] == expected:
                break
            time.sleep(0.02)
        assert job["status"] == expected
        assert forbidden.calls == 0
        after = client.get(f"/api/admin/sessions/{sid}").json()
        assert after["session"]["status"] == "completed"
        assert after["turns"] == before["turns"] and after["consents"] == before["consents"]
        assert client.get(f"/api/admin/sessions/{sid}/export").status_code == 200
        if stage == "checkpoint":
            assert len(after["reports"]) == 1
            assert after["reports"][0]["citations"][0]["turn_id"] == tid
        else:
            assert not after["reports"]
            response = client.post(
                f"/api/admin/sessions/{sid}/reports", json={"retry_job_id": jid}, headers=headers()
            )
            assert response.status_code == 409 and response.json()["code"] == "CHARGE_CONFIRMATION_REQUIRED"
        with restarted.state.db.read() as db:
            reservations = list(db.scalars(select(Reservation).where(Reservation.job_id == jid)))
            assert len(reservations) == 1
            assert reservations[0].state == ("unknown" if stage == "inflight" else "settled")
            assert len(list(db.scalars(select(Ledger).where(Ledger.job_id == jid)))) == 1
            assert db.get(Job, jid).attempt == (1 if stage == "inflight" else 2)
