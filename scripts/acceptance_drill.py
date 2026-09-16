"""Offline, fictional-data archive/restore drill; never calls an external provider."""

import asyncio
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from fastapi.testclient import TestClient  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.storage import Storage  # noqa: E402
from app.worker import Worker  # noqa: E402


def headers():
    return {"X-ProbeFlow-Client": "web", "Idempotency-Key": uuid4().hex}


def post(client, path, data=None):
    response = client.post(path, json=data or {}, headers=headers())
    assert response.status_code == 200, (path, response.status_code, response.text)
    return response.json()


def drain(app):
    async def run():
        worker = Worker(app.state.db, app.state.settings)
        for _ in range(40):
            if not await worker.run_once():
                return
        raise AssertionError("jobs did not settle")

    asyncio.run(run())


def recording():
    data = io.BytesIO()
    with wave.open(data, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16000)
        audio.writeframes(
            b"".join(
                struct.pack("<h", int(5000 * math.sin(index * 2 * math.pi * 440 / 16000)))
                for index in range(32000)
            )
        )
    return data.getvalue()


def main():
    root = Path(tempfile.mkdtemp(prefix="probeflow-archive-drill-"))
    settings = Settings(
        data_dir=root / "primary",
        backup_dir=root / "backups",
        mode="mock",
        admin_password="fictional-drill-password",
        worker_enabled=False,
    )
    app = create_app(settings)
    admin = TestClient(app)
    post(admin, "/api/admin/login", {"password": "fictional-drill-password"})
    study = post(
        admin,
        "/api/admin/studies",
        {
            "title": "虚构档案恢复演练",
            "objective": "验证永久保存及恢复完整性",
            "topics": [
                {"id": "process", "title": "经历", "minutes": 30},
                {"id": "check", "title": "核对", "minutes": 30},
            ],
        },
    )
    sessions = []
    for number in range(2):
        invite = post(admin, f"/api/admin/studies/{study['id']}/invites")
        token = parse_qs(urlsplit(invite["url"]).fragment)["token"][0]
        participant = TestClient(app)
        sid = post(participant, "/api/participant/exchange", {"token": token})["session_id"]
        version = participant.get("/api/config").json()["consent_version"]
        post(
            participant,
            "/api/participant/consent",
            {"version": version, "mode": "voice", "processing": True, "permanent": True},
        )
        drain(app)
        question = participant.get("/api/participant/session").json()["turns"][-1]
        post(
            participant,
            "/api/participant/control",
            {"action": "playback_done", "turn_id": question["id"], "played_complete": True},
        )
        tid = post(participant, "/api/participant/turns", {"input_mode": "voice", "mime_type": "audio/wav"})[
            "turn_id"
        ]
        sound = recording()
        manifest = []
        for seq, content in enumerate((sound[:12000], sound[12000:])):
            digest = hashlib.sha256(content).hexdigest()
            response = participant.put(
                f"/api/participant/turns/{tid}/chunks/{seq}",
                content=content,
                headers={**headers(), "X-Chunk-SHA256": digest},
            )
            assert response.status_code == 200, response.text
            manifest.append({"seq": seq, "sha256": digest})
        post(participant, f"/api/participant/turns/{tid}/finalize", {"chunks": manifest})
        drain(app)
        post(
            participant,
            f"/api/participant/turns/{tid}/confirm",
            {"text": f"上次我提交了一份虚构材料，只补充一次。样本{number}。"},
        )
        drain(app)
        post(participant, "/api/participant/control", {"action": "end"})
        drain(app)
        post(
            admin, f"/api/admin/turns/{tid}/revision", {"text": f"勘误：虚构材料只补充了一次。样本{number}。"}
        )
        post(admin, f"/api/admin/sessions/{sid}/reports")
        drain(app)
        detail = admin.get(f"/api/admin/sessions/{sid}").json()
        assert len(detail["reports"]) == 2
        assert len(next(turn for turn in detail["turns"] if turn["id"] == tid)["revisions"]) == 3
        assert not [job for job in detail["jobs"] if job["status"] in ("failed", "external_status_unknown")]
        sessions.append(sid)

    storage = Storage(app.state.db, settings)
    with sqlite3.connect(app.state.db.path) as db:
        provenance_before = {
            table: dict(db.execute(f"SELECT id, provenance FROM {table}"))
            for table in ("audio_assets", "transcript_revisions", "reports")
        }
    backup = storage.backup()
    response = admin.delete(f"/api/admin/sessions/{sessions[1]}", headers=headers())
    assert response.status_code == 200
    restored = root / "restored"
    recovery = storage.restore(Path(backup["backup_path"]), restored)
    with sqlite3.connect(restored / "probeflow.sqlite3") as db:
        assert db.execute("SELECT id FROM sessions").fetchall() == [(sessions[0],)]
        files = db.execute("SELECT path, sha256, retention, expires_at FROM audio_assets").fetchall()
        for path, digest, policy, expiry in files:
            assert hashlib.sha256((restored / path).read_bytes()).hexdigest() == digest
            assert policy == "permanent" and expiry is None
        revision_count = db.execute("SELECT COUNT(*) FROM transcript_revisions").fetchone()[0]
        report_count = db.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
        citation_count = db.execute("SELECT COUNT(*) FROM report_citations").fetchone()[0]
        assert revision_count == 5 and report_count == 2 and citation_count == 2
        provenance_count = 0
        for table, original in provenance_before.items():
            for row_id, provenance in db.execute(f"SELECT id, provenance FROM {table}"):
                assert provenance == original[row_id]
                assert json.loads(provenance)["mode"] == "mock"
                provenance_count += 1
        assert provenance_count == 10

    evidence = {
        "mode": "mock",
        "fictional_only": True,
        "root": str(root),
        "backup_path": backup["backup_path"],
        "restore_path": str(restored),
        "kept_sessions": 1,
        "deleted_sessions_restored": 0,
        "verified_audio_files": len(files),
        "all_audio_hashes_match": True,
        "restored_text_revisions": revision_count,
        "restored_report_versions": report_count,
        "restored_exact_citations": citation_count,
        "restored_provenance_records": provenance_count,
        "all_provenance_snapshots_match": True,
        "restore_validation": recovery,
        "archive_bytes": storage.diagnostics()["formal_bytes"],
        "warning": "提示音与固定转写仅证明软件流程，不代表真人或普通话能力验收。",
    }
    output = ROOT / "docs/verification/backup-drill.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(evidence, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
