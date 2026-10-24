"""Local film library (SQLite + Qdrant) used by ``@film`` tools."""

from .recommend import merge_candidates
from .store import FilmStore, get_store

__all__ = ["FilmStore", "get_store", "merge_candidates"]
