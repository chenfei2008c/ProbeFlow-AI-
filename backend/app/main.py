from contextlib import asynccontextmanager
import secrets
import time

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from sqlalchemy import func, select

from app.config import ROOT, Settings
from app.db import Database
from app.domain import (
    DELETED,
    TERMINAL,
    detail,
    emit,
    enqueue,
    idempotent,
    iso,
    micro,
    require_session,
    session_view,
    study_view,
)
from app.errors import AppError
from app.models import Consent, InterviewSession, Invite, Job, Owner, Study, StudyVersion, uid
from app.middleware import RequestBodyLimit
from app.schemas import ConsentInput, StudyInput
from app.security import (
    COOKIES,
    authenticate,
    create_auth,
    digest,
    initialize_owner,
    login,
    rate_limit,
    set_cookie,
)


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    database = Database(settings)
    database.initialize()
    with database.transaction() as db:
        initialize_owner(db, settings)

    @asynccontextmanager
    async def lifespan(app):
        worker = None
        if settings.worker_enabled:
            from app.worker import Worker

            worker = Worker(database, settings)
            app.state.worker = worker
            await worker.start()
        yield
        if worker:
            await worker.stop()

    app = FastAPI(title="ProbeFlow", version="1.1.0", lifespan=lifespan)
    app.add_middleware(RequestBodyLimit)
    app.state.db, app.state.settings = database, settings

    @app.middleware("http")
    async def request_boundary(request, call_next):
        request.state.request_id = uid()
        if request.url.path.startswith("/api/") and request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if request.headers.get("X-ProbeFlow-Client") != "web" or (
                origin and origin.rstrip("/") not in settings.origins
            ):
                return JSONResponse(
                    {
                        "code": "ORIGIN_REJECTED",
                        "message": "请求来源校验失败",
                        "retryable": False,
                        "request_id": request.state.request_id,
                    },
                    status_code=403,
                )
            key = request.headers.get("Idempotency-Key", "")
            if not key or len(key) > 128:
                return JSONResponse(
                    {
                        "code": "IDEMPOTENCY_REQUIRED",
                        "message": "请求缺少有效的幂等标识",
                        "retryable": False,
                        "request_id": request.state.request_id,
                    },
                    status_code=400,
                )
            length = request.headers.get("content-length", "0")
            if not length.isdigit() or int(length) > 12 * 1024 * 1024:
                return JSONResponse(
                    {
                        "code": "REQUEST_TOO_LARGE",
                        "message": "请求数据过大",
                        "retryable": False,
                        "request_id": request.state.request_id,
                    },
                    status_code=413,
                )
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; media-src 'self' blob:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(AppError)
    async def app_error(request, exc):
        return JSONResponse(
            {
                "code": exc.code,
                "message": exc.message,
                "retryable": exc.retryable,
                "request_id": getattr(request.state, "request_id", uid()),
            },
            status_code=exc.status,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        fields = ", ".join(".".join(str(p) for p in e["loc"][1:]) for e in exc.errors())
        return JSONResponse(
            {
                "code": "INVALID_INPUT",
                "message": f"请检查输入字段：{fields}",
                "retryable": False,
                "request_id": request.state.request_id,
            },
            status_code=422,
        )

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": "1.1.0", "mode": settings.mode}

    @app.get("/api/config")
    def config():
        with database.read() as db:
            return {
                "mode": settings.mode,
                "consent_version": settings.consent_version,
                "providers": settings.public_providers(),
                "admin_initialized": db.scalar(select(Owner.id)) is not None,
            }

    @app.post("/api/admin/login")
    def admin_login(body: dict, request: Request):
        with database.transaction() as db:
            allowed = rate_limit(db, "login:" + digest(request.client.host))
        if not allowed:
            raise AppError("RATE_LIMITED", "尝试过于频繁，请一分钟后重试", 429, True)
        password = body.get("password")
        if not isinstance(password, str) or len(password) > 1000:
            raise AppError("INVALID_CREDENTIALS", "请输入密码", 401)
        with database.transaction() as db:
            owner = login(db, password)
            token, duration = create_auth(db, "admin", owner.id)
            response = JSONResponse({"id": owner.id})
            set_cookie(response, settings, "admin", token, duration)
            return response

    @app.post("/api/admin/logout")
    def admin_logout(request: Request):
        with database.transaction() as db:
            authenticate(db, request, "admin").revoked_at = time.time()
        response = JSONResponse({"ok": True})
        response.delete_cookie(COOKIES["admin"])
        return response

    @app.get("/api/admin/me")
    def admin_me(request: Request):
        with database.read() as db:
            return {"id": authenticate(db, request, "admin").subject_id}

    @app.get("/api/admin/studies")
    def list_studies(request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            return [
                study_view(db, study) for study in db.scalars(select(Study).order_by(Study.updated_at.desc()))
            ]

    @app.post("/api/admin/studies")
    def create_study(body: StudyInput, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")

            def operation():
                study = Study(title=body.title)
                db.add(study)
                db.flush()
                version = StudyVersion(study_id=study.id, number=1, config=body.model_dump(mode="json"))
                db.add(version)
                db.flush()
                study.current_version_id = version.id
                return study_view(db, study)

            return idempotent(db, request, auth.subject_id, body.model_dump(mode="json"), operation)

    @app.get("/api/admin/studies/{study_id}")
    def get_study(study_id: str, request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            study = db.get(Study, study_id)
            if not study:
                raise AppError("NOT_FOUND", "研究不存在", 404)
            return study_view(db, study)

    @app.post("/api/admin/studies/{study_id}/versions")
    def new_version(study_id: str, body: StudyInput, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            study = db.get(Study, study_id)
            if not study:
                raise AppError("NOT_FOUND", "研究不存在", 404)

            def operation():
                number = (
                    db.scalar(select(func.max(StudyVersion.number)).where(StudyVersion.study_id == study_id))
                    or 0
                ) + 1
                version = StudyVersion(study_id=study_id, number=number, config=body.model_dump(mode="json"))
                db.add(version)
                db.flush()
                study.current_version_id, study.title, study.updated_at = version.id, body.title, time.time()
                return study_view(db, study)

            return idempotent(db, request, auth.subject_id, body.model_dump(mode="json"), operation)

    @app.post("/api/admin/studies/{study_id}/archive")
    def archive(study_id: str, body: dict, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            study = db.get(Study, study_id)
            if not study:
                raise AppError("NOT_FOUND", "研究不存在", 404)
            if not isinstance(body.get("archived"), bool):
                raise AppError("INVALID_INPUT", "请指定归档状态")

            def operation():
                study.archived = body["archived"]
                return study_view(db, study)

            return idempotent(db, request, auth.subject_id, body, operation)

    @app.post("/api/admin/studies/{study_id}/invites")
    def invite(study_id: str, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "admin")
            study = db.get(Study, study_id)
            if not study or study.archived:
                raise AppError("STUDY_UNAVAILABLE", "研究不存在或已归档", 409)

            def operation():
                token = secrets.token_urlsafe(32)
                row = Invite(
                    study_version_id=study.current_version_id,
                    token_hash=digest(token),
                    expires_at=time.time() + 7 * 86400,
                )
                db.add(row)
                return {
                    "url": f"{settings.public_base_url.rstrip('/')}/join#token={token}",
                    "expires_at": iso(row.expires_at),
                }

            return idempotent(db, request, auth.subject_id, {}, operation, confidential=True)

    @app.post("/api/participant/exchange")
    def exchange(body: dict, request: Request):
        with database.transaction() as db:
            allowed = rate_limit(db, "exchange:" + digest(request.client.host), maximum=30)
        if not allowed:
            raise AppError("RATE_LIMITED", "邀请兑换过于频繁，请稍后重试", 429, True)
        token = body.get("token")
        if not isinstance(token, str) or len(token) > 256:
            raise AppError("INVALID_INVITE", "邀请无效", 403)
        with database.transaction() as db:
            row = db.scalar(select(Invite).where(Invite.token_hash == digest(token)))
            if not row or row.revoked_at is not None or row.expires_at <= time.time():
                raise AppError("INVALID_INVITE", "邀请已到期、已撤销或不存在", 403)
            if row.redeemed_at is not None:
                raise AppError("INVITE_USED", "邀请已经兑换，请向研究者索取恢复邀请", 409)
            version = db.get(StudyVersion, row.study_version_id)
            if row.session_id:
                session = require_session(db, row.session_id)
            else:
                session = InterviewSession(
                    study_id=version.study_id,
                    study_version_id=version.id,
                    participant_code="P-" + secrets.token_hex(4).upper(),
                    budget_micro=micro(version.config["budget_cny"]),
                    target_seconds=version.config["target_minutes"] * 60,
                )
                db.add(session)
                db.flush()
            row.session_id, row.redeemed_at = session.id, time.time()
            auth_token, duration = create_auth(db, "participant", session.id)
            response = JSONResponse({"session_id": session.id})
            set_cookie(response, settings, "participant", auth_token, duration)
            return response

    @app.get("/api/participant/session")
    def participant_detail(request: Request):
        with database.read() as db:
            auth = authenticate(db, request, "participant")
            return detail(db, require_session(db, auth.subject_id), settings, admin=False)

    @app.post("/api/participant/consent")
    def consent(body: ConsentInput, request: Request):
        with database.transaction() as db:
            auth = authenticate(db, request, "participant")
            session = require_session(db, auth.subject_id)
            if body.version != settings.consent_version or not body.processing or not body.permanent:
                raise AppError("CONSENT_REQUIRED", "必须主动接受当前版本的数据处理及永久保存条件", 403)
            if session.status in TERMINAL:
                raise AppError("SESSION_ENDED", "访谈已结束", 409)

            def operation():
                if session.status == "pending_consent":
                    active = db.scalar(
                        select(func.count())
                        .select_from(InterviewSession)
                        .where(InterviewSession.status.in_(["ready", "in_progress", "finalizing"]))
                    )
                    if active >= settings.max_active_sessions:
                        raise AppError("CAPACITY_REACHED", "当前访谈已达两场，请稍后再试", 409, True)
                    session.status = "ready"
                session.consent_version, session.mode = body.version, body.mode
                session.consent_snapshot = settings.consent_snapshot()
                session.processing_consent = session.permanent_consent = True
                db.add(
                    Consent(
                        session_id=session.id,
                        version=body.version,
                        mode=body.mode,
                        processing=True,
                        permanent=True,
                        snapshot=settings.consent_snapshot(),
                    )
                )
                job = enqueue(db, "decide", {"first": True}, session.id, f"first:{session.id}")
                for waiting in db.scalars(
                    select(Job).where(
                        Job.session_id == session.id,
                        Job.status == "failed",
                        Job.error_code == "CONSENT_REQUIRED",
                    )
                ):
                    waiting.status, waiting.error_code, waiting.error_message = "queued", None, None
                emit(db, session, "consent.accepted")
                return {"session_id": session.id, "job_id": job.id}

            return idempotent(db, request, session.id, body.model_dump(), operation, session.id)

    @app.get("/api/admin/studies/{study_id}/sessions")
    def study_sessions(study_id: str, request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            return [
                session_view(s)
                for s in db.scalars(
                    select(InterviewSession)
                    .where(InterviewSession.study_id == study_id, InterviewSession.status.not_in(DELETED))
                    .order_by(InterviewSession.created_at.desc())
                )
            ]

    @app.get("/api/admin/sessions/{sid}")
    def admin_detail(sid: str, request: Request):
        with database.read() as db:
            authenticate(db, request, "admin")
            return detail(db, require_session(db, sid), settings)

    # Feature routers remain independent of the application factory for test isolation.
    from app.routes_session import register_session_routes

    register_session_routes(app, database, settings)

    @app.get("/{path:path}", include_in_schema=False)
    def frontend(path: str):
        if path.startswith("api/"):
            raise AppError("NOT_FOUND", "接口不存在", 404)
        dist = ROOT / "frontend" / "dist"
        candidate = (dist / path).resolve()
        if dist.resolve() in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        if (dist / "index.html").is_file():
            return FileResponse(dist / "index.html")
        return JSONResponse(
            {"message": "前端尚未构建，请在 frontend 执行 npm run build", "mode": settings.mode},
            status_code=503,
        )

    return app
