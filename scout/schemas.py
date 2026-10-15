"""Public TikTok evidence and attributed, explicitly reviewed interpretations."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    """extra=forbid：清单里写错字段名直接报错，不被静默忽略。"""
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)


class Competitor(StrictModel):
    brand: str = Field(min_length=1)
    handle: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.]{1,64}$")
    identity: Literal["official_link", "user_supplied", "unverified"] = "unverified"
    official_source: str = ""
    identity_checked_at: str = ""
    seed_videos: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def official_needs_source(self):
        if self.identity == "official_link" and not (
            self.official_source.startswith("https://") and self.identity_checked_at
        ):
            raise ValueError("official_link requires official_source and identity_checked_at")
        return self


class ResearchSpec(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    product_context: str = ""
    competitors: list[Competitor] = Field(min_length=1, max_length=50)
    max_videos: int = Field(default=20, ge=1, le=100)
    recent_days: int = Field(default=90, ge=1, le=3650)

    @model_validator(mode="after")
    def unique_competitors(self):
        keys = [c.handle or c.brand for c in self.competitors]
        if len(keys) != len(set(keys)):
            raise ValueError("competitor handles/keys must be unique")
        return self


class VideoEvidence(StrictModel):
    id: str
    brand: str
    handle: str
    url: str
    caption: str = ""
    published_at: str | None = None
    captured_at: str
    views: int | None = None
    likes: int | None = None
    comments: int | None = None
    shares: int | None = None
    duration_s: float | None = None
    # 发布地（TikTok 的 locationCreated）。是内容来源地，不是观众所在地，
    # 更不是受众证据。缺失时留空，不猜。
    region: str = ""
    source: Literal["tiktok_http_detail", "tiktok_http_embed"]
    status: Literal["ok", "partial"]
    detail_error: str = ""
    review_scope: str = "caption_and_metadata_only"
    paid_distribution: str = "unknown"


class Finding(StrictModel):
    handle: str
    worldview: str = Field(min_length=1)
    audience_tension: str = Field(min_length=1)
    product_role: str = Field(min_length=1)
    recurring_formats: list[str] = Field(min_length=1)
    hook: str = ""
    evidence_ids: list[str] = Field(min_length=1)
    counterevidence_ids: list[str] = Field(default_factory=list)
    review_scope: Literal["caption_and_metadata_only", "partial_video", "full_video"]
    review_notes: str = Field(min_length=1)
    limitations: str = Field(min_length=1)
    adaptation: str = Field(min_length=1)


class ContentIdea(StrictModel):
    title: str = Field(min_length=1)
    hook: str = Field(min_length=1)
    story: str = Field(min_length=1)
    product_proof: str = Field(min_length=1)
    ai_ugc_execution: str = Field(min_length=1)
    evidence_ids: list[str] = Field(min_length=1)
    hypothesis: str = Field(min_length=1)


class WorldviewAnalysis(StrictModel):
    # A human/agent writes this after reading the evidence. No paid model is called.
    analyst: str = Field(min_length=1)
    snapshot_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    findings: list[Finding] = Field(min_length=1)
    ideas: list[ContentIdea] = Field(default_factory=list)

    @field_validator("findings")
    @classmethod
    def one_finding_per_handle(cls, values):
        if len({v.handle for v in values}) != len(values):
            raise ValueError("only one finding per handle")
        return values
