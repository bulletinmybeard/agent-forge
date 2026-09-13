"""Read-only tool list for @review. Writes go through @agent after apply-intent."""

REVIEW_TOOLS = [
    "read_file",
    "find_files",
    "grep_text",
    "git_diff",
    "git_log",
    "git_status",
    "git_blame",
]
