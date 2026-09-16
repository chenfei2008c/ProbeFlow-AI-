import csv
import hashlib
import io
import json
import secrets
import shutil
import time

from fastapi import Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import func, select, update

from app.billing import totals
from app.domain import (
    TERMINAL,
    accrue_time,
    add_text_revision,
    detail,
    emit,
    enqueue,
    idempotent,
    iso,
    job_view,
    micro,
    money,
    new_turn,
    require_session,
    session_view,
)
from app.errors import AppError
from app.models import (
    Asset,
    Auth,
    Event,
    InterviewSession,
    Invite,
    Job,
    Ledger,
    Report,
    Study,
    StudyVersion,
    Turn,
    UploadChunk,
)
from app.schemas import BudgetInput, ControlInput, FinalizeInput, StudyInput, TextInput, TurnInput
from app.security import authenticate, digest


def participant_turn(db, request, turn_id, active=False):
    auth = authenticate(db, request, "participant")
    turn = db.get(Turn, turn_id)
    if not turn or turn.session_id != auth.subject_id:
        raise AppError("NOT_FOUND", "回答不存在", 404)
    return require_session(db, auth.subject_id, consent=True, active=active), turn


def busy(db, sid):
    return (
        db.scalar(
            select(Job.id)
            .where(
                Job.session_id == sid,
                Job.kind.in_(["decide", "asr", "tts"]),
                Job.status.in_(["queued", "running"]),
            )
            .limit(1)
        )
        is not None
    )


