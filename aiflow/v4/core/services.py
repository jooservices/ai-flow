"""Test services a project declares (`services: [postgres, redis]` in its project settings).

The node agent prepares them as Docker containers on the host at the test defaults the v4 tests expect
(tests/v4/conftest.py): PostgreSQL with pgvector on 127.0.0.1:15499 (user `aiflow`, password `test`) and Redis on
127.0.0.1:16399. Each pytest session still creates its own database on that server. The agent reports their state
in every heartbeat; a project whose declared service is not healthy on a node gets no work there.
"""
from __future__ import annotations

from typing import Any

HOST = "127.0.0.1"
PG_IMAGE = "pgvector/pgvector:0.8.7-pg16"
REDIS_IMAGE = "redis:7-alpine"

# Small footprint: capped memory and no durability. `command` is what follows the image in `docker run`.
SERVICES: dict[str, dict[str, Any]] = {
    "postgres": {
        "container": "aiflow4-test-pg", "image": PG_IMAGE, "port": 15499, "internal_port": 5432,
        "env": {"POSTGRES_USER": "aiflow", "POSTGRES_PASSWORD": "test", "POSTGRES_DB": "aiflow4"},
        "memory": "512m",
        "command": ["-c", "shared_buffers=64MB", "-c", "max_connections=100", "-c", "fsync=off",
                    "-c", "synchronous_commit=off", "-c", "full_page_writes=off"],
    },
    "redis": {
        "container": "aiflow4-test-redis", "image": REDIS_IMAGE, "port": 16399, "internal_port": 6379,
        "env": {}, "memory": "128m",
        "command": ["redis-server", "--save", "", "--appendonly", "no", "--maxmemory", "96mb"],
    },
}
ALIASES = {"postgresql": "postgres", "pg": "postgres", "pgvector": "postgres"}

# These environments occur in escalation text, but nodes have no probes for them yet. They are deliberately not
# accepted as parked requirements until the heartbeat can report their availability.
EXTERNAL_REQUIREMENTS = ("dev-vm", "sonarqube", "devices")


def normalize_services(value: Any) -> list[str]:
    """Canonical names of the declared services (a string or a list), without duplicates or unknown names."""
    items = [value] if isinstance(value, str) else list(value or [])
    names = [ALIASES.get(str(i).strip().lower(), str(i).strip().lower()) for i in items]
    return [n for n in dict.fromkeys(names) if n in SERVICES]


def endpoint(name: str) -> str:
    return f"{HOST}:{SERVICES[name]['port']}"


def service_gaps(reported: Any, needed: list[str]) -> dict[str, str]:
    """Declared services that are not reported healthy, each with the reason shown in Command Center."""
    reported = reported if isinstance(reported, dict) else {}
    gaps: dict[str, str] = {}
    for name in needed:
        state = reported.get(name)
        if not isinstance(state, dict):
            gaps[name] = "not reported by the node agent yet"
        elif not state.get("healthy"):
            gaps[name] = str(state.get("detail") or state.get("state") or "unhealthy")
    return gaps
