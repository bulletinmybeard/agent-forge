#!/bin/sh
# clean.sh — purge generated/cache artifacts from the working tree.
#
# Safe + idempotent: only removes regenerable, gitignored cruft (caches, .pyc,
# build output, .DS_Store, coverage). NEVER touches tracked source. Run it
# before publishing, or any time you want a tidy `git status`.
#
#   scripts/clean.sh           remove the cruft
#   scripts/clean.sh --check   list what WOULD be removed, remove nothing
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$ROOT"

CHECK=0
case "${1:-}" in
  --check) CHECK=1 ;;
  "")      ;;
  *)       echo "usage: $0 [--check]" >&2; exit 2 ;;
esac

# Generated dirs (by exact name, recursive) + file globs. All are gitignored.
# .git and .venv are pruned so we never descend into them.
list() {
  find . \
    \( -path ./.git -o -path ./.venv \) -prune -o \
    \( -type d \( -name __pycache__ -o -name .pytest_cache -o -name .mypy_cache \
                  -o -name .ruff_cache -o -name .hatch -o -name .tox \
                  -o -name htmlcov -o -name '*.egg-info' \) \
       -o -type f \( -name '.DS_Store' -o -name '*.pyc' -o -name '*.pyo' \
                  -o -name '*.orig' -o -name '*.rej' -o -name '.coverage' \
                  -o -name 'coverage.xml' \) \) \
    -print 2>/dev/null
}

targets=$(list)
if [ -z "$targets" ]; then
  echo "* working tree already clean — nothing to remove"
  exit 0
fi
count=$(printf '%s\n' "$targets" | grep -c .)

if [ "$CHECK" -eq 1 ]; then
  echo "i would remove $count item(s):"
  printf '%s\n' "$targets" | sed 's/^/  - /'
  exit 0
fi

printf '%s\n' "$targets" | while IFS= read -r p; do
  rm -rf -- "$p" && echo "  x removed $p"
done
echo "* done — removed $count item(s)"
