"""SQLAlchemy models for prompt_lab.db — multi-provider comparison runs.

Two tables:
  PromptLabRun      — one per /api/prompt-lab/run invocation
  PromptLabResult   — one per profile targeted by that run
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, relationship


def _now() -> datetime:
    return datetime.now()


class Base(DeclarativeBase):
    pass


class PromptLabRun(Base):
    __tablename__ = "prompt_lab_runs"

    id = Column(String(36), primary_key=True)  # UUID4
    system = Column(Text, nullable=True)
    prompt = Column(Text, nullable=False)
    total_latency_ms = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=_now, nullable=False)

    results = relationship(
        "PromptLabResult",
        back_populates="run",
        cascade="all, delete-orphan",
        order_by="PromptLabResult.id",
    )

    def to_dict(self, *, include_results: bool = True) -> dict:
        out: dict = {
            "id": self.id,
            "system": self.system,
            "prompt": self.prompt,
            "total_latency_ms": int(self.total_latency_ms or 0),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
        if include_results:
            out["results"] = [r.to_dict() for r in (self.results or [])]
            out["profile_count"] = len(out["results"])
        else:
            # Lightweight preview for the list endpoint.
            out["profile_count"] = len(self.results or [])
        return out


class PromptLabResult(Base):
    __tablename__ = "prompt_lab_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        String(36),
        ForeignKey("prompt_lab_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    profile = Column(String(120), nullable=False)
    provider = Column(String(50), nullable=True)
    model = Column(String(200), nullable=True)
    content = Column(Text, nullable=True)
    latency_ms = Column(Integer, nullable=False, default=0)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    error = Column(Text, nullable=True)

    run = relationship("PromptLabRun", back_populates="results")

    def to_dict(self) -> dict:
        return {
            "profile": self.profile,
            "provider": self.provider or "",
            "model": self.model or "",
            "content": self.content or "",
            "latency_ms": int(self.latency_ms or 0),
            "prompt_tokens": int(self.prompt_tokens or 0),
            "completion_tokens": int(self.completion_tokens or 0),
            "error": self.error,
        }
