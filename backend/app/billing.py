from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal, InvalidOperation

from sqlalchemy import func, select

from app.domain import micro
from app.errors import AppError
from app.models import InterviewSession, Ledger, Reservation

# Explicit pricing snapshot. Not a provider bill; configure/reverify before a live run.
PRICE_CARD = {
    "version": "bailian-beijing-2026-09-15",
    "source": "https://help.aliyun.com/zh/model-studio/model-pricing",
    "input_per_million": "0.8",
    "output_per_million": "2",
    "cached_per_million": "0.8",
    "asr_per_second": "0.00022",
    "tts_per_10000": "0.8",
}


def prices_for_role(settings, role):
    models = {
        "asr": "qwen3-asr-flash",
        "tts": "qwen3-tts-flash",
        "interview": "qwen-plus",
        "report": "qwen-plus",
    }
    units = {
        "asr": {"asr_per_second"},
        "tts": {"tts_per_10000"},
        "interview": {"input_per_million", "output_per_million", "cached_per_million"},
        "report": {"input_per_million", "output_per_million", "cached_per_million"},
    }
    model = getattr(settings, f"{role}_model")
    override = settings.price_overrides.get(role)
    if override is not None:
        required = units[role] | {"version", "source", "model", "region"}
        if set(override) != required or any(not override[key] for key in required):
            raise AppError("PRICE_NOT_CONFIGURED", f"{role} 价格配置字段不完整或无效", 409)
        if override["model"] != model or override["region"] != settings.provider_region:
            raise AppError("PRICE_NOT_CONFIGURED", f"{role} 价格配置与模型或地区不匹配", 409)
        try:
            for unit in units[role]:
                value = Decimal(override[unit])
                if not value.is_finite() or value < 0:
                    raise InvalidOperation
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise AppError("PRICE_NOT_CONFIGURED", f"{role} 价格必须为非负有限数值", 409) from exc
        return dict(override)
    if settings.mode == "live" and (model != models[role] or settings.provider_region != "cn-beijing"):
        raise AppError("PRICE_NOT_CONFIGURED", f"请先为 {role} 当前模型和地区配置价格快照", 409)
    return {key: PRICE_CARD[key] for key in units[role] | {"version", "source"}} | {
        "model": model,
        "region": settings.provider_region,
    }


def month_key():
    return datetime.now(UTC).strftime("%Y-%m")


def billable_characters(text):
    return sum(2 if ("\u3400" <= c <= "\u9fff" or "\U00020000" <= c <= "\U0003134f") else 1 for c in text)


def calculate(role, usage, prices=None):
    prices = prices or PRICE_CARD
    if role == "asr":
        amount = Decimal(str(usage.get("audio_seconds", 0))) * Decimal(prices["asr_per_second"]) * 1_000_000
    elif role == "tts":
        amount = Decimal(usage.get("characters", 0)) * Decimal(prices["tts_per_10000"]) * 100
    else:
        input_tokens = max(0, int(usage.get("input_tokens", 0)))
        cached = min(input_tokens, max(0, int(usage.get("cached_tokens", 0))))
        # Reasoning tokens are a subset of completion/output tokens, never added twice.
        amount = (
            Decimal(input_tokens - cached) * Decimal(prices["input_per_million"])
            + Decimal(cached) * Decimal(prices["cached_per_million"])
            + Decimal(max(0, int(usage.get("output_tokens", 0)))) * Decimal(prices["output_per_million"])
        )
    return int(amount.to_integral_value(rounding=ROUND_CEILING))


def totals(db, month=None):
    month = month or month_key()
    spent = db.scalar(select(func.coalesce(func.sum(Ledger.amount_micro), 0)).where(Ledger.month == month))
    held = db.scalar(
        select(func.coalesce(func.sum(Reservation.amount_micro), 0)).where(
            Reservation.month == month, Reservation.state.in_(["held", "unknown"])
        )
    )
    return int(spent), int(held)


def reserve(db, settings, job, amount, role=None, prices=None):
    if amount < 0:
        raise ValueError("negative reservation")
    spent, held = totals(db)
    session = db.get(InterviewSession, job.session_id) if job.session_id else None
    if spent + held + amount > micro(settings.monthly_limit_cny) or (
        session and session.spent_micro + session.reserved_micro + amount > session.budget_micro
    ):
        raise AppError("BUDGET_EXCEEDED", "预算不足，已暂停新的付费请求；仍可导出已有记录", 409)
    role = role or {"asr": "asr", "tts": "tts", "report": "report"}.get(job.kind, "interview")
    prices = dict(prices or prices_for_role(settings, role))
    reservation = Reservation(
        session_id=job.session_id,
        job_id=job.id,
        amount_micro=amount,
        month=month_key(),
        call_meta={
            "role": role,
            "attempt": job.attempt,
            "prices": prices,
            "provider": getattr(settings, f"{role}_provider"),
            "model": getattr(settings, f"{role}_model"),
            "region": settings.provider_region,
            "mode": settings.mode,
        },
    )
    db.add(reservation)
    if session:
        session.reserved_micro += amount
        session.price_snapshot = {**session.price_snapshot, role: prices}
    db.flush()
    return reservation


def settle(db, settings, job, reservation, role, usage, status="succeeded", prices=None):
    if reservation.state not in {"held"}:
        raise AppError("SETTLEMENT_CONFLICT", "该请求预算已结算", 409)
    session = db.get(InterviewSession, job.session_id) if job.session_id else None
    meta = reservation.call_meta or {}
    prices = meta.get("prices") or prices or PRICE_CARD
    mode = meta.get("mode", settings.mode)
    unknown = status == "external_status_unknown"
    failed = status == "failed"
    amount = None if unknown else 0 if failed or mode == "mock" else calculate(role, usage, prices)
    reservation.state = "unknown" if unknown else "released" if failed else "settled"
    if session:
        if not unknown:
            session.reserved_micro -= reservation.amount_micro
            session.spent_micro += amount
    source = "unknown" if unknown else "mock" if mode == "mock" else usage.get("source", "estimated")
    entry = Ledger(
        session_id=job.session_id,
        job_id=job.id,
        role=role,
        provider=meta.get("provider", getattr(settings, f"{role}_provider")),
        model=meta.get("model", getattr(settings, f"{role}_model")),
        region=meta.get("region", settings.provider_region),
        request_id=usage.get("request_id"),
        attempt=job.attempt,
        status=status,
        usage=usage,
        amount_micro=amount,
        amount_source=source,
        price_snapshot=dict(prices),
        month=reservation.month,
    )
    db.add(entry)
    db.flush()
    return entry
