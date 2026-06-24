"""Pydantic request/response models for the Knowledge Database API."""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

VALID_CONTENT_TYPES = {
    "note",
    "reference",
    "documentation",
    "document",
    "cheatsheet",
    "snippet",
}


def _normalize_tags(tags: list[str] | None) -> list[str] | None:
    if tags is None:
        return None
    return [t.strip().lower() for t in tags if t.strip()]


class CreateEntryRequest(BaseModel):
    title: str = Field(..., max_length=200)
    content: str
    content_type: str
    language: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = None
    notes: str | None = None
    project: str = "Uncategorized"
    metadata: dict | None = None
    parent_id: str | None = None

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str) -> str:
        if v not in VALID_CONTENT_TYPES:
            raise ValueError(f"content_type must be one of: {', '.join(sorted(VALID_CONTENT_TYPES))}")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: list[str]) -> list[str]:
        return _normalize_tags(v) or []


class UpdateEntryRequest(BaseModel):
    title: str | None = None
    content: str | None = None
    content_type: str | None = None
    language: str | None = None
    tags: list[str] | None = None
    source_url: str | None = None
    notes: str | None = None
    project: str | None = None
    parent_id: str | None = None

    @field_validator("content_type")
    @classmethod
    def validate_content_type(cls, v: str | None) -> str | None:
        if v is not None and v not in VALID_CONTENT_TYPES:
            raise ValueError(f"content_type must be one of: {', '.join(sorted(VALID_CONTENT_TYPES))}")
        return v

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_tags(v)


class KnowledgeSearchRequest(BaseModel):
    query: str
    tags: list[str] | None = None
    content_type: str | None = None
    language: str | None = None
    project: str | None = None
    parent_id: str | None = None
    limit: int = Field(default=10, ge=1)  # clamped to 50 by cap_limit validator
    score_threshold: float | None = None

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_tags(v)

    @field_validator("limit")
    @classmethod
    def cap_limit(cls, v: int) -> int:
        return min(v, 50)


class BatchCreateRequest(BaseModel):
    entries: list[CreateEntryRequest] = Field(..., min_length=1, max_length=100)


class BulkDeleteRequest(BaseModel):
    tags: list[str] | None = None
    content_type: str | None = None
    before: str | None = None
    project: str | None = None

    @model_validator(mode="after")
    def at_least_one_filter(self) -> BulkDeleteRequest:
        if not self.tags and not self.content_type and not self.before and not self.project:
            raise ValueError("At least one filter (tags, content_type, before, or project) is required")
        return self

    @field_validator("tags", mode="before")
    @classmethod
    def normalize_tags(cls, v: list[str] | None) -> list[str] | None:
        return _normalize_tags(v)


class EntryResponse(BaseModel):
    id: str
    title: str
    content: str
    content_type: str
    language: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = None
    notes: str | None = None
    project: str = "Uncategorized"
    metadata: dict | None = None
    parent_id: str | None = None
    created_at: str
    updated_at: str


class SearchResultResponse(BaseModel):
    id: str
    score: float
    title: str
    content: str
    content_type: str
    language: str | None = None
    tags: list[str] = Field(default_factory=list)
    source_url: str | None = None
    notes: str | None = None
    project: str = "Uncategorized"
    metadata: dict | None = None
    parent_id: str | None = None
    created_at: str


class SearchResponse(BaseModel):
    query: str
    results: list[SearchResultResponse]
    count: int


class BatchResponse(BaseModel):
    job_id: str
    status: str
    entry_count: int


class BatchStatusResponse(BaseModel):
    job_id: str
    status: str
    indexed: int = 0
    skipped: int = 0
    deduped: int = 0
    errors: int = 0


class TagResponse(BaseModel):
    tag: str
    count: int


class StatsResponse(BaseModel):
    total_entries: int
    by_content_type: dict[str, int]
    recent_entries: int
    tag_count: int
