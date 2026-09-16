import json
import time
from pathlib import Path
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select

from app.config import CONSENT_VERSION
from app.errors import AppError
from app.models import (
    Asset,
    Citation,
    Event,
    InterviewSession,
    Job,
    Memory,
    Report,
    RequestRecord,
    Revision,
    StudyVersion,
    Turn,
    uid,
)
from app.security import digest
from app.provenance import archive_mode, origin_modes

TERMINAL = {"completed", "withdrawn", "deleted", "expired"}
DELETED = {"withdrawn", "deleted"}


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat() if value is not None else None


def money(value):
    # JSON display amounts only; all accumulation and budget comparisons use integer micro-CNY.
    return float(Decimal(value) / Decimal(1_000_000))


def micro(value):
    return int(Decimal(str(value)) * 1_000_000)


def require_session(db, sid, consent=False, active=False):
    session = db.get(InterviewSession, sid)
    if not session or session.status in DELETED:
        raise AppError("NOT_FOUND", "访谈不存在或已删除", 404)
    if consent and (
        not session.processing_consent
        or not session.permanent_consent
        or session.consent_version != db.info.get("consent_version", CONSENT_VERSION)
    ):
        raise AppError("CONSENT_REQUIRED", "请先确认数据处理与永久保存说明", 403)
    if active and session.status not in {"ready", "in_progress"}:
        raise AppError("SESSION_NOT_ACTIVE", "当前访谈已暂停或结束，请先恢复", 409)
    return session


def emit(db, session, kind, payload=None):
    seq = (db.scalar(select(func.max(Event.seq)).where(Event.session_id == session.id)) or 0) + 1
    db.add(Event(session_id=session.id, seq=seq, type=kind, payload=payload or {}))
    db.flush()


def enqueue(db, kind, payload, session_id=None, dedup_key=None):
    key = dedup_key or f"{kind}:{uid()}"
    existing = db.scalar(select(Job).where(Job.dedup_key == key))
    if existing:
        return existing
    job = Job(kind=kind, payload=payload, session_id=session_id, dedup_key=key)
    db.add(job)
    db.flush()
    if session_id:
        emit(db, db.get(InterviewSession, session_id), "job.queued", {"job_id": job.id, "kind": kind})
    return job


def idempotent(db, request, scope, body, operation, session_id=None, confidential=False):
    key = request.headers.get("Idempotency-Key", "")
    full_scope = f"{scope}:{request.method}:{request.url.path}"
    body_hash = digest(json.dumps(body, ensure_ascii=False, sort_keys=True, default=str))
    prior = db.scalar(
        select(RequestRecord).where(RequestRecord.scope == full_scope, RequestRecord.key == key)
    )
    if prior:
        if prior.body_hash != body_hash:
            raise AppError("IDEMPOTENCY_CONFLICT", "同一请求标识不能用于不同内容", 409)
        if prior.response is None:
            raise AppError("ALREADY_PROCESSED", "该请求已完成；敏感邀请不会重复显示，请创建新邀请", 409)
        return prior.response
    result = operation()
    db.add(
        RequestRecord(
            scope=full_scope,
            key=key,
            body_hash=body_hash,
            response=None if confidential else result,
            session_id=session_id,
        )
    )
    return result


def study_view(db, study):
    version = db.get(StudyVersion, study.current_version_id)
    sessions = list(
        db.scalars(
            select(InterviewSession).where(
                InterviewSession.study_id == study.id, InterviewSession.status.not_in(DELETED)
            )
        )
    )
    return {
        "id": study.id,
        "title": study.title,
        "archived": study.archived,
        "current_version_id": study.current_version_id,
        "version_number": version.number,
        "version": version.config,
        "session_count": len(sessions),
        "completed_count": sum(s.status == "completed" for s in sessions),
        "total_cost_cny": money(sum(s.spent_micro for s in sessions)),
        "updated_at": iso(study.updated_at),
    }


def session_view(session, admin=True):
    fields = (
        "id",
        "study_id",
        "status",
        "participant_code",
        "mode",
        "consent_version",
        "processing_consent",
        "permanent_consent",
        "pause_reason",
        "retention",
    )
    result = {field: getattr(session, field) for field in fields}
    result.update(
        active_seconds=int(session.active_seconds),
        target_seconds=session.target_seconds,
        created_at=iso(session.created_at),
        ended_at=iso(session.ended_at),
    )
    if admin:
        result.update(
            budget_cny=money(session.budget_micro),
            spent_cny=money(session.spent_micro),
            reserved_cny=money(session.reserved_micro),
        )
    return result


def invite_view(invite, version_number):
    status = "available"
    if invite.revoked_at is not None:
        status = "revoked"
    elif invite.redeemed_at is not None:
        status = "redeemed"
    elif invite.expires_at <= time.time():
        status = "expired"
    return {
        "id": invite.id,
        "version_number": version_number,
        "session_id": invite.session_id,
        "status": status,
        "created_at": iso(invite.created_at),
        "expires_at": iso(invite.expires_at),
        "redeemed_at": iso(invite.redeemed_at),
        "revoked_at": iso(invite.revoked_at),
    }


