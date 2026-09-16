"""Real SQLite/file acceptance tests for retention, deletion, and backup recovery."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import Database
from app.errors import AppError
from app.models import (
    Asset,
    Auth,
    Citation,
    Consent,
    Event,
    InterviewSession,
    Invite,
    Job,
    Ledger,
    Memory,
    Report,
    RequestRecord,
    Reservation,
    Revision,
    Study,
    StudyVersion,
    Turn,
    UploadChunk,
)


@pytest.fixture
def storage_env(tmp_path):
    from app.storage import Storage

    settings = Settings(
        data_dir=tmp_path / "primary",
        backup_dir=tmp_path / "backups",
        admin_password="a-test-password-only",
        worker_enabled=False,
        min_free_bytes=0,
        temp_retention_hours=24,
        backup_count=7,
    )
    database = Database(settings)
    database.initialize()
    return Storage(database, settings), database, settings


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _write_fixture(settings: Settings, relative: str, content: bytes) -> Path:
    path = settings.data_dir / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _seed_archive(
    database: Database,
    settings: Settings,
    suffix: str = "one",
    study_context: tuple[str, str] | None = None,
) -> dict[str, str]:
    audio = f"permanent audio {suffix}".encode()
    relative = f"assets/{suffix}/answer.wav"
    _write_fixture(settings, relative, audio)
    with database.transaction() as db:
        if study_context:
            study = db.get(Study, study_context[0])
            version = db.get(StudyVersion, study_context[1])
            assert study is not None and version is not None
        else:
            study = Study(title=f"Study {suffix}")
            db.add(study)
            db.flush()
            version = StudyVersion(
                study_id=study.id,
                number=1,
                config={"title": f"Study {suffix}"},
                retention="permanent",
            )
            db.add(version)
            db.flush()
            study.current_version_id = version.id
        session = InterviewSession(
            study_id=study.id,
            study_version_id=version.id,
            participant_code=f"P-{suffix}",
            status="completed",
            retention="permanent",
            expires_at=None,
        )
        db.add(session)
        db.flush()
        turn = Turn(
            session_id=session.id,
            seq=1,
            role="participant",
            input_mode="voice",
            status="confirmed",
            finalized_chunks=[{"seq": 0, "sha256": _sha(b"chunk")}],
        )
        db.add(turn)
        db.flush()
        original = Revision(turn_id=turn.id, text="original", source="asr", editor="system")
        db.add(original)
        db.flush()
        corrected = Revision(
            turn_id=turn.id,
            text="corrected answer",
            source="manual",
            editor="participant",
            previous_id=original.id,
        )
        db.add(corrected)
        db.flush()
        turn.revision_id = corrected.id
        asset = Asset(
            session_id=session.id,
            turn_id=turn.id,
            path=relative,
            mime_type="audio/wav",
            sha256=_sha(audio),
            byte_size=len(audio),
            duration_seconds=1.0,
            source="participant",
            retention="permanent",
            expires_at=None,
        )
        db.add(asset)
        db.flush()
        turn.audio_asset_id = asset.id
        db.add(Memory(session_id=session.id, version=1, through_seq=1, content={"topic": "answer"}))
        db.add(Event(session_id=session.id, seq=1, type="turn.confirmed", payload={"text": "secret"}))
        report = Report(
            session_id=session.id,
            version=1,
            source_revision=1,
            body={"finding": "corrected answer"},
            markdown="# report",
        )
        db.add(report)
        db.flush()
        citation = Citation(
            report_id=report.id,
            revision_id=corrected.id,
            turn_id=turn.id,
            start=0,
            end=9,
            quote="corrected",
        )
        db.add(citation)
        db.flush()
        return {
            "study_id": study.id,
            "version_id": version.id,
            "session_id": session.id,
            "turn_id": turn.id,
            "revision_id": corrected.id,
            "asset_id": asset.id,
            "asset_path": relative,
            "report_id": report.id,
            "citation_id": citation.id,
        }


def test_safe_atomic_write_rejects_escape_and_stops_when_space_is_reserved(storage_env, tmp_path):
    storage, database, settings = storage_env

    written = storage.write_file("assets/session/audio.bin", b"archive")

    assert written.read_bytes() == b"archive"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert stat.S_IMODE(written.parent.stat().st_mode) == 0o700
    with pytest.raises(AppError) as traversal:
        storage.safe_path("../outside.bin")
    assert traversal.value.code == "INVALID_PATH"
    outside = tmp_path / "outside"
    outside.mkdir()
    (settings.data_dir / "escape").symlink_to(outside, target_is_directory=True)
    with pytest.raises(AppError) as symlink:
        storage.safe_path("escape/stolen.bin")
    assert symlink.value.code == "INVALID_PATH"

    full_settings = settings.model_copy(update={"min_free_bytes": 10**30})
    from app.storage import Storage

    full = Storage(database, full_settings)
    with pytest.raises(AppError) as no_space:
        full.write_file("assets/new.bin", b"new")
    assert no_space.value.code == "STORAGE_FULL"
    assert written.read_bytes() == b"archive"


def test_delete_removes_content_keeps_accounting_and_external_tombstone(storage_env):
    storage, database, settings = storage_env
    ids = _seed_archive(database, settings)
    chunk_path = _write_fixture(settings, f"chunks/{ids['session_id']}/0.part", b"chunk")
    orphan_audio = _write_fixture(settings, f"audio/{ids['session_id']}/uncommitted.audio", b"late")
    with database.transaction() as db:
        db.add(
            UploadChunk(
                session_id=ids["session_id"],
                turn_id=ids["turn_id"],
                seq=0,
                sha256=_sha(b"chunk"),
                byte_size=5,
                path=str(chunk_path.relative_to(settings.data_dir)),
            )
        )
        db.add(
            Consent(
                session_id=ids["session_id"],
                version="v1",
                mode="voice",
                processing=True,
                permanent=True,
            )
        )
        db.add(
            Invite(
                study_version_id=ids["version_id"],
                token_hash="i" * 64,
                expires_at=time.time() + 100,
                session_id=ids["session_id"],
            )
        )
        db.add(
            Auth(
                token_hash="a" * 64,
                role="participant",
                subject_id=ids["session_id"],
                expires_at=time.time() + 100,
            )
        )
        job = Job(
            session_id=ids["session_id"],
            kind="asr",
            dedup_key=f"asr:{ids['session_id']}",
            status="running",
            payload={"transcript": "secret"},
            result={"answer": "secret"},
        )
        db.add(job)
        db.flush()
        db.add(
            Ledger(
                session_id=ids["session_id"],
                job_id=job.id,
                role="asr",
                provider="mock",
                model="mock",
                region="local",
                attempt=1,
                status="settled",
                usage={"input_tokens": 1},
                amount_micro=10,
                amount_source="provider",
                price_snapshot={"unit": 10},
                month="2026-09",
            )
        )
        db.add(
            Reservation(
                session_id=ids["session_id"],
                job_id=job.id,
                amount_micro=10,
                state="settled",
                month="2026-09",
            )
        )
        db.add(
            RequestRecord(
                scope="session:test",
                key="delete-me",
                body_hash="b" * 64,
                response={"secret": "yes"},
                session_id=ids["session_id"],
            )
        )

    assert storage.delete_session(ids["session_id"], withdrawn=True) == {
        "id": ids["session_id"],
        "status": "withdrawn",
    }
    assert storage.is_deleted(ids["session_id"])
    assert not (settings.data_dir / ids["asset_path"]).exists()
    assert not chunk_path.exists()
    assert not orphan_audio.exists()
    with pytest.raises(AppError) as late_write:
        storage.write_file(f"assets/{ids['session_id']}/late.wav", b"late provider result")
    assert late_write.value.code == "SESSION_DELETED"
    assert storage.deletion_ledger_path.parent == settings.data_dir.parent
    assert settings.data_dir not in storage.deletion_ledger_path.parents

    with database.read() as db:
        assert db.get(InterviewSession, ids["session_id"]) is None
        for model in (Turn, Revision, Asset, UploadChunk, Memory, Report, Citation, Event, Job, Consent):
            assert db.scalar(select(func.count()).select_from(model)) == 0
        assert db.scalar(select(func.count()).select_from(Auth)) == 0
        assert db.scalar(select(func.count()).select_from(Invite)) == 0
        assert db.scalar(select(func.count()).select_from(RequestRecord)) == 0
        assert db.scalar(select(func.count()).select_from(Ledger)) == 1
        assert db.scalar(select(func.count()).select_from(Reservation)) == 1
        assert db.get(StudyVersion, ids["version_id"]) is not None

    # Simulate a late worker resurrecting rows; startup reconciliation must remove them again.
    with database.transaction() as db:
        db.add(
            InterviewSession(
                id=ids["session_id"],
                study_id=ids["study_id"],
                study_version_id=ids["version_id"],
                participant_code="late",
                status="in_progress",
            )
        )
        db.flush()
        db.add(Event(session_id=ids["session_id"], seq=1, type="late", payload={"secret": True}))
    result = storage.reconcile_deletions()
    assert result["sessions"] == 1
    with database.read() as db:
        assert db.get(InterviewSession, ids["session_id"]) is None
        assert db.scalar(select(func.count()).select_from(Event)) == 0


def test_delete_empty_study_removes_versions_and_unredeemed_invites_but_keeps_other_studies(storage_env):
    storage, database, settings = storage_env
    other = _seed_archive(database, settings, "other-study")
    with database.transaction() as db:
        study = Study(title="Empty confidential study")
        db.add(study)
        db.flush()
        first = StudyVersion(study_id=study.id, number=1, config={"objective": "confidential one"})
        second = StudyVersion(study_id=study.id, number=2, config={"objective": "confidential two"})
        db.add_all([first, second])
        db.flush()
        study.current_version_id = second.id
        db.add(
            Invite(
                study_version_id=first.id,
                token_hash="e" * 64,
                expires_at=time.time() + 86400,
            )
        )
        study_id = study.id

    assert storage.delete_study(study_id) == {"id": study_id, "status": "deleted"}
    assert storage.delete_study(study_id) == {"id": study_id, "status": "deleted"}
    assert storage.study_deletion_ledger_path.parent == settings.data_dir.parent
    assert study_id in storage.study_deletion_ledger_path.read_text(encoding="utf-8")
    with database.read() as db:
        assert db.get(Study, study_id) is None
        assert not list(db.scalars(select(StudyVersion).where(StudyVersion.study_id == study_id)))
        assert db.scalar(select(func.count()).select_from(Invite)) == 0
        assert db.get(Study, other["study_id"]) is not None
        assert db.get(InterviewSession, other["session_id"]) is not None


def test_delete_study_removes_all_sessions_and_content_while_preserving_another_study(storage_env):
    storage, database, settings = storage_env
    first = _seed_archive(database, settings, "research-first")
    second = _seed_archive(
        database,
        settings,
        "research-second",
        (first["study_id"], first["version_id"]),
    )
    other = _seed_archive(database, settings, "research-other")
    with database.transaction() as db:
        db.add(
            Invite(
                study_version_id=first["version_id"],
                token_hash="u" * 64,
                expires_at=time.time() + 86400,
            )
        )

    result = storage.delete_study(first["study_id"])

    assert result == {"id": first["study_id"], "status": "deleted"}
    assert storage.is_deleted(first["session_id"])
    assert storage.is_deleted(second["session_id"])
    assert not (settings.data_dir / first["asset_path"]).exists()
    assert not (settings.data_dir / second["asset_path"]).exists()
    assert (settings.data_dir / other["asset_path"]).exists()
    with database.read() as db:
        assert db.get(Study, first["study_id"]) is None
        assert db.get(StudyVersion, first["version_id"]) is None
        assert db.get(InterviewSession, first["session_id"]) is None
        assert db.get(InterviewSession, second["session_id"]) is None
        assert db.get(Study, other["study_id"]) is not None
        assert db.get(InterviewSession, other["session_id"]) is not None
        assert db.scalar(select(func.count()).select_from(Revision)) == 2
        assert db.scalar(select(func.count()).select_from(Report)) == 1
        assert db.scalar(select(func.count()).select_from(Citation)) == 1


def test_reconcile_finishes_study_deletion_interrupted_after_external_tombstone(storage_env):
    storage, database, settings = storage_env
    deleted = _seed_archive(database, settings, "interrupted-study")
    other = _seed_archive(database, settings, "interrupted-other")
    record = {
        "study_id": deleted["study_id"],
        "deleted_at": time.time(),
        "status": "deleted",
    }
    storage.study_deletion_ledger_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")

    result = storage.reconcile_deletions()

    assert result["studies"] == 1
    assert storage.is_deleted(deleted["session_id"])
    with database.read() as db:
        assert db.get(Study, deleted["study_id"]) is None
        assert db.get(InterviewSession, deleted["session_id"]) is None
        assert db.get(Study, other["study_id"]) is not None
        assert db.get(InterviewSession, other["session_id"]) is not None


@pytest.mark.parametrize("age_days", [8, 91, 10 * 365])
def test_cleanup_preserves_multi_year_archive_and_live_dependencies(storage_env, age_days):
    storage, database, settings = storage_env
    kept = _seed_archive(database, settings, "years-old")
    old = time.time() - age_days * 86400
    os.utime(settings.data_dir / kept["asset_path"], (old, old))

    safe_chunk = _write_fixture(settings, f"chunks/{kept['session_id']}/safe.part", b"safe chunk")
    os.utime(safe_chunk, (old, old))
    pending = _seed_archive(database, settings, "pending")
    pending_chunk = _write_fixture(settings, f"chunks/{pending['session_id']}/pending.part", b"pending")
    os.utime(pending_chunk, (old, old))
    transcribing = _seed_archive(database, settings, "transcribing")
    transcribing_chunk = _write_fixture(
        settings, f"chunks/{transcribing['session_id']}/transcribing.part", b"transcribing"
    )
    os.utime(transcribing_chunk, (old, old))
    needed_temp = _write_fixture(settings, "temp/needed.tmp", b"needed")
    stale_temp = _write_fixture(settings, "temp/stale.tmp", b"stale")
    os.utime(needed_temp, (old, old))
    os.utime(stale_temp, (old, old))
    with database.transaction() as db:
        db.add(
            UploadChunk(
                session_id=kept["session_id"],
                turn_id=kept["turn_id"],
                seq=0,
                sha256=_sha(b"safe chunk"),
                byte_size=10,
                path=str(safe_chunk.relative_to(settings.data_dir)),
                created_at=old,
            )
        )
        pending_turn = db.get(Turn, pending["turn_id"])
        pending_turn.finalized_chunks = None
        db.add(
            UploadChunk(
                session_id=pending["session_id"],
                turn_id=pending["turn_id"],
                seq=0,
                sha256=_sha(b"pending"),
                byte_size=7,
                path=str(pending_chunk.relative_to(settings.data_dir)),
                created_at=old,
            )
        )
        db.add(
            Job(
                session_id=pending["session_id"],
                kind="asr",
                dedup_key=f"pending:{pending['session_id']}",
                status="external_status_unknown",
                payload={"input_path": "temp/needed.tmp"},
            )
        )
        transcribing_turn = db.get(Turn, transcribing["turn_id"])
        transcribing_turn.status = "transcribing"
        db.add(
            UploadChunk(
                session_id=transcribing["session_id"],
                turn_id=transcribing["turn_id"],
                seq=0,
                sha256=_sha(b"transcribing"),
                byte_size=12,
                path=str(transcribing_chunk.relative_to(settings.data_dir)),
                created_at=old,
            )
        )

    result = storage.cleanup(now=time.time())

    assert result["removed_files"] == 2
    assert not safe_chunk.exists()
    assert not stale_temp.exists()
    assert pending_chunk.exists()
    assert transcribing_chunk.exists()
    assert needed_temp.exists()
    assert (settings.data_dir / kept["asset_path"]).read_bytes() == b"permanent audio years-old"
    with database.read() as db:
        assert db.scalar(select(func.count()).select_from(UploadChunk)) == 2
        assert db.scalar(select(func.count()).select_from(Revision)) == 6
        assert db.scalar(select(func.count()).select_from(Report)) == 3
        assert db.scalar(select(func.count()).select_from(Citation)) == 3
        asset = db.get(Asset, kept["asset_id"])
        assert asset.retention == "permanent"
        assert asset.expires_at is None


def test_backup_restore_filters_deletion_after_backup_and_validates_links(storage_env, tmp_path):
    storage, database, settings = storage_env
    kept = _seed_archive(database, settings, "restore-kept")
    removed = _seed_archive(database, settings, "restore-deleted")

    backup = storage.backup()
    assert backup["counts"]["assets"] == 2
    backup_path = Path(backup["backup_path"])
    assert backup_path.is_dir()

    storage.delete_session(removed["session_id"], withdrawn=True)
    target = tmp_path / "isolated-restore"
    restored = storage.restore(backup_path, target)

    assert restored["counts"]["sessions"] == 1
    assert restored["deletions_applied"] == 1
    connection = sqlite3.connect(target / "probeflow.sqlite3")
    try:
        assert connection.execute("SELECT id FROM sessions").fetchall() == [(kept["session_id"],)]
        assert connection.execute("SELECT COUNT(*) FROM transcript_revisions").fetchone()[0] == 2
        assert connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM report_citations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (removed["session_id"],)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
    assert (target / kept["asset_path"]).read_bytes() == b"permanent audio restore-kept"
    assert not (target / removed["asset_path"]).exists()
    restored_ledger = tmp_path / "isolated-restore-deletions.jsonl"
    assert removed["session_id"] in restored_ledger.read_text(encoding="utf-8")


def test_restore_filters_study_deleted_after_backup_including_unredeemed_invites(storage_env, tmp_path):
    storage, database, settings = storage_env
    removed = _seed_archive(database, settings, "study-restore-deleted")
    removed_second = _seed_archive(
        database,
        settings,
        "study-restore-deleted-two",
        (removed["study_id"], removed["version_id"]),
    )
    kept = _seed_archive(database, settings, "study-restore-kept")
    with database.transaction() as db:
        db.add(
            Invite(
                study_version_id=removed["version_id"],
                token_hash="r" * 64,
                expires_at=time.time() + 86400,
            )
        )

    backup_path = Path(storage.backup()["backup_path"])
    storage.delete_study(removed["study_id"])
    target = tmp_path / "study-filtered-restore"
    restored = storage.restore(backup_path, target)

    assert restored["study_deletions_applied"] == 1
    connection = sqlite3.connect(target / "probeflow.sqlite3")
    try:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM studies WHERE id = ?", (removed["study_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM study_versions WHERE study_id = ?", (removed["study_id"],)
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE id IN (?, ?)",
                (removed["session_id"], removed_second["session_id"]),
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0
        assert (
            connection.execute("SELECT COUNT(*) FROM studies WHERE id = ?", (kept["study_id"],)).fetchone()[0]
            == 1
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM sessions WHERE id = ?", (kept["session_id"],)
            ).fetchone()[0]
            == 1
        )
    finally:
        connection.close()
    assert not (target / removed["asset_path"]).exists()
    assert not (target / removed_second["asset_path"]).exists()
    assert (target / kept["asset_path"]).exists()
    restored_study_ledger = tmp_path / "study-filtered-restore-study-deletions.jsonl"
    assert removed["study_id"] in restored_study_ledger.read_text(encoding="utf-8")


def test_restore_rejects_tampered_backup_before_opening_target(storage_env, tmp_path):
    storage, database, settings = storage_env
    ids = _seed_archive(database, settings, "tamper")
    backup_path = Path(storage.backup()["backup_path"])
    (backup_path / "files" / ids["asset_path"]).write_bytes(b"tampered")
    target = tmp_path / "tampered-restore"

    with pytest.raises(AppError) as corrupted:
        storage.restore(backup_path, target)

    assert corrupted.value.code == "BACKUP_INVALID"
    assert not target.exists() or not any(target.iterdir())


def test_restore_rejects_citation_that_no_longer_matches_its_revision(storage_env, tmp_path):
    storage, database, settings = storage_env
    _seed_archive(database, settings, "citation-corruption")
    backup_path = Path(storage.backup()["backup_path"])
    database_copy = backup_path / "probeflow.sqlite3"
    connection = sqlite3.connect(database_copy)
    try:
        connection.execute("UPDATE report_citations SET quote = 'fabricated'")
        connection.commit()
    finally:
        connection.close()
    manifest_path = backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["database"]["sha256"] = _sha(database_copy.read_bytes())
    manifest["database"]["byte_size"] = database_copy.stat().st_size
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AppError) as corrupted:
        storage.restore(backup_path, tmp_path / "bad-citation-restore")

    assert corrupted.value.code == "RESTORE_INVALID"


def test_backup_validation_rejects_manifest_that_omits_database_file_reference(storage_env, tmp_path):
    storage, database, settings = storage_env
    _seed_archive(database, settings, "missing-manifest-reference")
    backup_path = Path(storage.backup()["backup_path"])
    manifest_path = backup_path / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = []
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(AppError) as corrupted:
        storage.restore(backup_path, tmp_path / "missing-reference-restore")

    assert corrupted.value.code == "BACKUP_INVALID"


def test_backup_rotation_keeps_seven_independent_points_and_primary(storage_env):
    storage, database, settings = storage_env
    ids = _seed_archive(database, settings, "rotation")
    created = [Path(storage.backup()["backup_path"]) for _ in range(8)]

    retained = sorted(path for path in settings.backups.iterdir() if path.name.startswith("backup-"))
    assert len(retained) == 7
    assert created[0] not in retained
    assert created[-1] in retained
    for backup_path in retained:
        manifest = json.loads((backup_path / "manifest.json").read_text(encoding="utf-8"))
        entry = next(item for item in manifest["files"] if item["path"] == ids["asset_path"])
        archived = backup_path / "files" / ids["asset_path"]
        assert archived.read_bytes() == b"permanent audio rotation"
        assert _sha(archived.read_bytes()) == entry["sha256"]
    assert (settings.data_dir / ids["asset_path"]).read_bytes() == b"permanent audio rotation"


def test_diagnostics_separates_primary_temp_and_backup_usage(storage_env):
    storage, database, settings = storage_env
    _seed_archive(database, settings, "diagnostics")
    _write_fixture(settings, "temp/cache.tmp", b"temporary")
    storage.backup()

    result = storage.diagnostics()

    assert result["primary_bytes"] >= len(b"permanent audio diagnostics")
    assert result["temp_bytes"] >= len(b"temporary")
    assert result["backup_bytes"] > 0
    assert result["free_bytes"] > 0
    assert result["last_backup"]["status"] == "ok"


def test_backup_failure_remains_visible_after_recreating_storage(storage_env):
    from app.storage import Storage

    storage, database, settings = storage_env
    archive = _seed_archive(database, settings, "failed-backup")
    storage.backup()
    (settings.data_dir / archive["asset_path"]).unlink()
    with pytest.raises(AppError, match="备份引用的文件不存在"):
        storage.backup()
    reloaded = Storage(Database(settings), settings)
    result = reloaded.diagnostics()["last_backup"]
    assert result["status"] == "failed" and result["code"] == "BACKUP_INVALID"


def test_manual_backups_do_not_rotate_away_daily_recovery_points(storage_env):
    storage, database, settings = storage_env
    _seed_archive(database, settings, "daily-rotation")
    daily = [Path(storage.backup(kind="daily")["backup_path"]) for _ in range(8)]
    manual = [Path(storage.backup()["backup_path"]) for _ in range(8)]
    assert not daily[0].exists() and not manual[0].exists()
    assert all(path.exists() for path in daily[1:] + manual[1:])


def test_backup_preserves_inflight_audio_checkpoint(storage_env, tmp_path):
    storage, database, settings = storage_env
    kept = _seed_archive(database, settings, "checkpoint")
    relative = f"tmp/{kept['session_id']}/tts/pending.audio"
    content = b"fictional checkpoint bytes"
    storage.write_file(relative, content)
    with database.transaction() as db:
        db.add(
            Job(
                session_id=kept["session_id"],
                kind="tts",
                dedup_key="checkpoint",
                status="running",
                result={
                    "_checkpoints": {
                        "tts": {
                            "type": "audio",
                            "path": relative,
                            "sha256": _sha(content),
                            "byte_size": len(content),
                            "mime_type": "audio/wav",
                            "usage": {},
                        }
                    }
                },
            )
        )
    backup = storage.backup()
    target = tmp_path / "checkpoint-restore"
    storage.restore(Path(backup["backup_path"]), target)
    assert (target / relative).read_bytes() == content
