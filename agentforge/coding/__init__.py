"""Coding mode — map-reduce code transformation pipeline.

This package is deliberately sparse in Phase 1. Later phases fill in:

- ``coding.driver``   — executes a plan of tool steps
- ``coding.burst``    — parallel per-file LLM bursts via asyncio.gather
- ``coding.planner``  — optional LLM planner + JSON plan validator
- ``coding.rollback`` — Redis-backed burst-ID → snapshot-ID map

See ``.claude/2026-04-24-coding-mode-design.md`` for the full design and
``.claude/2026-04-24-coding-mode-plan.md`` for the phase breakdown.
"""
