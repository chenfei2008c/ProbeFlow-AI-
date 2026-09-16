from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Topic(StrictModel):
    id: str = Field(min_length=1, max_length=64, pattern=r"^[\w-]+$")
    title: str = Field(min_length=1, max_length=100)
    research_question: str = Field(default="", max_length=500)
    priority: int = Field(default=1, ge=1, le=10)
    evidence_type: str = Field(default="具体事件", max_length=100)
    minutes: int = Field(default=10, ge=1, le=90)


class StudyInput(StrictModel):
    title: str = Field(min_length=1, max_length=80)
    objective: str = Field(min_length=1, max_length=2000)
    participant_description: str = Field(default="", max_length=500)
    target_minutes: Literal[15, 30, 45, 60] = 60
    language: Literal["zh-CN"] = "zh-CN"
    topics: list[Topic] = Field(min_length=1, max_length=10)
    exclusions: str = Field(default="", max_length=4000)
    glossary: list[str] = Field(default_factory=list, max_length=100)
    budget_cny: Decimal = Field(default=Decimal("5"), gt=0, le=10000, decimal_places=2)
    confirm_transcript: bool = True
    tone: str = Field(default="中性、尊重", max_length=100)
    retention: Literal["permanent"] = "permanent"

    @field_validator("topics")
    @classmethod
    def unique_topics(cls, value):
        if len({x.id for x in value}) != len(value):
            raise ValueError("主题 ID 不得重复")
        return value

    @field_validator("glossary")
    @classmethod
    def bounded_glossary(cls, value):
        if any(not x.strip() or len(x) > 100 for x in value):
            raise ValueError("术语应为 1—100 个字符")
        return value


class ConsentInput(StrictModel):
    version: str = Field(max_length=64)
    mode: Literal["text", "voice"]
    processing: bool
    permanent: bool


class TurnInput(StrictModel):
    input_mode: Literal["text", "voice"]
    text: str | None = Field(default=None, max_length=20000)
    mime_type: str | None = Field(default=None, max_length=80)


class TextInput(StrictModel):
    text: str = Field(min_length=1, max_length=20000)


class ChunkItem(StrictModel):
    seq: int = Field(ge=0, le=10000)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class FinalizeInput(StrictModel):
    chunks: list[ChunkItem] = Field(min_length=1, max_length=10000)


class ControlInput(StrictModel):
    action: Literal[
        "pause", "resume", "skip", "end", "withdraw", "extend", "retry", "playback_done", "rerecord"
    ]
    reason: str | None = Field(default=None, max_length=64)
    job_id: str | None = None
    accept_possible_charge: bool = False
    turn_id: str | None = None
    played_complete: bool | None = None


class BudgetInput(StrictModel):
    budget_cny: Decimal = Field(gt=0, le=10000, decimal_places=2)


class ReportInput(StrictModel):
    retry_job_id: str | None = Field(default=None, min_length=1, max_length=32)
    accept_possible_charge: bool = False
