"""Console entry point for chat-database migrations.

Installed as ``agentforge-db`` when the package is installed editable
(``pip install -e .``). In package-mode Poetry envs you can also run::

    python -m web.server.database.cli upgrade
"""

from __future__ import annotations


def main() -> None:
    from web.server.database.cli import main as cli_main

    raise SystemExit(cli_main())


if __name__ == "__main__":
    main()
