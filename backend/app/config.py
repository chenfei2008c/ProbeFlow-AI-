from pathlib import Path
import hashlib
import json
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]
CONSENT_VERSION = "V1.1-2026-09-16"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", case_sensitive=False)
    mode: Literal["mock", "live"] = "mock"
    data_dir: Path = Path.home() / "Library" / "Application Support" / "ProbeFlow"
    backup_dir: Path | None = None
    public_base_url: str = "http://localhost:8765"
    allowed_origins: str = "http://localhost:8765,http://127.0.0.1:8765,http://localhost:5173"
    admin_password: SecretStr | None = None
    retention_policy: Literal["permanent"] = "permanent"
    worker_enabled: bool = True
    monthly_limit_cny: str = "100.00"
    max_active_sessions: int = Field(default=2, ge=1, le=2)
    min_free_bytes: int = Field(default=100 * 1024 * 1024, ge=0)
    temp_retention_hours: int = Field(default=24, ge=1)
    backup_count: int = Field(default=7, ge=1)
    provider_timeout_seconds: float = Field(default=45, gt=0, le=120)
    price_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)
    asr_provider: str = "bailian"
    interview_provider: str = "bailian"
    tts_provider: str = "bailian"
    report_provider: str = "bailian"
    asr_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    interview_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    tts_base_url: str = "https://dashscope.aliyuncs.com/api/v1"
    report_base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    asr_model: str = "qwen3-asr-flash"
    interview_model: str = "qwen-plus"
    tts_model: str = "qwen3-tts-flash"
    report_model: str = "qwen-plus"
    asr_key_env: str = "DASHSCOPE_API_KEY"
    interview_key_env: str = "DASHSCOPE_API_KEY"
    tts_key_env: str = "DASHSCOPE_API_KEY"
    report_key_env: str = "DASHSCOPE_API_KEY"
    provider_region: str = "cn-beijing"
    tts_voice: str = "Cherry"

    @field_validator("price_overrides")
    @classmethod
    def known_price_roles(cls, value):
        if set(value) - {"asr", "interview", "tts", "report"}:
            raise ValueError("价格配置只接受 asr、interview、tts、report 四种角色")
        return value

    @field_validator("asr_base_url", "interview_base_url", "tts_base_url", "report_base_url")
    @classmethod
    def safe_provider_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("供应商地址必须使用 HTTPS，且不能含凭证、查询参数或片段")
        return value.rstrip("/")

    @field_validator("data_dir", "backup_dir")
    @classmethod
    def outside_repository(cls, value):
        if value is None:
            return None
        path = value.expanduser().resolve()
        if path == ROOT or ROOT in path.parents:
            raise ValueError("访谈数据和备份必须位于源码目录之外")
        return path

    @model_validator(mode="after")
    def validate_deployment(self):
        from decimal import Decimal, InvalidOperation

        try:
            amount = Decimal(self.monthly_limit_cny)
            if not amount.is_finite() or amount <= 0:
                raise ValueError("月份预算必须为正数")
        except InvalidOperation as exc:
            raise ValueError("月份预算格式错误") from exc
        parsed = urlsplit(self.public_base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username:
            raise ValueError("公开地址必须是有效的 HTTP/HTTPS 地址")
        if parsed.scheme != "https" and parsed.hostname not in {
            "localhost",
            "127.0.0.1",
            "::1",
            "testserver",
        }:
            raise ValueError("远程访谈必须配置 HTTPS")
        if self.admin_password and len(self.admin_password.get_secret_value()) < 12:
            raise ValueError("管理员密码至少 12 个字符")
        return self

    @property
    def secure_cookie(self):
        return self.public_base_url.startswith("https://")

    @property
    def origins(self):
        return {x.strip().rstrip("/") for x in self.allowed_origins.split(",")} | {
            self.public_base_url.rstrip("/")
        }

    @property
    def backups(self):
        return self.backup_dir or self.data_dir.parent / f"{self.data_dir.name}-backups"

    def public_providers(self):
        return {
            role: {
                "provider": getattr(self, f"{role}_provider"),
                "model": getattr(self, f"{role}_model"),
                "region": self.provider_region,
                "endpoint_host": urlsplit(getattr(self, f"{role}_base_url")).hostname,
            }
            for role in ("asr", "interview", "tts", "report")
        }

    def consent_snapshot(self):
        return {
            "notice_version": CONSENT_VERSION,
            "mode": self.mode,
            "retention": "permanent",
            "providers": {
                role: {**provider, "base_url": getattr(self, f"{role}_base_url").rstrip("/")}
                for role, provider in self.public_providers().items()
            },
        }

    @property
    def consent_version(self):
        # Credential rotation does not change recipients; credentials are never
        # copied into a consent record or exposed to participants.
        payload = json.dumps(self.consent_snapshot(), ensure_ascii=False, sort_keys=True)
        return CONSENT_VERSION + "-" + hashlib.sha256(payload.encode()).hexdigest()[:12]
