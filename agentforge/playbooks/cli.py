"""Playbook CLI: (re)index the library, list entries, test a query.

Run with:  python -m agentforge.playbooks.cli reindex
           python -m agentforge.playbooks.cli list
           python -m agentforge.playbooks.cli match "why is my disk full"
"""

from __future__ import annotations

import argparse
import logging
import sys

from . import service


def _cmd_reindex(_args: argparse.Namespace) -> int:
    result = service.reindex()
    print(f"indexed={result['indexed']} skipped={result['skipped']} pruned={result['pruned']}")
    return 0


def _cmd_list(_args: argparse.Namespace) -> int:
    library = service.get_library(reload=True)
    if len(library) == 0:
        print("(no playbooks)")
        return 0
    for pb in library.playbooks.values():
        threshold = library.threshold_for(pb)
        print(f"{pb.id}  (threshold={threshold:.2f}, {len(pb.commands)} commands)")
        print(f"    {pb.description}")
    return 0


def _cmd_match(args: argparse.Namespace) -> int:
    matched = service.match(args.query)
    if matched is None:
        print("no match")
        return 1
    pb, score = matched
    print(f"matched: {pb.id}  (score={score:.3f})")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="agentforge-playbooks", description="Manage the playbook library.")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("reindex", help="Rebuild the Qdrant index from playbooks.yaml").set_defaults(func=_cmd_reindex)
    sub.add_parser("list", help="List loaded playbooks").set_defaults(func=_cmd_list)

    match_parser = sub.add_parser("match", help="Show the best-matching playbook for a query")
    match_parser.add_argument("query")
    match_parser.set_defaults(func=_cmd_match)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