def register_session_routes(app, database, settings):
    def storage():
        from app.storage import Storage

        return Storage(database, settings)

    @app.post("/api/participant/turns")
    def create_turn(body: TurnInput, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "participant")
            session = require_session(db, auth.subject_id, consent=True, active=True)

            def operation():
                accrue_time(session)
                if session.active_seconds >= 5400:
                    raise AppError("TIME_LIMIT", "已达到 90 分钟安全上限，请结束访谈；当前资料仍保留", 409)
                if session.active_seconds >= session.target_seconds:
                    raise AppError(
                        "TARGET_TIME_REACHED", "目标时长已到，请结束访谈或明确选择延长 10 分钟", 409
                    )
                if busy(db, session.id):
                    raise AppError("TURN_BUSY", "上一轮还在处理中，请稍候", 409, True)
                pending = db.scalar(
                    select(Turn).where(
                        Turn.session_id == session.id,
                        Turn.role == "participant",
                        Turn.status.in_(["recording", "uploading", "transcribing", "confirming"]),
                    )
                )
                if pending:
                    raise AppError("CONFIRMATION_REQUIRED", "请先确认当前回答或选择重新回答", 409)
                if body.input_mode == "voice" and session.mode != "voice":
                    raise AppError("VOICE_CONSENT_REQUIRED", "请先同意语音处理和录音永久保存", 403)
                if body.input_mode == "text" and not body.text:
                    raise AppError("EMPTY_ANSWER", "请输入回答")
                storage().ensure_space()
                latest = db.scalar(
                    select(Turn)
                    .where(Turn.session_id == session.id, Turn.role == "assistant")
                    .order_by(Turn.seq.desc())
                    .limit(1)
                )
                if not latest:
                    raise AppError("QUESTION_NOT_READY", "请等待访谈问题生成", 409, True)
                turn = new_turn(
                    db,
                    session,
                    "participant",
                    body.input_mode,
                    "confirming" if body.input_mode == "text" else "recording",
                    mime_type=body.mime_type,
                    topic_id=latest.topic_id,
                    start_seconds=session.active_seconds,
                )
                if body.input_mode == "text":
                    add_text_revision(db, turn, body.text, "typed", "participant")
                session.status = "in_progress"
                session.last_heartbeat = time.time()
                emit(db, session, "turn.created", {"turn_id": turn.id})
                config = db.get(StudyVersion, session.study_version_id).config
                result = {"turn_id": turn.id}
                if body.input_mode == "text" and not config["confirm_transcript"]:
                    turn.confirmed, turn.status = True, "confirmed"
                    session.revision += 1
                    job = enqueue(db, "decide", {"answer_id": turn.id}, session.id, f"answer:{turn.id}")
                    result["job_id"] = job.id
                return result

            return idempotent(db, request, session.id, body.model_dump(), operation, session.id)

    @app.put("/api/participant/turns/{turn_id}/chunks/{seq}")
    async def upload_chunk(turn_id: str, seq: int, request: Request):
        data = bytearray()
        async for part in request.stream():
            data.extend(part)
            if len(data) > 12 * 1024 * 1024:
                raise AppError("CHUNK_TOO_LARGE", "音频块过大", 413)
        expected = request.headers.get("X-Chunk-SHA256")
        actual = digest(bytes(data))
        if seq < 0 or seq > 10000 or not data or expected != actual:
            raise AppError("CHUNK_INVALID", "音频块序号、内容或校验值无效", 409)
        with database.transaction() as db:
            session, turn = participant_turn(db, request, turn_id, active=True)
            if session.mode != "voice" or turn.input_mode != "voice":
                raise AppError("VOICE_CONSENT_REQUIRED", "当前回答不允许上传音频", 403)
            prior = db.scalar(
                select(UploadChunk).where(UploadChunk.turn_id == turn.id, UploadChunk.seq == seq)
            )
            if prior:
                if prior.sha256 != actual:
                    raise AppError("CHUNK_CONFLICT", "同一位置的音频块内容不一致", 409)
                return {"seq": seq, "sha256": actual}
            if turn.finalized_chunks is not None or turn.status not in {"recording", "uploading"}:
                raise AppError("TURN_FINALIZED", "这一轮音频已经提交", 409)
            size = db.scalar(
                select(func.coalesce(func.sum(UploadChunk.byte_size), 0)).where(
                    UploadChunk.turn_id == turn.id
                )
            )
            if size + len(data) > 100 * 1024 * 1024:
                raise AppError("TURN_TOO_LARGE", "本轮录音超过 100MB，请结束当前回答", 413)
            relative = f"chunks/{session.id}/{turn.id}/{seq:05d}.chunk"
            storage().write_file(relative, bytes(data))
            db.add(
                UploadChunk(
                    session_id=session.id,
                    turn_id=turn.id,
                    seq=seq,
                    sha256=actual,
                    byte_size=len(data),
                    path=relative,
                )
            )
            turn.status = "uploading"
            return {"seq": seq, "sha256": actual}

    @app.post("/api/participant/turns/{turn_id}/finalize")
    def finalize(turn_id: str, body: FinalizeInput, request: Request):
        with database.transaction() as db:
            session, turn = participant_turn(db, request, turn_id, active=True)

            def operation():
                records = list(
                    db.scalars(
                        select(UploadChunk).where(UploadChunk.turn_id == turn.id).order_by(UploadChunk.seq)
                    )
                )
                manifest = [item.model_dump() for item in body.chunks]
                if turn.finalized_chunks is not None:
                    if manifest != turn.finalized_chunks:
                        raise AppError("FINALIZE_CONFLICT", "已提交的音频清单不能改变", 409)
                    job = db.scalar(select(Job).where(Job.dedup_key == f"asr:{turn.id}"))
                    return {"job_id": job.id}
                if [item.seq for item in body.chunks] != list(range(len(records))) or len(manifest) != len(
                    records
                ):
                    raise AppError("MISSING_CHUNKS", "录音分块缺失或顺序不正确，请重试上传", 409, True)
                for row, item in zip(records, body.chunks):
                    path = storage().safe_path(row.path)
                    if (
                        row.sha256 != item.sha256
                        or not path.exists()
                        or digest(path.read_bytes()) != item.sha256
                    ):
                        raise AppError("CHUNK_CONFLICT", "音频块完整性校验失败", 409)
                turn.finalized_chunks, turn.status = manifest, "transcribing"
                turn.end_seconds = session.active_seconds
                job = enqueue(db, "asr", {"turn_id": turn.id}, session.id, f"asr:{turn.id}")
                return {"job_id": job.id}

            return idempotent(db, request, session.id, body.model_dump(), operation, session.id)

    @app.post("/api/participant/turns/{turn_id}/confirm")
    def confirm(turn_id: str, body: TextInput, request: Request):
        with database.transaction() as db:
            session, turn = participant_turn(db, request, turn_id)

            def operation():
                asr_job = db.scalar(select(Job).where(Job.dedup_key == f"asr:{turn.id}"))
                manual = (
                    turn.status == "transcribing"
                    and asr_job is not None
                    and asr_job.status in {"failed", "external_status_unknown"}
                )
                if session.status not in {"ready", "in_progress", "paused"}:
                    raise AppError("SESSION_ENDED", "访谈已结束", 409)
                if not manual and session.status == "paused":
                    raise AppError("SESSION_NOT_ACTIVE", "请先恢复访谈再确认转写", 409)
                if turn.role != "participant" or (turn.status != "confirming" and not manual):
                    raise AppError("TURN_NOT_CONFIRMABLE", "当前回答不能重复确认", 409)
                add_text_revision(
                    db,
                    turn,
                    body.text,
                    "typed_after_asr_failure" if manual else "participant_confirmed",
                    "participant",
                )
                if manual:
                    asr_job.status, asr_job.payload, asr_job.result = "cancelled", {}, None
                turn.status, turn.confirmed = "confirmed", True
                turn.end_seconds = session.active_seconds
                session.revision += 1
                job = enqueue(db, "decide", {"answer_id": turn.id}, session.id, f"answer:{turn.id}")
                emit(db, session, "turn.confirmed", {"turn_id": turn.id})
                return {"job_id": job.id}

            return idempotent(db, request, session.id, body.model_dump(), operation, session.id)

    @app.delete("/api/admin/studies/{study_id}")
    def delete_study(study_id: str, request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
        return storage().delete_study(study_id)

    @app.post("/api/participant/control")
    def control(body: ControlInput, request: Request):
        if body.action == "withdraw":
            with database.read() as db:
                auth = authenticate(db, request, "participant")
                require_session(db, auth.subject_id)
                sid = auth.subject_id
            return storage().delete_session(sid, withdrawn=True)
        with database.transaction() as db:
            auth = authenticate(db, request, "participant")
            session = require_session(db, auth.subject_id, consent=True)

            def operation():
                if (
                    body.action in {"pause", "resume", "skip", "end", "extend", "rerecord"}
                    and session.status in TERMINAL
                ):
                    raise AppError("SESSION_ENDED", "访谈已结束", 409)
                accrue_time(session)
                result = {}
                if body.action == "pause":
                    session.status, session.pause_reason = "paused", body.reason or "user_paused"
                elif body.action == "resume":
                    if session.status != "paused":
                        raise AppError("NOT_PAUSED", "当前访谈没有暂停", 409)
                    active = db.scalar(
                        select(func.count())
                        .select_from(InterviewSession)
                        .where(InterviewSession.status.in_(["ready", "in_progress", "finalizing"]))
                    )
                    if active >= settings.max_active_sessions:
                        raise AppError("CAPACITY_REACHED", "当前访谈已达两场", 409, True)
                    storage().ensure_space()
                    if session.reserved_micro + session.spent_micro >= session.budget_micro:
                        raise AppError("BUDGET_EXCEEDED", "请先由研究者补充预算", 409)
                    session.status, session.pause_reason = "in_progress", None
                elif body.action == "end":
                    unfinished = db.scalar(
                        select(Turn.id)
                        .where(
                            Turn.session_id == session.id,
                            Turn.role == "participant",
                            Turn.status.in_(["recording", "uploading"]),
                        )
                        .limit(1)
                    )
                    if unfinished:
                        raise AppError(
                            "UPLOAD_INCOMPLETE", "录音尚未完整提交，请先恢复上传或明确重新回答", 409
                        )
                    session.status, session.ended_at, session.end_reason = (
                        "finalizing",
                        time.time(),
                        "participant_ended",
                    )
                    db.execute(
                        update(Job)
                        .where(
                            Job.session_id == session.id,
                            Job.kind.in_(["decide", "tts"]),
                            Job.status == "queued",
                        )
                        .values(status="cancelled")
                    )
                    job = enqueue(db, "report", {}, session.id, f"end-report:{session.id}")
                    result["job_id"] = job.id
                elif body.action == "extend":
                    if session.target_seconds >= 5400:
                        raise AppError("TIME_LIMIT", "已达到 90 分钟安全上限，请结束当前回答", 409)
                    session.target_seconds = min(5400, session.target_seconds + 600)
                elif body.action == "playback_done":
                    turn = db.get(Turn, body.turn_id)
                    if not turn or turn.session_id != session.id or turn.role != "assistant":
                        raise AppError("NOT_FOUND", "问题不存在", 404)
                    turn.played_complete = bool(body.played_complete)
                    turn.status = "ready_to_record"
                elif body.action == "rerecord":
                    turn = db.get(Turn, body.turn_id)
                    if not turn or turn.session_id != session.id or turn.role != "participant":
                        raise AppError("NOT_FOUND", "回答不存在", 404)
                    if turn.confirmed or turn.status == "transcribing":
                        raise AppError("TURN_BUSY", "请等待当前处理结束；已确认回答不能重新录制", 409)
                    turn.status = "superseded"
                elif body.action == "skip":
                    if busy(db, session.id):
                        raise AppError("TURN_BUSY", "请等待当前处理结束", 409, True)
                    latest = db.scalar(
                        select(Turn).where(Turn.session_id == session.id).order_by(Turn.seq.desc()).limit(1)
                    )
                    if latest and latest.role == "participant" and not latest.confirmed:
                        latest.status = "superseded"
                    job = enqueue(
                        db,
                        "decide",
                        {"skipped": True, "topic_id": latest.topic_id if latest else None},
                        session.id,
                    )
                    result["job_id"] = job.id
                elif body.action == "retry":
                    job = db.get(Job, body.job_id)
                    if (
                        not job
                        or job.session_id != session.id
                        or job.status not in {"failed", "external_status_unknown"}
                    ):
                        raise AppError("JOB_NOT_RETRYABLE", "该任务不能重试", 409)
                    if job.status == "external_status_unknown" and not body.accept_possible_charge:
                        raise AppError(
                            "CHARGE_CONFIRMATION_REQUIRED", "上次请求可能已计费，重试需要明确确认", 409
                        )
                    job.status, job.error_code, job.error_message, job.call_started = (
                        "queued",
                        None,
                        None,
                        False,
                    )
                    job.lease_until, job.lease_token = None, None
                    session.pause_reason = None
                    if session.status == "paused":
                        session.status = "in_progress"
                    result["job_id"] = job.id
                emit(db, session, f"session.{body.action}", {"turn_id": body.turn_id})
                result.update(session_view(session, admin=False))
                return result

            return idempotent(db, request, session.id, body.model_dump(), operation, session.id)

    @app.post("/api/participant/heartbeat")
    def heartbeat(request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "participant")
            session = require_session(db, auth.subject_id)
            accrue_time(session)
            return {"active_seconds": int(session.active_seconds), "status": session.status}

    @app.get("/api/participant/events")
    def events(request: Request, after: int = 0):
        with database.read() as db:
            auth = authenticate(db, request, "participant")
            require_session(db, auth.subject_id)
            rows = list(
                db.scalars(
                    select(Event)
                    .where(Event.session_id == auth.subject_id, Event.seq > max(0, after))
                    .order_by(Event.seq)
                    .limit(200)
                )
            )
            return {
                "events": [
                    {"seq": r.seq, "type": r.type, "payload": r.payload, "created_at": iso(r.created_at)}
                    for r in rows
                ],
                "cursor": rows[-1].seq if rows else after,
            }

    @app.get("/api/media/{asset_id}")
    def media(asset_id: str, request: Request):
        with database.read() as db:
            try:
                authenticate(db, request, "admin")
                allowed_sid = None
            except AppError:
                allowed_sid = authenticate(db, request, "participant").subject_id
            asset = db.get(Asset, asset_id)
            if not asset or (allowed_sid is not None and asset.session_id != allowed_sid):
                raise AppError("NOT_FOUND", "音频不存在", 404)
            if asset.session_id:
                require_session(db, asset.session_id)
            path = storage().safe_path(asset.path)
            if not path.is_file():
                raise AppError("ARCHIVE_MISSING", "音频档案缺失，请联系研究者检查备份", 404)
            if asset.duration_seconds is None:
                raise AppError("AUDIO_UNPLAYABLE", "原始文件已保存，但格式或时长校验失败，暂不能回放", 409)
            with path.open("rb") as source:
                actual_hash = hashlib.file_digest(source, "sha256").hexdigest()
            if path.stat().st_size != asset.byte_size or actual_hash != asset.sha256:
                raise AppError(
                    "ARCHIVE_CORRUPTED", "音频档案校验失败，请联系研究者检查备份；文字仍可访问", 409
                )
            return FileResponse(path, media_type=asset.mime_type, headers={"Cache-Control": "no-store"})

    @app.get("/api/admin/jobs/{job_id}")
    def admin_job(job_id: str, request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            job = db.get(Job, job_id)
            if not job:
                raise AppError("NOT_FOUND", "任务不存在", 404)
            return job_view(job)

    @app.post("/api/admin/studies/{study_id}/outline")
    def outline(study_id: str, body: StudyInput, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            if not db.get(Study, study_id):
                raise AppError("NOT_FOUND", "研究不存在", 404)
            return idempotent(
                db,
                request,
                auth.subject_id,
                body.model_dump(mode="json"),
                lambda: {
                    "job_id": enqueue(
                        db, "outline", {"study_id": study_id, "study": body.model_dump(mode="json")}
                    ).id
                },
            )

    @app.post("/api/admin/sessions/{sid}/reports")
    def generate_report(sid: str, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            require_session(db, sid, consent=True)
            return idempotent(
                db, request, auth.subject_id, {}, lambda: {"job_id": enqueue(db, "report", {}, sid).id}, sid
            )

    @app.post("/api/admin/sessions/{sid}/budget")
    def change_budget(sid: str, body: BudgetInput, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            session = require_session(db, sid)

            def operation():
                amount = micro(body.budget_cny)
                if amount < session.spent_micro + session.reserved_micro:
                    raise AppError("BUDGET_TOO_LOW", "预算不能低于已用及预占金额")
                session.budget_micro = amount
                return session_view(session)

            return idempotent(db, request, auth.subject_id, body.model_dump(mode="json"), operation, sid)

    @app.post("/api/admin/turns/{turn_id}/revision")
    def revise(turn_id: str, body: TextInput, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            turn = db.get(Turn, turn_id)
            if not turn:
                raise AppError("NOT_FOUND", "发言不存在", 404)
            session = require_session(db, turn.session_id)

            def operation():
                revision = add_text_revision(db, turn, body.text, "researcher_correction", "admin")
                session.revision += 1
                db.execute(update(Report).where(Report.session_id == session.id).values(source_updated=True))
                return {"revision_id": revision.id}

            return idempotent(db, request, auth.subject_id, body.model_dump(), operation, session.id)

    @app.delete("/api/admin/sessions/{sid}")
    def delete_session(sid: str, request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            require_session(db, sid)
        return storage().delete_session(sid)

    @app.post("/api/admin/sessions/{sid}/recovery-invite")
    def recovery(sid: str, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            session = require_session(db, sid)

            def operation():
                db.execute(
                    update(Auth)
                    .where(Auth.role == "participant", Auth.subject_id == sid)
                    .values(revoked_at=time.time())
                )
                db.execute(update(Invite).where(Invite.session_id == sid).values(revoked_at=time.time()))
                token = secrets.token_urlsafe(32)
                invite = Invite(
                    study_version_id=session.study_version_id,
                    session_id=sid,
                    token_hash=digest(token),
                    expires_at=time.time() + 86400,
                )
                db.add(invite)
                return {
                    "url": f"{settings.public_base_url.rstrip('/')}/join#token={token}",
                    "expires_at": iso(invite.expires_at),
                }

            return idempotent(db, request, auth.subject_id, {}, operation, sid, confidential=True)

    @app.get("/api/admin/sessions/{sid}/export")
    def export(sid: str, request: Request, format: str = "json"):
        with database.read() as db:
            authenticate(db, request, "admin")
            data = detail(db, require_session(db, sid), settings)
        marker = "模拟结果（不代表真实访谈或模型质量）" if settings.mode == "mock" else "真实访谈记录"
        metadata = {
            "mode": settings.mode,
            "label": marker,
            "retention": "permanent",
            "prompt_version": "V1.1",
            "providers": settings.public_providers(),
            "exported_at": iso(time.time()),
        }
        if format == "json":
            data.pop("jobs", None)
            content, mime, suffix = (
                json.dumps({**metadata, **data}, ensure_ascii=False, indent=2),
                "application/json",
                "json",
            )
        elif format == "csv":
            buffer = io.StringIO(newline="")
            writer = csv.writer(buffer)
            writer.writerow(
                ["模式", "轮次", "发言ID", "文本版本", "角色", "来源", "文本", "创建时间", "保存策略"]
            )
            for turn in data["turns"]:
                for revision in turn["revisions"]:
                    text = revision["text"]
                    if text.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                        text = "'" + text
                    writer.writerow(
                        [
                            marker,
                            turn["seq"],
                            turn["id"],
                            revision["id"],
                            turn["role"],
                            revision["source"],
                            text,
                            revision["created_at"],
                            "permanent",
                        ]
                    )
            content, mime, suffix = "\ufeff" + buffer.getvalue(), "text/csv", "csv"
        elif format == "markdown":
            from app.interview import render_text

            report = data["reports"][-1] if data["reports"] else None
            content = f"# 访谈记录\n\n{marker}\n\n保存策略：永久保存\n\n"
            if report:
                content += (
                    "**来源已更新，请重新生成报告。**\n\n" if report["source_updated"] else ""
                ) + report["markdown"]
            else:
                content += "报告尚未生成。\n\n"
            content += "\n## 逐字稿与修订历史\n\n"
            for turn in data["turns"]:
                content += f"### {turn['seq']} · {turn['role']}\n\n"
                for number, revision in enumerate(turn["revisions"], 1):
                    content += (
                        f"文本版本 {number} · {revision['source']}\n\n{render_text(revision['text'])}\n\n"
                    )
            mime, suffix = "text/markdown", "md"
        else:
            raise AppError("INVALID_FORMAT", "导出格式仅支持 json、csv、markdown")
        return Response(
            content,
            media_type=mime,
            headers={"Content-Disposition": f'attachment; filename="interview-{sid}.{suffix}"'},
        )

    @app.get("/api/admin/usage")
    def usage(request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            spent, reserved = totals(db)
            entries = list(db.scalars(select(Ledger).order_by(Ledger.created_at.desc()).limit(500)))
            return {
                "month_spent_cny": money(spent),
                "month_reserved_cny": money(reserved),
                "monthly_limit_cny": float(settings.monthly_limit_cny),
                "entries": [
                    {
                        "id": e.id,
                        "session_id": e.session_id,
                        "role": e.role,
                        "model": e.model,
                        "status": e.status,
                        "amount_cny": money(e.amount_micro) if e.amount_micro is not None else None,
                        "amount_source": e.amount_source,
                        "usage": e.usage,
                        "created_at": iso(e.created_at),
                    }
                    for e in entries
                ],
            }

    @app.get("/api/admin/diagnostics")
    def diagnostics(request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            errors = list(
                db.scalars(
                    select(Job)
                    .where(Job.status.in_(["failed", "external_status_unknown"]))
                    .order_by(Job.created_at.desc())
                    .limit(20)
                )
            )
            result = {
                "mode": settings.mode,
                "providers": settings.public_providers(),
                "ffmpeg_available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
                "recent_errors": [
                    {"code": j.error_code, "message": j.error_message, "request_id": j.id} for j in errors
                ],
            }
        stats = storage().diagnostics()
        result["storage"] = {
            "archive": stats["formal_bytes"],
            "temporary": stats["temp_bytes"],
            "backup": stats["backup_bytes"],
            "available": stats["free_bytes"],
        }
        result["backup"] = stats["last_backup"]
        import os
        from dotenv import dotenv_values

        values = {**dotenv_values(".env"), **os.environ}
        for role, config in result["providers"].items():
            config["configured"] = bool(values.get(getattr(settings, f"{role}_key_env")))
            config["available"] = True if settings.mode == "mock" else None
        return result

    @app.post("/api/admin/diagnostics/tts")
    def diagnostic_tts(body: TextInput, request: Request):
        if len(body.text) > 100:
            raise AppError("TEXT_TOO_LONG", "试听文字最多 100 字")
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            return idempotent(
                db,
                request,
                auth.subject_id,
                body.model_dump(),
                lambda: {"job_id": enqueue(db, "tts", {"text": body.text}).id},
            )