def turn_view(db, turn):
    revisions = list(
        db.scalars(
            select(Revision).where(Revision.turn_id == turn.id).order_by(Revision.created_at, Revision.id)
        )
    )
    current = db.get(Revision, turn.revision_id) if turn.revision_id else None
    asset = db.get(Asset, turn.audio_asset_id) if turn.audio_asset_id else None
    audio_status = "unavailable"
    if asset:
        archive = Path(db.bind.url.database).parent / asset.path
        if not archive.is_file() or archive.stat().st_size != asset.byte_size:
            audio_status = "missing"
        elif asset.duration_seconds is None or asset.mime_type == "application/octet-stream":
            audio_status = "unplayable"
        else:
            audio_status = "available"
    return {
        "id": turn.id,
        "seq": turn.seq,
        "role": turn.role,
        "status": turn.status,
        "input_mode": turn.input_mode,
        "text": current.text if current else "",
        "revision_id": turn.revision_id,
        "provenance": current.provenance if current else {},
        "revisions": [
            {
                "id": r.id,
                "text": r.text,
                "source": r.source,
                "provenance": r.provenance,
                "previous_id": r.previous_id,
                "created_at": iso(r.created_at),
            }
            for r in revisions
        ],
        "audio_asset_id": turn.audio_asset_id,
        "audio_status": audio_status,
        "audio_provenance": asset.provenance if asset else None,
        "action": turn.action,
        "topic_id": turn.topic_id,
        "created_at": iso(turn.created_at),
        "confirmed": turn.confirmed,
        "played_complete": turn.played_complete,
        "start_seconds": turn.start_seconds,
        "end_seconds": turn.end_seconds,
    }


def job_view(job):
    result = {k: v for k, v in (job.result or {}).items() if not k.startswith("_")}
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "error_code": job.error_code,
        "error_message": job.error_message,
        "result": result or None,
        "created_at": iso(job.created_at),
    }


def report_view(db, report):
    citations = list(db.scalars(select(Citation).where(Citation.report_id == report.id)))
    return {
        "id": report.id,
        "version": report.version,
        "status": report.status,
        "source_updated": report.source_updated,
        "body": report.body,
        "markdown": report.markdown,
        "provenance": report.provenance,
        "archive_mode": archive_mode([report.provenance]),
        "created_at": iso(report.created_at),
        "citations": [
            {
                "turn_id": c.turn_id,
                "revision_id": c.revision_id,
                "start": c.start,
                "end": c.end,
                "quote": c.quote,
            }
            for c in citations
        ],
    }


def detail(db, session, settings, admin=True):
    turns = list(db.scalars(select(Turn).where(Turn.session_id == session.id).order_by(Turn.seq)))
    jobs = list(db.scalars(select(Job).where(Job.session_id == session.id).order_by(Job.created_at)))
    config = dict(db.get(StudyVersion, session.study_version_id).config)
    if not admin:
        config = {
            key: config[key]
            for key in ("title", "objective", "target_minutes", "language", "retention", "confirm_transcript")
        }
    result = {
        "session": session_view(session, admin),
        "study": config,
        "turns": [turn_view(db, t) for t in turns],
        "jobs": [job_view(j) for j in jobs if admin or j.kind not in {"report", "summary"}],
        "mode": settings.mode,
        "consent_version": settings.consent_version,
        "providers": settings.public_providers(),
    }
    if admin:
        reports = list(
            db.scalars(select(Report).where(Report.session_id == session.id).order_by(Report.version))
        )
        memory = db.scalar(
            select(Memory).where(Memory.session_id == session.id).order_by(Memory.version.desc()).limit(1)
        )
        result.update(
            reports=[report_view(db, r) for r in reports],
            memory=memory.content if memory else {"topics": [], "unresolved": []},
        )
    provenances = [r["provenance"] for t in result["turns"] for r in t["revisions"]]
    provenances.extend(t["audio_provenance"] for t in result["turns"] if t["audio_provenance"] is not None)
    provenances.extend(r["provenance"] for r in result.get("reports", []))
    result["archive_mode"] = archive_mode(provenances)
    return result


def add_text_revision(db, turn, text, source, editor, provenance=None):
    previous = db.get(Revision, turn.revision_id) if turn.revision_id else None
    provenance = (
        dict(provenance) if provenance is not None else {"mode": db.info.get("runtime_mode", "unknown")}
    )
    if previous:
        provenance["source_modes"] = sorted(
            set(provenance.get("source_modes", [])) | origin_modes(previous.provenance)
        )
    revision = Revision(
        turn_id=turn.id,
        text=text,
        source=source,
        editor=editor,
        previous_id=turn.revision_id,
        provenance=provenance,
    )
    db.add(revision)
    db.flush()
    turn.revision_id = revision.id
    return revision


def new_turn(db, session, role, input_mode, status, **kwargs):
    seq = (db.scalar(select(func.max(Turn.seq)).where(Turn.session_id == session.id)) or 0) + 1
    turn = Turn(session_id=session.id, seq=seq, role=role, input_mode=input_mode, status=status, **kwargs)
    db.add(turn)
    db.flush()
    return turn


def accrue_time(session, now=None):
    now = time.time() if now is None else now
    if session.status == "in_progress" and session.last_heartbeat:
        # Missing >30s heartbeat is an explicit recovery boundary, not unlimited active time.
        session.active_seconds += max(0, min(now - session.last_heartbeat, 30))
    session.last_heartbeat = now
