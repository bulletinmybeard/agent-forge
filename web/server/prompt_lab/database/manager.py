"""PromptLabDatabase — persistence manager for prompt_lab.db."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from sqlalchemy import create_engine, desc, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from .models import Base, PromptLabResult, PromptLabRun

logger = logging.getLogger(__name__)


def _set_sqlite_pragmas(dbapi_conn, connection_record):
    cursor = dbapi_conn.execute("PRAGMA journal_mode=DELETE")
    cursor.close()
    cursor = dbapi_conn.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def _generate_run_id() -> str:
    return str(uuid.uuid4())


class PromptLabDatabase:
    """SQLite-backed store for /api/prompt-lab/* runs."""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            f"sqlite:///{self.db_path}",
            echo=False,
            poolclass=NullPool,
            connect_args={"check_same_thread": False},
        )
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        self.SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=self.engine)
        logger.info("PromptLabDatabase initialised at %s", self.db_path)

    def create_tables(self) -> None:
        Base.metadata.create_all(bind=self.engine)

    # ── run operations ────────────────────────────────────────────────

    def save_run(
        self,
        *,
        system: str | None,
        prompt: str,
        total_latency_ms: int,
        results: list[dict],
    ) -> PromptLabRun:
        """Persist a completed run + its per-profile results. Returns the run.

        ``results`` is the list of dicts produced by the fan-out in api.py:
        each entry has ``profile, provider, model, content, latency_ms,
        prompt_tokens, completion_tokens, error``.
        """
        run_id = _generate_run_id()
        with self.SessionLocal() as session:
            run = PromptLabRun(
                id=run_id,
                system=system or None,
                prompt=prompt,
                total_latency_ms=int(total_latency_ms or 0),
            )
            for r in results:
                run.results.append(
                    PromptLabResult(
                        profile=str(r.get("profile", "")),
                        provider=str(r.get("provider", "") or ""),
                        model=str(r.get("model", "") or ""),
                        content=str(r.get("content", "") or ""),
                        latency_ms=int(r.get("latency_ms", 0) or 0),
                        prompt_tokens=int(r.get("prompt_tokens", 0) or 0),
                        completion_tokens=int(r.get("completion_tokens", 0) or 0),
                        error=r.get("error") if r.get("error") else None,
                    )
                )
            session.add(run)
            session.commit()
            session.refresh(run)
            # Eagerly read the children so to_dict works after expunge.
            _ = [r.id for r in run.results]
            session.expunge_all()
            logger.info("Saved prompt-lab run %s with %d results", run_id, len(results))
            return run

    def get_run(self, run_id: str) -> PromptLabRun | None:
        with self.SessionLocal() as session:
            run = session.query(PromptLabRun).filter_by(id=run_id).first()
            if run:
                _ = [r.id for r in run.results]  # force-load children
                session.expunge_all()
            return run

    def list_runs(self, limit: int = 20) -> list[PromptLabRun]:
        """Most-recent-first list for the history dropdown."""
        with self.SessionLocal() as session:
            runs = (
                session.query(PromptLabRun)
                .order_by(desc(PromptLabRun.created_at))
                .limit(max(1, min(int(limit), 200)))
                .all()
            )
            for run in runs:
                _ = [r.id for r in run.results]  # force-load for profile_count
            session.expunge_all()
            return runs

    def delete_run(self, run_id: str) -> bool:
        with self.SessionLocal() as session:
            run = session.query(PromptLabRun).filter_by(id=run_id).first()
            if not run:
                return False
            session.delete(run)
            session.commit()
            return True
