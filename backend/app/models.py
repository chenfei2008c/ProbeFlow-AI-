import time
from uuid import uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def uid():
    return uuid4().hex


class Base(DeclarativeBase):
    pass


class Identity:
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    created_at: Mapped[float] = mapped_column(Float, default=time.time)


class Owner(Identity, Base):
    __tablename__ = "owner_accounts"
    singleton: Mapped[int] = mapped_column(Integer, unique=True, default=1)
    password_hash: Mapped[str] = mapped_column(Text)


class Auth(Identity, Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16))
    subject_id: Mapped[str] = mapped_column(String(32), index=True)
    expires_at: Mapped[float] = mapped_column(Float)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Study(Identity, Base):
    __tablename__ = "studies"
    title: Mapped[str] = mapped_column(String(80))
    archived: Mapped[bool] = mapped_column(Boolean, default=False)
    current_version_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    updated_at: Mapped[float] = mapped_column(Float, default=time.time)


class StudyVersion(Identity, Base):
    __tablename__ = "study_versions"
    __table_args__ = (
        UniqueConstraint("study_id", "number"),
        CheckConstraint("retention = 'permanent'", name="study_permanent"),
    )
    study_id: Mapped[str] = mapped_column(ForeignKey("studies.id"), index=True)
    number: Mapped[int] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSON)
    retention: Mapped[str] = mapped_column(String(16), default="permanent")
    prompt_version: Mapped[str] = mapped_column(String(32), default="V1.1")


class InterviewSession(Identity, Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("retention = 'permanent' AND expires_at IS NULL", name="session_permanent"),
    )
    study_id: Mapped[str] = mapped_column(ForeignKey("studies.id"), index=True)
    study_version_id: Mapped[str] = mapped_column(ForeignKey("study_versions.id"))
    participant_code: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="pending_consent", index=True)
    mode: Mapped[str] = mapped_column(String(16), default="text")
    consent_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    consent_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    processing_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    permanent_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    retention: Mapped[str] = mapped_column(String(16), default="permanent")
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    active_seconds: Mapped[float] = mapped_column(Float, default=0)
    last_heartbeat: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_seconds: Mapped[int] = mapped_column(Integer, default=3600)
    budget_micro: Mapped[int] = mapped_column(Integer, default=5_000_000)
    spent_micro: Mapped[int] = mapped_column(Integer, default=0)
    reserved_micro: Mapped[int] = mapped_column(Integer, default=0)
    pause_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    memory_version: Mapped[int] = mapped_column(Integer, default=0)
    price_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class Consent(Identity, Base):
    __tablename__ = "consents"
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    version: Mapped[str] = mapped_column(String(64))
    mode: Mapped[str] = mapped_column(String(16))
    processing: Mapped[bool] = mapped_column(Boolean)
    permanent: Mapped[bool] = mapped_column(Boolean)
    retention: Mapped[str] = mapped_column(String(16), default="permanent")
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class Invite(Identity, Base):
    __tablename__ = "invites"
    study_version_id: Mapped[str] = mapped_column(ForeignKey("study_versions.id"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[float] = mapped_column(Float)
    redeemed_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    revoked_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Turn(Identity, Base):
    __tablename__ = "turns"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    input_mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(32))
    revision_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    audio_asset_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    action: Mapped[str | None] = mapped_column(String(32), nullable=True)
    topic_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    played_complete: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    start_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    finalized_chunks: Mapped[list | None] = mapped_column(JSON, nullable=True)


class Revision(Identity, Base):
    __tablename__ = "transcript_revisions"
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(32))
    editor: Mapped[str] = mapped_column(String(32))
    previous_id: Mapped[str | None] = mapped_column(String(32), nullable=True)


class Asset(Identity, Base):
    __tablename__ = "audio_assets"
    __table_args__ = (
        CheckConstraint("retention = 'permanent' AND expires_at IS NULL", name="asset_permanent"),
    )
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True, index=True)
    turn_id: Mapped[str | None] = mapped_column(ForeignKey("turns.id"), nullable=True)
    path: Mapped[str] = mapped_column(Text)
    mime_type: Mapped[str] = mapped_column(String(80))
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str] = mapped_column(String(16))
    retention: Mapped[str] = mapped_column(String(16), default="permanent")
    expires_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    segments: Mapped[list] = mapped_column(JSON, default=list)


class UploadChunk(Identity, Base):
    __tablename__ = "upload_chunks"
    __table_args__ = (UniqueConstraint("turn_id", "seq"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str] = mapped_column(String(64))
    byte_size: Mapped[int] = mapped_column(Integer)
    path: Mapped[str] = mapped_column(Text)


class Event(Identity, Base):
    __tablename__ = "session_events"
    __table_args__ = (UniqueConstraint("session_id", "seq"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, default=dict)


class Memory(Identity, Base):
    __tablename__ = "memory_snapshots"
    __table_args__ = (UniqueConstraint("session_id", "version"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    through_seq: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict] = mapped_column(JSON)


class Job(Identity, Base):
    __tablename__ = "jobs"
    session_id: Mapped[str | None] = mapped_column(ForeignKey("sessions.id"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(32))
    dedup_key: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[float | None] = mapped_column(Float, nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(32), nullable=True)
    call_started: Mapped[bool] = mapped_column(Boolean, default=False)
    next_run: Mapped[float] = mapped_column(Float, default=time.time)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    finished_at: Mapped[float | None] = mapped_column(Float, nullable=True)


class Ledger(Identity, Base):
    __tablename__ = "usage_ledger"
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    role: Mapped[str] = mapped_column(String(16))
    provider: Mapped[str] = mapped_column(String(32))
    model: Mapped[str] = mapped_column(String(100))
    region: Mapped[str] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(40))
    usage: Mapped[dict] = mapped_column(JSON, default=dict)
    amount_micro: Mapped[int | None] = mapped_column(Integer, nullable=True)
    amount_source: Mapped[str] = mapped_column(String(16))
    price_snapshot: Mapped[dict] = mapped_column(JSON)
    month: Mapped[str] = mapped_column(String(7), index=True)


class Reservation(Identity, Base):
    __tablename__ = "budget_reservations"
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    amount_micro: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(16), default="held")
    call_meta: Mapped[dict] = mapped_column(JSON, default=dict)
    month: Mapped[str] = mapped_column(String(7), index=True)


class Report(Identity, Base):
    __tablename__ = "reports"
    __table_args__ = (UniqueConstraint("session_id", "version"),)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="ready")
    source_updated: Mapped[bool] = mapped_column(Boolean, default=False)
    source_revision: Mapped[int] = mapped_column(Integer)
    body: Mapped[dict] = mapped_column(JSON)
    markdown: Mapped[str] = mapped_column(Text)


class Citation(Identity, Base):
    __tablename__ = "report_citations"
    report_id: Mapped[str] = mapped_column(ForeignKey("reports.id"), index=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("transcript_revisions.id"))
    turn_id: Mapped[str] = mapped_column(ForeignKey("turns.id"))
    start: Mapped[int] = mapped_column(Integer)
    end: Mapped[int] = mapped_column(Integer)
    quote: Mapped[str] = mapped_column(Text)


class RequestRecord(Identity, Base):
    __tablename__ = "idempotency_requests"
    __table_args__ = (UniqueConstraint("scope", "key"),)
    scope: Mapped[str] = mapped_column(String(250))
    key: Mapped[str] = mapped_column(String(128))
    body_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)


class RequestLimit(Base):
    __tablename__ = "request_limits"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)
    window: Mapped[float] = mapped_column(Float)
