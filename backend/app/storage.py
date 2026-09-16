"""Durable file, retention, deletion-ledger, and offline recovery services."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterable
from uuid import uuid4

from sqlalchemy import delete, select

from app.config import ROOT
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
    Memory,
    Report,
    RequestRecord,
    Revision,
    Study,
    StudyVersion,
    Turn,
    UploadChunk,
)


_BACKUP_SCHEMA = 2
_TERMINAL_JOB_STATES = {"succeeded", "completed", "cancelled", "canceled"}
# Export downloads have no persisted lease model in V1.1 yet, so cleanup leaves
# their cache alone; explicit session deletion still removes its export subtree.
_TEMP_ROOTS = ("temp", "tmp", "transcodes", "segments", "cache")
_SESSION_ROOTS = (*_TEMP_ROOTS, "exports", "chunks", "assets", "audio")


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, UTC).isoformat()


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_bytes(root: Path) -> int:
    if not root.exists() or root.is_symlink():
        return 0
    total = 0
    for current, directories, files in os.walk(root, followlinks=False):
        directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
        for name in files:
            path = Path(current) / name
            try:
                if not path.is_symlink():
                    total += path.stat().st_size
            except OSError:
                continue
    return total


def _nested_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _nested_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _nested_strings(item)


def _contains_identifier(value: Any, identifiers: set[str]) -> bool:
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, dict):
        return any(_contains_identifier(item, identifiers) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_identifier(item, identifiers) for item in value)
    return False


def _checkpoint_rows(connection):
    rows = []
    for job_id, raw in connection.execute("SELECT id, result FROM jobs WHERE result IS NOT NULL"):
        result = json.loads(raw) if isinstance(raw, str) else raw
        for key, value in (result or {}).get("_checkpoints", {}).items():
            if value.get("type") == "audio":
                rows.append(
                    ("job_checkpoint", f"{job_id}:{key}", value["path"], value["sha256"], value["byte_size"])
                )
    return rows


class Storage:
    """One serialization boundary for file writes, deletion, cleanup and backup."""

    def __init__(self, database, settings):
        self.database = database
        self.settings = settings
        self.data_dir = settings.data_dir.expanduser().resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self.deletion_ledger_path = self.data_dir.parent / f"{self.data_dir.name}-deletions.jsonl"
        self.study_deletion_ledger_path = self.data_dir.parent / f"{self.data_dir.name}-study-deletions.jsonl"
        self._last_backup: dict[str, Any] | None = None

    def safe_path(self, relative) -> Path:
        """Resolve a relative archive path without allowing lexical or symlink escape."""
        raw = Path(relative)
        if raw.is_absolute() or not raw.parts or str(relative).find("\x00") >= 0:
            raise AppError("INVALID_PATH", "文件路径必须位于数据目录内", 400)
        try:
            candidate = (self.data_dir / raw).resolve(strict=False)
            candidate.relative_to(self.data_dir)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AppError("INVALID_PATH", "文件路径越出数据目录", 400) from exc
        if candidate == self.data_dir:
            raise AppError("INVALID_PATH", "文件路径不能是数据目录本身", 400)
        return candidate

    def ensure_space(self, needed: int = 0) -> None:
        if not isinstance(needed, int) or needed < 0:
            raise AppError("INVALID_INPUT", "所需空间必须是非负整数", 400)
        try:
            free = shutil.disk_usage(self.data_dir).free
        except OSError as exc:
            raise AppError("STORAGE_UNAVAILABLE", "无法读取存储空间状态", 503, True) from exc
        if free - needed < self.settings.min_free_bytes:
            raise AppError("STORAGE_FULL", "可用空间不足，已停止新的文件写入", 507, True)

    def write_file(self, relative, content: bytes) -> Path:
        if not isinstance(content, bytes):
            raise AppError("INVALID_INPUT", "文件内容必须是字节数据", 400)
        with self.database.lock:
            deleted_ids = self._read_ledger()
            if any(part in deleted_ids for part in Path(relative).parts):
                raise AppError("SESSION_DELETED", "访谈已删除，不能写入迟到结果", 409)
            path = self.safe_path(relative)
            self.ensure_space(len(content))
            self._secure_mkdir(path.parent)
            descriptor = -1
            temporary: Path | None = None
            try:
                descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
                temporary = Path(name)
                os.fchmod(descriptor, 0o600)
                with os.fdopen(descriptor, "wb") as output:
                    descriptor = -1
                    output.write(content)
                    output.flush()
                    os.fsync(output.fileno())
                if self.safe_path(relative) != path:
                    raise AppError("INVALID_PATH", "文件路径在写入期间发生变化", 409)
                os.replace(temporary, path)
                os.chmod(path, 0o600)
                self._fsync_directory(path.parent)
                return path
            except AppError:
                raise
            except OSError as exc:
                if exc.errno in {errno.ENOSPC, errno.EDQUOT}:
                    raise AppError("STORAGE_FULL", "可用空间不足，文件未安全落盘", 507, True) from exc
                raise AppError("STORAGE_WRITE_FAILED", "文件未能安全落盘", 500, True) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary is not None:
                    temporary.unlink(missing_ok=True)

    def is_deleted(self, session_id: str) -> bool:
        with self.database.lock:
            return session_id in self._read_ledger()

    def delete_session(self, session_id: str, withdrawn: bool = False) -> dict[str, str]:
        if not session_id:
            raise AppError("INVALID_INPUT", "缺少访谈标识", 400)
        requested_status = "withdrawn" if withdrawn else "deleted"
        with self.database.lock:
            tombstones = self._read_ledger()
            if session_id in tombstones:
                return {"id": session_id, "status": tombstones[session_id]["status"]}
            with self.database.read() as reader:
                if reader.get(InterviewSession, session_id) is None:
                    raise AppError("NOT_FOUND", "访谈不存在或已删除", 404)

            tombstones[session_id] = {
                "session_id": session_id,
                "deleted_at": time.time(),
                "status": requested_status,
            }
            # Commit the fail-closed tombstone first. A crash after this point is
            # completed by reconcile_deletions and cannot resurrect the session.
            self._write_ledger(tombstones, self.deletion_ledger_path)
            paths: set[str] = set()
            with self.database.transaction() as db:
                paths.update(self._delete_session_rows(db, session_id))
            self._remove_unreferenced_paths(paths)
            self._remove_session_directories(session_id)
            return {"id": session_id, "status": requested_status}

    def delete_study(self, study_id: str) -> dict[str, str]:
        if not study_id:
            raise AppError("INVALID_INPUT", "缺少研究标识", 400)
        with self.database.lock:
            studies = self._read_study_ledger()
            if study_id in studies:
                return {"id": study_id, "status": "deleted"}
            with self.database.read() as reader:
                if reader.get(Study, study_id) is None:
                    raise AppError("NOT_FOUND", "研究不存在或已删除", 404)
                session_ids = list(
                    reader.scalars(select(InterviewSession.id).where(InterviewSession.study_id == study_id))
                )
            record = {"study_id": study_id, "deleted_at": time.time(), "status": "deleted"}
            studies[study_id] = record
            # The research tombstone is the first durable step. If any later
            # operation stops, startup reconciliation derives all session
            # tombstones from the still-present database and completes it.
            self._write_study_ledger(studies, self.study_deletion_ledger_path)
            sessions = self._read_ledger()
            for session_id in session_ids:
                current = sessions.get(session_id)
                if current is None or record["deleted_at"] >= current["deleted_at"]:
                    sessions[session_id] = {
                        "session_id": session_id,
                        "deleted_at": record["deleted_at"],
                        "status": "deleted",
                    }
            self._write_ledger(sessions, self.deletion_ledger_path)
            with self.database.transaction() as db:
                paths, deleted_session_ids = self._delete_study_rows(db, study_id)
            self._remove_unreferenced_paths(paths)
            for session_id in deleted_session_ids:
                self._remove_session_directories(session_id)
            return {"id": study_id, "status": "deleted"}

    def reconcile_deletions(self) -> dict[str, int]:
        with self.database.lock:
            tombstones = self._read_ledger()
            study_tombstones = self._read_study_ledger()
            with self.database.read() as reader:
                study_session_ids = {
                    study_id: list(
                        reader.scalars(
                            select(InterviewSession.id).where(InterviewSession.study_id == study_id)
                        )
                    )
                    for study_id in study_tombstones
                }
                deleted_sessions = len(
                    {
                        session_id
                        for session_id in tombstones
                        if reader.get(InterviewSession, session_id) is not None
                    }
                    | {session_id for values in study_session_ids.values() for session_id in values}
                )
                deleted_studies = sum(
                    reader.get(Study, study_id) is not None for study_id in study_tombstones
                )
            ledger_changed = False
            for study_id, session_ids in study_session_ids.items():
                study_record = study_tombstones[study_id]
                for session_id in session_ids:
                    current = tombstones.get(session_id)
                    if current is None or study_record["deleted_at"] >= current["deleted_at"]:
                        tombstones[session_id] = {
                            "session_id": session_id,
                            "deleted_at": study_record["deleted_at"],
                            "status": "deleted",
                        }
                        ledger_changed = True
            if ledger_changed:
                self._write_ledger(tombstones, self.deletion_ledger_path)
            paths: set[str] = set()
            with self.database.transaction() as db:
                for study_id in study_tombstones:
                    study_paths, _ = self._delete_study_rows(db, study_id)
                    paths.update(study_paths)
                for session_id in tombstones:
                    paths.update(self._delete_session_rows(db, session_id))
            self._remove_unreferenced_paths(paths)
            for session_id in tombstones:
                self._remove_session_directories(session_id)
            return {
                "studies": deleted_studies,
                "sessions": deleted_sessions,
                "tombstones": len(tombstones),
                "study_tombstones": len(study_tombstones),
            }

    def cleanup(self, now: float | None = None) -> dict[str, int]:
        now = time.time() if now is None else now
        cutoff = now - self.settings.temp_retention_hours * 3600
        reconciled = self.reconcile_deletions()
        removed_files = 0
        removed_bytes = 0
        removed_chunks = 0
        with self.database.lock, self.database.transaction() as db:
            assets = list(db.scalars(select(Asset)))
            asset_paths = {asset.path for asset in assets}
            assets_by_turn = {asset.turn_id: asset for asset in assets if asset.turn_id}
            jobs = list(db.scalars(select(Job)))
            live_jobs = [job for job in jobs if job.status not in _TERMINAL_JOB_STATES]
            live_sessions = {job.session_id for job in live_jobs if job.session_id}
            referenced_job_paths = {
                value
                for job in live_jobs
                for value in (*_nested_strings(job.payload), *_nested_strings(job.result))
            }
            chunks = list(db.scalars(select(UploadChunk)))
            for chunk in chunks:
                path = self._optional_safe_path(chunk.path)
                timestamp = max(chunk.created_at, self._mtime(path)) if path else chunk.created_at
                if timestamp > cutoff or chunk.session_id in live_sessions:
                    continue
                turn = db.get(Turn, chunk.turn_id)
                asset = assets_by_turn.get(chunk.turn_id)
                if (
                    not turn
                    or turn.finalized_chunks is None
                    or turn.status not in {"confirming", "confirmed", "superseded"}
                    or not asset
                    or not self._asset_valid(asset)
                ):
                    continue
                if chunk.path in referenced_job_paths:
                    continue
                size = self._unlink_file(path)
                if size is not None:
                    removed_files += 1
                    removed_bytes += size
                db.delete(chunk)
                removed_chunks += 1

            remaining_chunks = {row.path for row in db.scalars(select(UploadChunk))}
            protected = asset_paths | remaining_chunks | referenced_job_paths
            for root_name in _TEMP_ROOTS:
                root = self.safe_path(root_name)
                if not root.exists() or root.is_symlink():
                    continue
                for current, directories, files in os.walk(root, topdown=True, followlinks=False):
                    directories[:] = [name for name in directories if not (Path(current) / name).is_symlink()]
                    for name in files:
                        path = Path(current) / name
                        try:
                            relative = str(path.relative_to(self.data_dir))
                            if relative in protected or path.is_symlink() or path.stat().st_mtime > cutoff:
                                continue
                        except (OSError, ValueError):
                            continue
                        size = self._unlink_file(path)
                        if size is not None:
                            removed_files += 1
                            removed_bytes += size
                self._remove_empty_directories(root)
        return {
            "removed_files": removed_files,
            "removed_bytes": removed_bytes,
            "removed_chunks": removed_chunks,
            "deleted_sessions": reconciled["sessions"],
        }

    def backup(self, kind: str = "manual") -> dict[str, Any]:
        if kind not in {"manual", "daily"}:
            raise AppError("BACKUP_INVALID", "备份类别无效", 400)
        backup_root = self.settings.backups.expanduser().resolve()
        if backup_root == self.data_dir or self.data_dir in backup_root.parents:
            raise AppError("BACKUP_INVALID", "备份目录必须独立于主档案目录", 400)
        self.reconcile_deletions()
        self._secure_mkdir(backup_root)
        os.chmod(backup_root, 0o700)
        created = time.time()
        name = f"backup-{time.time_ns()}-{uuid4().hex[:8]}"
        staging = backup_root / f".incomplete-{uuid4().hex}"
        final = backup_root / name
        counts: dict[str, int] = {}
        try:
            with self.database.lock:
                staging.mkdir(mode=0o700)
                database_copy = staging / "probeflow.sqlite3"
                self._sqlite_backup(self.database.path, database_copy)
                file_entries = self._copy_referenced_files(database_copy, staging / "files")
                tombstones = self._read_ledger()
                ledger_copy = staging / "deletion-ledger.jsonl"
                self._write_ledger(tombstones, ledger_copy)
                study_tombstones = self._read_study_ledger()
                study_ledger_copy = staging / "study-deletion-ledger.jsonl"
                self._write_study_ledger(study_tombstones, study_ledger_copy)
                counts = self._database_counts(database_copy)
                counts["files"] = len(file_entries)
                counts["bytes"] = sum(entry["byte_size"] for entry in file_entries)
                manifest = {
                    "schema": _BACKUP_SCHEMA,
                    "kind": kind,
                    "created_at": _iso(created),
                    "database": {
                        "path": "probeflow.sqlite3",
                        "sha256": _hash_file(database_copy),
                        "byte_size": database_copy.stat().st_size,
                    },
                    "deletion_ledger": {
                        "path": "deletion-ledger.jsonl",
                        "sha256": _hash_file(ledger_copy),
                        "entries": len(tombstones),
                    },
                    "study_deletion_ledger": {
                        "path": "study-deletion-ledger.jsonl",
                        "sha256": _hash_file(study_ledger_copy),
                        "entries": len(study_tombstones),
                    },
                    "files": file_entries,
                    "counts": counts,
                }
                manifest_path = staging / "manifest.json"
                self._write_json(manifest_path, manifest)
                self._protect_tree(staging)
                self._validate_backup_package(staging)
                os.replace(staging, final)
                self._fsync_directory(backup_root)
            self._rotate_backups(backup_root, kind)
            result = {"backup_path": str(final), "created_at": _iso(created), "counts": counts, "kind": kind}
            self._record_backup_status({"status": "ok", **result})
            return result
        except AppError as exc:
            self._record_backup_status(
                {"status": "failed", "created_at": _iso(created), "code": exc.code, "kind": kind}
            )
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except (OSError, sqlite3.Error) as exc:
            self._record_backup_status(
                {"status": "failed", "created_at": _iso(created), "code": "BACKUP_FAILED", "kind": kind}
            )
            shutil.rmtree(staging, ignore_errors=True)
            raise AppError("BACKUP_FAILED", "备份创建或校验失败", 500, True) from exc

    def restore(self, backup_path: Path, target_dir: Path) -> dict[str, Any]:
        requested_source = Path(backup_path).expanduser()
        requested_target = Path(target_dir).expanduser()
        if requested_source.is_symlink() or requested_target.is_symlink():
            raise AppError("RESTORE_TARGET_INVALID", "备份和恢复目录不能是符号链接", 400)
        source = requested_source.resolve()
        manifest = self._validate_backup_package(source)
        target = requested_target.resolve(strict=False)
        self._validate_restore_target(source, target)
        parent = target.parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        staging = parent / f".{target.name}.restore-{uuid4().hex}"
        target_ledger = parent / f"{target.name}-deletions.jsonl"
        target_study_ledger = parent / f"{target.name}-study-deletions.jsonl"
        try:
            staging.mkdir(mode=0o700)
            shutil.copyfile(source / manifest["database"]["path"], staging / "probeflow.sqlite3")
            os.chmod(staging / "probeflow.sqlite3", 0o600)
            for entry in manifest["files"]:
                relative = entry["path"]
                archived = self._safe_join(source / "files", relative)
                destination = self._safe_join(staging, relative)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(archived, destination)
                os.chmod(destination, 0o600)

            tombstones = self._merge_ledgers(
                self._read_ledger_file(source / manifest["deletion_ledger"]["path"]),
                self._read_ledger(),
            )
            if target_ledger.exists():
                tombstones = self._merge_ledgers(tombstones, self._read_ledger_file(target_ledger))
            study_tombstones: dict[str, dict[str, Any]] = {}
            study_ledger_meta = manifest.get("study_deletion_ledger")
            if study_ledger_meta:
                study_tombstones = self._read_study_ledger_file(source / study_ledger_meta["path"])
            study_tombstones = self._merge_ledgers(study_tombstones, self._read_study_ledger())
            if target_study_ledger.exists():
                study_tombstones = self._merge_ledgers(
                    study_tombstones, self._read_study_ledger_file(target_study_ledger)
                )
            deleted_paths: set[str] = set()
            applied = 0
            applied_studies = 0
            connection = sqlite3.connect(staging / "probeflow.sqlite3")
            try:
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("BEGIN IMMEDIATE")
                for study_id, study_record in study_tombstones.items():
                    exists = connection.execute("SELECT 1 FROM studies WHERE id = ?", (study_id,)).fetchone()
                    if exists:
                        applied_studies += 1
                    study_paths, session_ids = self._delete_study_sqlite(connection, study_id)
                    deleted_paths.update(study_paths)
                    for session_id in session_ids:
                        current = tombstones.get(session_id)
                        if current is None or study_record["deleted_at"] >= current["deleted_at"]:
                            tombstones[session_id] = {
                                "session_id": session_id,
                                "deleted_at": study_record["deleted_at"],
                                "status": "deleted",
                            }
                for session_id in tombstones:
                    exists = connection.execute(
                        "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
                    ).fetchone()
                    if exists:
                        applied += 1
                    deleted_paths.update(self._delete_session_sqlite(connection, session_id))
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
            finally:
                connection.close()
            for relative in deleted_paths:
                path = self._optional_join(staging, relative)
                if path:
                    self._unlink_file(path)
            for session_id in tombstones:
                self._remove_session_directories(session_id, root=staging)
            counts = self._validate_restored_archive(staging)
            live_paths = self._database_file_paths(staging / "probeflow.sqlite3")
            for entry in manifest["files"]:
                if entry["path"] not in live_paths:
                    path = self._optional_join(staging, entry["path"])
                    if path:
                        self._unlink_file(path)
            self._protect_tree(staging)
            if target.exists():
                target.rmdir()
            os.replace(staging, target)
            self._write_ledger(tombstones, target_ledger)
            self._write_study_ledger(study_tombstones, target_study_ledger)
            return {
                "target_dir": str(target),
                "created_at": manifest["created_at"],
                "deletions_applied": applied,
                "study_deletions_applied": applied_studies,
                "deletion_ledger_path": str(target_ledger),
                "study_deletion_ledger_path": str(target_study_ledger),
                "counts": counts,
            }
        except AppError:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        except (OSError, sqlite3.Error) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise AppError("RESTORE_FAILED", "隔离恢复或校验失败", 500) from exc

    def diagnostics(self) -> dict[str, Any]:
        primary = _tree_bytes(self.data_dir)
        temporary = sum(_tree_bytes(self.data_dir / name) for name in (*_TEMP_ROOTS, "chunks"))
        backups = _tree_bytes(self.settings.backups.expanduser().resolve())
        try:
            free = shutil.disk_usage(self.data_dir).free
        except OSError:
            free = 0
        last = self._read_backup_status() or self._discover_last_backup()
        try:
            tombstones = len(self._read_ledger())
            study_tombstones = len(self._read_study_ledger())
        except AppError:
            tombstones = None
            study_tombstones = None
        return {
            "status": "full" if free < self.settings.min_free_bytes else "ok",
            "primary_bytes": primary,
            "formal_bytes": max(0, primary - temporary),
            "temp_bytes": temporary,
            "backup_bytes": backups,
            "free_bytes": free,
            "minimum_free_bytes": self.settings.min_free_bytes,
            "deletion_tombstones": tombstones,
            "study_deletion_tombstones": study_tombstones,
            "last_backup": last,
        }

    def _record_backup_status(self, status: dict[str, Any]) -> None:
        self._last_backup = status
        self.database.last_backup_status = status
        try:
            self.write_file("maintenance/backup-status.json", json.dumps(status).encode())
        except AppError:
            # Full/unavailable disks may also prevent writing the status file.
            # Preserve the current process's failure evidence without masking it.
            pass

    def _read_backup_status(self) -> dict[str, Any] | None:
        cached = self._last_backup or getattr(self.database, "last_backup_status", None)
        if cached:
            return cached
        path = self.safe_path("maintenance/backup-status.json")
        if not path.exists():
            return None
        try:
            result = json.loads(path.read_text())
            if not isinstance(result, dict) or result.get("status") not in {"ok", "failed"}:
                raise ValueError
            return result
        except (OSError, ValueError):
            return {"status": "failed", "code": "BACKUP_STATUS_UNREADABLE"}

    def _secure_mkdir(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        current = path
        while current == self.data_dir or self.data_dir in current.parents:
            if current.exists() and not current.is_symlink():
                os.chmod(current, 0o700)
            if current == self.data_dir:
                break
            current = current.parent

    @staticmethod
    def _protect_tree(root: Path) -> None:
        os.chmod(root, 0o700)
        for current, directories, files in os.walk(root, followlinks=False):
            for name in directories:
                path = Path(current) / name
                if not path.is_symlink():
                    os.chmod(path, 0o700)
            for name in files:
                path = Path(current) / name
                if not path.is_symlink():
                    os.chmod(path, 0o600)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError:
            pass

    def _read_ledger(self) -> dict[str, dict[str, Any]]:
        return self._read_ledger_file(self.deletion_ledger_path)

    def _read_study_ledger(self) -> dict[str, dict[str, Any]]:
        return self._read_study_ledger_file(self.study_deletion_ledger_path)

    @staticmethod
    def _read_ledger_file(path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        if path.is_symlink() or not path.is_file():
            raise AppError("DELETION_LEDGER_INVALID", "删除清单不是受保护的普通文件", 500)
        records: dict[str, dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    session_id = record.get("session_id")
                    status = record.get("status")
                    deleted_at = record.get("deleted_at")
                    if (
                        not isinstance(session_id, str)
                        or not session_id
                        or status not in {"deleted", "withdrawn"}
                    ):
                        raise ValueError
                    if not isinstance(deleted_at, (int, float)):
                        raise ValueError
                    previous = records.get(session_id)
                    if previous is None or deleted_at >= previous["deleted_at"]:
                        records[session_id] = {
                            "session_id": session_id,
                            "deleted_at": float(deleted_at),
                            "status": status,
                        }
            return records
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AppError("DELETION_LEDGER_INVALID", "删除清单损坏，系统已停止恢复访问", 500) from exc

    @staticmethod
    def _read_study_ledger_file(path: Path) -> dict[str, dict[str, Any]]:
        if not path.exists():
            return {}
        if path.is_symlink() or not path.is_file():
            raise AppError("DELETION_LEDGER_INVALID", "研究删除清单不是受保护的普通文件", 500)
        records: dict[str, dict[str, Any]] = {}
        try:
            with path.open("r", encoding="utf-8") as source:
                for line in source:
                    if not line.strip():
                        continue
                    record = json.loads(line)
                    study_id = record.get("study_id")
                    deleted_at = record.get("deleted_at")
                    if (
                        not isinstance(study_id, str)
                        or not study_id
                        or record.get("status") != "deleted"
                        or not isinstance(deleted_at, (int, float))
                    ):
                        raise ValueError
                    previous = records.get(study_id)
                    if previous is None or deleted_at >= previous["deleted_at"]:
                        records[study_id] = {
                            "study_id": study_id,
                            "deleted_at": float(deleted_at),
                            "status": "deleted",
                        }
            return records
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AppError("DELETION_LEDGER_INVALID", "研究删除清单损坏，系统已停止恢复访问", 500) from exc

    @staticmethod
    def _write_ledger(records: dict[str, dict[str, Any]], path: Path) -> None:
        parent_existed = path.parent.exists()
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.is_symlink():
            raise AppError("DELETION_LEDGER_INVALID", "删除清单目录不能是符号链接", 500)
        if not parent_existed:
            os.chmod(path.parent, 0o700)
        descriptor, name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        temporary = Path(name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                descriptor = -1
                for session_id in sorted(records):
                    output.write(json.dumps(records[session_id], ensure_ascii=False, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _write_study_ledger(records: dict[str, dict[str, Any]], path: Path) -> None:
        Storage._write_ledger(records, path)

    @staticmethod
    def _merge_ledgers(*ledgers: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for ledger in ledgers:
            for session_id, record in ledger.items():
                current = merged.get(session_id)
                if current is None or record["deleted_at"] >= current["deleted_at"]:
                    merged[session_id] = record
        return merged

    def _delete_session_rows(self, db, session_id: str) -> set[str]:
        turn_ids = list(db.scalars(select(Turn.id).where(Turn.session_id == session_id)))
        report_ids = list(db.scalars(select(Report.id).where(Report.session_id == session_id)))
        revision_ids = (
            list(db.scalars(select(Revision.id).where(Revision.turn_id.in_(turn_ids)))) if turn_ids else []
        )
        asset_paths = set(db.scalars(select(Asset.path).where(Asset.session_id == session_id)))
        chunk_paths = set(db.scalars(select(UploadChunk.path).where(UploadChunk.session_id == session_id)))
        if report_ids or turn_ids or revision_ids:
            conditions = []
            if report_ids:
                conditions.append(Citation.report_id.in_(report_ids))
            if turn_ids:
                conditions.append(Citation.turn_id.in_(turn_ids))
            if revision_ids:
                conditions.append(Citation.revision_id.in_(revision_ids))
            for condition in conditions:
                db.execute(delete(Citation).where(condition))
        db.execute(delete(Report).where(Report.session_id == session_id))
        if turn_ids:
            db.execute(delete(Revision).where(Revision.turn_id.in_(turn_ids)))
        db.execute(delete(Asset).where(Asset.session_id == session_id))
        db.execute(delete(UploadChunk).where(UploadChunk.session_id == session_id))
        db.execute(delete(Memory).where(Memory.session_id == session_id))
        db.execute(delete(Event).where(Event.session_id == session_id))
        db.execute(delete(Job).where(Job.session_id == session_id))
        db.execute(delete(RequestRecord).where(RequestRecord.session_id == session_id))
        db.execute(delete(Consent).where(Consent.session_id == session_id))
        db.execute(delete(Invite).where(Invite.session_id == session_id))
        db.execute(delete(Auth).where(Auth.subject_id == session_id))
        db.execute(delete(Turn).where(Turn.session_id == session_id))
        db.execute(delete(InterviewSession).where(InterviewSession.id == session_id))
        return asset_paths | chunk_paths

    def _delete_study_rows(self, db, study_id: str) -> tuple[set[str], list[str]]:
        version_ids = list(db.scalars(select(StudyVersion.id).where(StudyVersion.study_id == study_id)))
        session_ids = list(
            db.scalars(select(InterviewSession.id).where(InterviewSession.study_id == study_id))
        )
        paths: set[str] = set()
        for session_id in session_ids:
            paths.update(self._delete_session_rows(db, session_id))
        study_jobs = [job for job in db.scalars(select(Job)) if job.payload.get("study_id") == study_id]
        identifiers = {study_id, *version_ids, *(job.id for job in study_jobs)}
        for job in study_jobs:
            db.delete(job)
        for record in db.scalars(select(RequestRecord).where(RequestRecord.response.is_not(None))):
            if _contains_identifier(record.response, identifiers):
                db.delete(record)
        if version_ids:
            db.execute(delete(Invite).where(Invite.study_version_id.in_(version_ids)))
        db.execute(delete(StudyVersion).where(StudyVersion.study_id == study_id))
        db.execute(delete(Study).where(Study.id == study_id))
        return paths, session_ids

    def _remove_unreferenced_paths(self, paths: Iterable[str]) -> None:
        with self.database.read() as db:
            referenced = set(db.scalars(select(Asset.path))) | set(db.scalars(select(UploadChunk.path)))
        for relative in set(paths) - referenced:
            path = self._optional_safe_path(relative)
            if path:
                self._unlink_file(path)

    def _remove_session_directories(self, session_id: str, root: Path | None = None) -> None:
        root = self.data_dir if root is None else root.resolve()
        for root_name in _SESSION_ROOTS:
            directory = self._optional_join(root, f"{root_name}/{session_id}")
            if not directory or not directory.exists() or directory.is_symlink():
                continue
            shutil.rmtree(directory)

    def _optional_safe_path(self, relative: str) -> Path | None:
        try:
            return self.safe_path(relative)
        except AppError:
            return None

    @staticmethod
    def _safe_join(root: Path, relative: str) -> Path:
        raw = Path(relative)
        base = root.resolve()
        if raw.is_absolute() or not raw.parts:
            raise AppError("BACKUP_INVALID", "备份包含不安全的文件路径", 400)
        try:
            candidate = (base / raw).resolve(strict=False)
            candidate.relative_to(base)
        except (OSError, RuntimeError, ValueError) as exc:
            raise AppError("BACKUP_INVALID", "备份文件路径越界", 400) from exc
        return candidate

    @classmethod
    def _optional_join(cls, root: Path, relative: str) -> Path | None:
        try:
            return cls._safe_join(root, relative)
        except AppError:
            return None

    @staticmethod
    def _mtime(path: Path | None) -> float:
        if path is None:
            return float("inf")
        try:
            return path.stat().st_mtime
        except OSError:
            return float("inf")

    @staticmethod
    def _unlink_file(path: Path | None) -> int | None:
        if path is None:
            return None
        try:
            if path.is_symlink() or not path.is_file():
                return None
            size = path.stat().st_size
            path.unlink()
            return size
        except OSError:
            return None

    @staticmethod
    def _remove_empty_directories(root: Path) -> None:
        if not root.exists() or root.is_symlink():
            return
        directories = [Path(current) for current, _, _ in os.walk(root, topdown=False, followlinks=False)]
        for directory in directories:
            try:
                directory.rmdir()
            except OSError:
                pass

    def _asset_valid(self, asset: Asset) -> bool:
        path = self._optional_safe_path(asset.path)
        try:
            return bool(
                asset.retention == "permanent"
                and asset.expires_at is None
                and path
                and path.is_file()
                and not path.is_symlink()
                and path.stat().st_size == asset.byte_size
                and _hash_file(path) == asset.sha256
            )
        except OSError:
            return False

    @staticmethod
    def _sqlite_backup(source: Path, destination: Path) -> None:
        source_connection = sqlite3.connect(source)
        destination_connection = sqlite3.connect(destination)
        try:
            source_connection.backup(destination_connection)
            destination_connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            destination_connection.commit()
        finally:
            destination_connection.close()
            source_connection.close()
        os.chmod(destination, 0o600)

    def _copy_referenced_files(self, database_copy: Path, files_root: Path) -> list[dict[str, Any]]:
        connection = sqlite3.connect(database_copy)
        try:
            rows: list[tuple[str, str, str, str, int]] = []
            rows.extend(
                ("asset", row[0], row[1], row[2], row[3])
                for row in connection.execute("SELECT id, path, sha256, byte_size FROM audio_assets")
            )
            rows.extend(
                ("upload_chunk", row[0], row[1], row[2], row[3])
                for row in connection.execute("SELECT id, path, sha256, byte_size FROM upload_chunks")
            )
            rows.extend(_checkpoint_rows(connection))
        finally:
            connection.close()
        entries: dict[str, dict[str, Any]] = {}
        for kind, identifier, relative, expected_hash, expected_size in rows:
            source = self.safe_path(relative)
            if source.is_symlink() or not source.is_file():
                raise AppError("BACKUP_INVALID", f"备份引用的文件不存在：{relative}", 409)
            actual_size = source.stat().st_size
            actual_hash = _hash_file(source)
            if actual_size != expected_size or actual_hash != expected_hash:
                raise AppError("BACKUP_INVALID", f"备份引用的文件校验失败：{relative}", 409)
            existing = entries.get(relative)
            if existing and (existing["sha256"], existing["byte_size"]) != (actual_hash, actual_size):
                raise AppError("BACKUP_INVALID", f"同一路径存在冲突引用：{relative}", 409)
            if not existing:
                destination = self._safe_join(files_root, relative)
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                shutil.copyfile(source, destination)
                os.chmod(destination, 0o600)
                existing = {
                    "path": relative,
                    "sha256": actual_hash,
                    "byte_size": actual_size,
                    "references": [],
                }
                entries[relative] = existing
            existing["references"].append({"kind": kind, "id": identifier})
        return [entries[key] for key in sorted(entries)]

    @staticmethod
    def _database_counts(path: Path) -> dict[str, int]:
        connection = sqlite3.connect(path)
        try:
            return {
                "sessions": connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0],
                "assets": connection.execute("SELECT COUNT(*) FROM audio_assets").fetchone()[0],
                "upload_chunks": connection.execute("SELECT COUNT(*) FROM upload_chunks").fetchone()[0],
                "revisions": connection.execute("SELECT COUNT(*) FROM transcript_revisions").fetchone()[0],
                "reports": connection.execute("SELECT COUNT(*) FROM reports").fetchone()[0],
                "citations": connection.execute("SELECT COUNT(*) FROM report_citations").fetchone()[0],
            }
        finally:
            connection.close()

    @staticmethod
    def _write_json(path: Path, value: dict[str, Any]) -> None:
        path.write_text(
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        os.chmod(path, 0o600)

    def _validate_backup_package(self, path: Path) -> dict[str, Any]:
        if not path.is_dir() or path.is_symlink():
            raise AppError("BACKUP_INVALID", "备份路径不是普通目录", 400)
        try:
            manifest_path = path / "manifest.json"
            if manifest_path.is_symlink():
                raise ValueError
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            schema = manifest.get("schema")
            if schema not in {1, _BACKUP_SCHEMA} or not isinstance(manifest.get("files"), list):
                raise ValueError
            database = self._safe_join(path, manifest["database"]["path"])
            ledger = self._safe_join(path, manifest["deletion_ledger"]["path"])
            if database.is_symlink() or ledger.is_symlink():
                raise ValueError
            if database.stat().st_size != manifest["database"]["byte_size"]:
                raise ValueError
            if _hash_file(database) != manifest["database"]["sha256"]:
                raise ValueError
            if _hash_file(ledger) != manifest["deletion_ledger"]["sha256"]:
                raise ValueError
            self._read_ledger_file(ledger)
            study_ledger_meta = manifest.get("study_deletion_ledger")
            if schema >= 2 and not study_ledger_meta:
                raise ValueError
            if study_ledger_meta:
                study_ledger = self._safe_join(path, study_ledger_meta["path"])
                if study_ledger.is_symlink() or _hash_file(study_ledger) != study_ledger_meta["sha256"]:
                    raise ValueError
                self._read_study_ledger_file(study_ledger)
            seen: set[str] = set()
            manifest_files: dict[str, dict[str, Any]] = {}
            for entry in manifest["files"]:
                relative = entry["path"]
                if relative in seen:
                    raise ValueError
                seen.add(relative)
                manifest_files[relative] = entry
                archived = self._safe_join(path / "files", relative)
                if archived.is_symlink() or archived.stat().st_size != entry["byte_size"]:
                    raise ValueError
                if _hash_file(archived) != entry["sha256"]:
                    raise ValueError
            connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
            try:
                if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError
                if connection.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError
                database_references: dict[str, dict[str, Any]] = {}
                reference_rows = [
                    ("asset", *row)
                    for row in connection.execute("SELECT id, path, sha256, byte_size FROM audio_assets")
                ] + [
                    ("upload_chunk", *row)
                    for row in connection.execute("SELECT id, path, sha256, byte_size FROM upload_chunks")
                ]
                reference_rows.extend(_checkpoint_rows(connection))
                for kind, identifier, relative, expected_hash, expected_size in reference_rows:
                    expected = database_references.setdefault(
                        relative,
                        {"sha256": expected_hash, "byte_size": expected_size, "references": set()},
                    )
                    if (expected["sha256"], expected["byte_size"]) != (expected_hash, expected_size):
                        raise ValueError
                    expected["references"].add((kind, identifier))
                if set(manifest_files) != set(database_references):
                    raise ValueError
                for relative, expected in database_references.items():
                    entry = manifest_files[relative]
                    actual_references = {
                        (reference.get("kind"), reference.get("id"))
                        for reference in entry.get("references", [])
                    }
                    if (
                        entry.get("sha256") != expected["sha256"]
                        or entry.get("byte_size") != expected["byte_size"]
                        or actual_references != expected["references"]
                    ):
                        raise ValueError
            finally:
                connection.close()
            return manifest
        except AppError as exc:
            if exc.code == "BACKUP_INVALID":
                raise
            raise AppError("BACKUP_INVALID", "备份清单或删除清单校验失败", 400) from exc
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError, sqlite3.Error) as exc:
            raise AppError("BACKUP_INVALID", "备份清单、数据库或文件哈希校验失败", 400) from exc

    def _rotate_backups(self, root: Path, kind: str = "manual") -> None:
        candidates: list[tuple[str, Path]] = []
        for path in root.iterdir():
            if not path.is_dir() or path.is_symlink() or not path.name.startswith("backup-"):
                continue
            try:
                manifest = self._validate_backup_package(path)
                if manifest.get("kind", "manual") == kind:
                    candidates.append((manifest["created_at"], path))
            except AppError:
                continue
        candidates.sort(key=lambda item: (item[0], item[1].name), reverse=True)
        for _, path in candidates[self.settings.backup_count :]:
            shutil.rmtree(path)

    def _validate_restore_target(self, source: Path, target: Path) -> None:
        root = ROOT.resolve()
        backup_root = self.settings.backups.expanduser().resolve()
        if target == root or root in target.parents:
            raise AppError("RESTORE_TARGET_INVALID", "恢复目录必须位于源码仓库之外", 400)
        if (
            target in {self.data_dir, source, backup_root}
            or source in target.parents
            or target in source.parents
            or backup_root in target.parents
            or target in backup_root.parents
            or self.data_dir in target.parents
            or target in self.data_dir.parents
        ):
            raise AppError("RESTORE_TARGET_INVALID", "恢复目录必须与主档案和备份目录分离", 400)
        if target.is_symlink():
            raise AppError("RESTORE_TARGET_INVALID", "恢复目录不能是符号链接", 400)
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise AppError("RESTORE_TARGET_NOT_EMPTY", "恢复目录必须为空", 409)

    @staticmethod
    def _delete_session_sqlite(connection: sqlite3.Connection, session_id: str) -> set[str]:
        paths = {
            row[0]
            for row in connection.execute(
                "SELECT path FROM audio_assets WHERE session_id = ? UNION SELECT path FROM upload_chunks WHERE session_id = ?",
                (session_id, session_id),
            )
        }
        report_ids = [
            row[0] for row in connection.execute("SELECT id FROM reports WHERE session_id = ?", (session_id,))
        ]
        turn_ids = [
            row[0] for row in connection.execute("SELECT id FROM turns WHERE session_id = ?", (session_id,))
        ]
        revision_ids: list[str] = []
        if turn_ids:
            marks = ",".join("?" for _ in turn_ids)
            revision_ids = [
                row[0]
                for row in connection.execute(
                    f"SELECT id FROM transcript_revisions WHERE turn_id IN ({marks})", turn_ids
                )
            ]
        for column, values in (
            ("report_id", report_ids),
            ("turn_id", turn_ids),
            ("revision_id", revision_ids),
        ):
            if values:
                marks = ",".join("?" for _ in values)
                connection.execute(f"DELETE FROM report_citations WHERE {column} IN ({marks})", values)
        connection.execute("DELETE FROM reports WHERE session_id = ?", (session_id,))
        if turn_ids:
            marks = ",".join("?" for _ in turn_ids)
            connection.execute(f"DELETE FROM transcript_revisions WHERE turn_id IN ({marks})", turn_ids)
        for table in (
            "audio_assets",
            "upload_chunks",
            "memory_snapshots",
            "session_events",
            "jobs",
            "idempotency_requests",
            "consents",
        ):
            connection.execute(f"DELETE FROM {table} WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM invites WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM auth_sessions WHERE subject_id = ?", (session_id,))
        connection.execute("DELETE FROM turns WHERE session_id = ?", (session_id,))
        connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
        return paths

    @classmethod
    def _delete_study_sqlite(
        cls, connection: sqlite3.Connection, study_id: str
    ) -> tuple[set[str], list[str]]:
        session_ids = [
            row[0] for row in connection.execute("SELECT id FROM sessions WHERE study_id = ?", (study_id,))
        ]
        version_ids = [
            row[0]
            for row in connection.execute("SELECT id FROM study_versions WHERE study_id = ?", (study_id,))
        ]
        paths: set[str] = set()
        for session_id in session_ids:
            paths.update(cls._delete_session_sqlite(connection, session_id))
        job_ids = [
            job_id
            for job_id, raw_payload in connection.execute("SELECT id, payload FROM jobs")
            if json.loads(raw_payload).get("study_id") == study_id
        ]
        if job_ids:
            marks = ",".join("?" for _ in job_ids)
            connection.execute(f"DELETE FROM jobs WHERE id IN ({marks})", job_ids)
        identifiers = {study_id, *version_ids, *job_ids}
        request_ids: list[str] = []
        for request_id, raw_response in connection.execute(
            "SELECT id, response FROM idempotency_requests WHERE response IS NOT NULL"
        ):
            try:
                response = json.loads(raw_response) if isinstance(raw_response, str) else raw_response
            except (TypeError, json.JSONDecodeError):
                continue
            if _contains_identifier(response, identifiers):
                request_ids.append(request_id)
        if request_ids:
            marks = ",".join("?" for _ in request_ids)
            connection.execute(f"DELETE FROM idempotency_requests WHERE id IN ({marks})", request_ids)
        if version_ids:
            marks = ",".join("?" for _ in version_ids)
            connection.execute(f"DELETE FROM invites WHERE study_version_id IN ({marks})", version_ids)
        connection.execute("DELETE FROM study_versions WHERE study_id = ?", (study_id,))
        connection.execute("DELETE FROM studies WHERE id = ?", (study_id,))
        return paths, session_ids

    def _validate_restored_archive(self, root: Path) -> dict[str, int]:
        database = root / "probeflow.sqlite3"
        connection = sqlite3.connect(database)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise AppError("RESTORE_INVALID", "恢复数据库完整性校验失败", 409)
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise AppError("RESTORE_INVALID", "恢复数据库存在孤立外键", 409)
            mismatch = connection.execute(
                """
                SELECT 1 FROM turns t JOIN transcript_revisions r ON r.id = t.revision_id
                WHERE r.turn_id != t.id LIMIT 1
                """
            ).fetchone()
            if mismatch:
                raise AppError("RESTORE_INVALID", "当前文本修订不属于对应轮次", 409)
            mismatch = connection.execute(
                """
                SELECT 1 FROM transcript_revisions r JOIN transcript_revisions p ON p.id = r.previous_id
                WHERE p.turn_id != r.turn_id LIMIT 1
                """
            ).fetchone()
            if mismatch:
                raise AppError("RESTORE_INVALID", "文本修订链跨越了轮次", 409)
            citations = connection.execute(
                """
                SELECT c.start, c.end, c.quote, r.text, r.turn_id, c.turn_id,
                       rp.session_id, t.session_id
                FROM report_citations c
                JOIN transcript_revisions r ON r.id = c.revision_id
                JOIN reports rp ON rp.id = c.report_id
                JOIN turns t ON t.id = c.turn_id
                """
            ).fetchall()
            for (
                start,
                end,
                quote,
                text,
                revision_turn,
                citation_turn,
                report_session,
                turn_session,
            ) in citations:
                if (
                    revision_turn != citation_turn
                    or report_session != turn_session
                    or start < 0
                    or end < start
                    or end > len(text)
                    or text[start:end] != quote
                ):
                    raise AppError("RESTORE_INVALID", "报告引用与文本修订不匹配", 409)
            asset_rows = connection.execute(
                "SELECT id, path, sha256, byte_size, session_id, turn_id FROM audio_assets"
            ).fetchall()
            chunk_rows = connection.execute(
                "SELECT id, path, sha256, byte_size FROM upload_chunks"
            ).fetchall()
            chunk_rows.extend(
                (identifier, path, sha, size)
                for _, identifier, path, sha, size in _checkpoint_rows(connection)
            )
            for _, relative, expected_hash, expected_size, session_id, turn_id in asset_rows:
                path = self._safe_join(root, relative)
                if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_size:
                    raise AppError("RESTORE_INVALID", f"恢复文件缺失或大小错误：{relative}", 409)
                if _hash_file(path) != expected_hash:
                    raise AppError("RESTORE_INVALID", f"恢复文件哈希错误：{relative}", 409)
                if turn_id:
                    linked = connection.execute(
                        "SELECT 1 FROM turns WHERE id = ? AND session_id = ?", (turn_id, session_id)
                    ).fetchone()
                    if not linked:
                        raise AppError("RESTORE_INVALID", "音频资产跨越了会话边界", 409)
            for _, relative, expected_hash, expected_size in chunk_rows:
                path = self._safe_join(root, relative)
                if path.is_symlink() or not path.is_file() or path.stat().st_size != expected_size:
                    raise AppError("RESTORE_INVALID", f"恢复上传块缺失或大小错误：{relative}", 409)
                if _hash_file(path) != expected_hash:
                    raise AppError("RESTORE_INVALID", f"恢复上传块哈希错误：{relative}", 409)
            return self._database_counts(database)
        except sqlite3.Error as exc:
            raise AppError("RESTORE_INVALID", "恢复数据库查询校验失败", 409) from exc
        finally:
            connection.close()

    @staticmethod
    def _database_file_paths(database: Path) -> set[str]:
        connection = sqlite3.connect(database)
        try:
            return {
                row[0]
                for row in connection.execute(
                    "SELECT path FROM audio_assets UNION SELECT path FROM upload_chunks"
                )
            } | {row[2] for row in _checkpoint_rows(connection)}
        finally:
            connection.close()

    def _discover_last_backup(self, kind: str | None = None) -> dict[str, Any]:
        root = self.settings.backups.expanduser().resolve()
        if not root.exists():
            return {"status": "never"}
        latest: tuple[str, Path, dict[str, Any]] | None = None
        for path in root.iterdir():
            if not path.is_dir() or not path.name.startswith("backup-"):
                continue
            try:
                manifest = self._validate_backup_package(path)
            except AppError:
                continue
            if kind and manifest.get("kind", "manual") != kind:
                continue
            item = (manifest["created_at"], path, manifest)
            if latest is None or item[0] > latest[0]:
                latest = item
        if latest is None:
            return {"status": "never"}
        return {
            "status": "ok",
            "backup_path": str(latest[1]),
            "created_at": latest[0],
            "counts": latest[2].get("counts", {}),
        }


__all__ = ["Storage"]
