import hashlib
import secrets
import time

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import select

from app.errors import AppError
from app.models import Auth, Owner, RequestLimit

HASHER = PasswordHasher()
COOKIES = {"admin": "probeflow_admin", "participant": "probeflow_participant"}


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def initialize_owner(db, settings):
    if not db.scalar(select(Owner)) and settings.admin_password:
        db.add(Owner(password_hash=HASHER.hash(settings.admin_password.get_secret_value())))


def login(db, password):
    owner = db.scalar(select(Owner))
    if not owner:
        raise AppError("ADMIN_NOT_INITIALIZED", "请先在运行终端初始化管理员密码", 409)
    try:
        HASHER.verify(owner.password_hash, password)
    except VerificationError:
        raise AppError("INVALID_CREDENTIALS", "密码不正确", 401) from None
    return owner


def create_auth(db, role, subject_id):
    token = secrets.token_urlsafe(32)
    duration = 12 * 3600 if role == "admin" else 7 * 86400
    db.add(
        Auth(token_hash=digest(token), role=role, subject_id=subject_id, expires_at=time.time() + duration)
    )
    return token, duration


def authenticate(db, request, role):
    raw = request.cookies.get(COOKIES[role])
    auth = db.scalar(select(Auth).where(Auth.token_hash == digest(raw))) if raw else None
    if not auth or auth.role != role or auth.revoked_at is not None or auth.expires_at <= time.time():
        raise AppError("UNAUTHORIZED", "请重新登录或使用有效的访谈邀请", 401)
    return auth


def set_cookie(response, settings, role, token, max_age):
    response.set_cookie(
        COOKIES[role],
        token,
        max_age=max_age,
        httponly=True,
        secure=settings.secure_cookie,
        samesite="strict",
        path="/",
    )


def rate_limit(db, key, maximum=10, window_seconds=60):
    now = time.time()
    row = db.get(RequestLimit, key)
    if not row:
        row = RequestLimit(key=key, count=0, window=now)
        db.add(row)
    if now - row.window >= window_seconds:
        row.window, row.count = now, 0
    row.count += 1
    return row.count <= maximum
