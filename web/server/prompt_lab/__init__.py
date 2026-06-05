"""Prompt Lab persistence — saves multi-provider comparison runs to ``prompt_lab.db``.

One row per run (with system + user prompt + total latency) and one child
row per per-profile result. Used by ``/api/prompt-lab/*`` endpoints; the UI
routes ``/test-tool/prompt-lab/:runId`` pull from here so runs are
bookmarkable and shareable.
"""
