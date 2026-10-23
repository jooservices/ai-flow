#!/usr/bin/env python3
"""CI-only entry point for the backup/restore acceptance case; not registered as an operator command."""
from __future__ import annotations

import argparse
from pathlib import Path

from aiflow.v4.core.settings import Settings
from aiflow.v4.install import backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("create", "restore"))
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    settings = Settings.from_env()
    try:
        manifest = (backup.create(settings.database_url, args.archive) if args.action == "create"
                    else backup.restore(settings.database_url, args.archive))
    except (backup.BackupError, OSError) as exc:
        parser.exit(1, f"acceptance backup {args.action} failed: {exc}\n")
    print(f"acceptance backup {args.action} passed ({len(manifest['tables'])} tables)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
