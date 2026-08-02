"""Playbook data structures.

Pure dataclasses — no jinja2/qdrant/ollama imports, so this module (and the
loader) stay importable in CI/lint contexts without the heavy deps or a
config.yaml.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

SYNTHESIS_MODES = ("diagnostic", "informational")


@dataclass
class PlaybookCommand:
    """One diagnostic command in a playbook.

    ``id`` is the placeholder key referenced from the Jinja template
    (``commands.<id>.output``); it must be unique within a playbook.
    """

    id: str
    command: str
    cwd: str = "/tmp"


@dataclass
class Playbook:
    """A curated command-combination + output template for one diagnostic case."""

    id: str
    description: str
    trigger_examples: list[str] = field(default_factory=list)
    commands: list[PlaybookCommand] = field(default_factory=list)
    template_path: str = ""  # as declared in playbooks.yaml (relative)
    template_text: str = ""  # resolved template contents
    score_threshold: float | None = None  # per-playbook override of the global default
    # "diagnostic" (default) → discovery's cleanup-plan synthesis; "informational"
    # → a descriptive analysis with no forced fix recommendations.
    synthesis: str = "diagnostic"

    def index_text(self) -> str:
        """Text embedded for semantic retrieval: description + trigger examples.

        Commands and template are deliberately excluded — retrieval matches on
        what the case *is about*, not on the shell it happens to run.
        """
        parts = [self.description.strip()]
        parts.extend(e.strip() for e in self.trigger_examples)
        return "\n".join(p for p in parts if p)

    def content_hash(self) -> str:
        """SHA-256 of the retrieval text — drives content-addressed re-embedding."""
        return hashlib.sha256(self.index_text().encode("utf-8")).hexdigest()


@dataclass
class PlaybookLibrary:
    """Loaded set of playbooks plus the global match threshold."""

    playbooks: dict[str, Playbook] = field(default_factory=dict)
    default_threshold: float = 0.55

    def get(self, playbook_id: str) -> Playbook | None:
        return self.playbooks.get(playbook_id)

    def threshold_for(self, playbook: Playbook) -> float:
        return playbook.score_threshold if playbook.score_threshold is not None else self.default_threshold

    def __len__(self) -> int:
        return len(self.playbooks)
