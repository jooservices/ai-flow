"""Shared helpers of the read-only audit tools (report, quota, preflight, reconcile, fleet). None of them writes anywhere."""
from __future__ import annotations

import os
import tomllib
from pathlib import Path

from .config import read_env_file


def workspaces_root(start: Path | None = None) -> Path:
    """The Workspaces checkout: `AIFLOW_WORKSPACES`, else the nearest parent holding AGENTS.md and scripts/jira."""
    if os.environ.get("AIFLOW_WORKSPACES"):
        return Path(os.environ["AIFLOW_WORKSPACES"]).resolve()
    here = (start or Path(__file__)).resolve()
    for parent in [here, *here.parents]:
        if (parent / "AGENTS.md").is_file() and (parent / "scripts" / "jira").exists():
            return parent
    raise RuntimeError("Workspaces root not found; set AIFLOW_WORKSPACES.")


def env_values(root: Path) -> dict[str, str]:
    """The private root .env overlaid by the process environment (values are never printed by the tools)."""
    return {**read_env_file(root / ".env"), **{k: v for k, v in os.environ.items() if k.startswith(("AIFLOW_", "WORKSPACES_"))}}


def config_dir(root: Path) -> Path:
    return Path(os.environ.get("AIFLOW_CONFIG_DIR") or root / "JOOservices" / "config")


def runtime(root: Path) -> dict:
    path = config_dir(root) / "aiflow.toml"
    if not path.is_file():
        return {}
    return tomllib.loads(path.read_text(encoding="utf-8")).get("runtime", {})


def projects(root: Path) -> list[dict]:
    found = []
    directory = config_dir(root) / "aiflow"
    for path in sorted(directory.glob("*.toml")) if directory.is_dir() else []:
        table = tomllib.loads(path.read_text(encoding="utf-8")).get("project", {})
        if table.get("jira_key"):
            found.append(table)
    return found
