"""Create or update the checked-in AI Flow dashboards in OpenObserve."""
from __future__ import annotations

import json
import os
from copy import deepcopy
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

DASHBOARD_DIR = Path(__file__).with_name("dashboards")
ORG = "default"
FOLDER = "default"


def load_dashboards() -> list[dict[str, Any]]:
    """Load all dashboard definitions in stable filename order."""
    files = sorted(DASHBOARD_DIR.glob("*.json"))
    if not files:
        raise RuntimeError(f"no dashboard definitions found in {DASHBOARD_DIR}")
    return [json.loads(path.read_text(encoding="utf-8")) for path in files]


def provision(base_url: str, basic: str, client: Any | None = None) -> list[str]:
    """Upsert dashboards by title, using OpenObserve's dashboard id and concurrency hash."""
    if not base_url.strip():
        raise ValueError("AIFLOW4_O2_URL is required")
    if not basic.strip():
        raise ValueError("O2_BASIC is required")

    own_client = client is None
    if client is None:
        client = httpx.Client(timeout=20, headers={"Authorization": f"Basic {basic}"})
    api_url = f"{base_url.rstrip('/')}/api/{ORG}/dashboards"
    actions: list[str] = []
    try:
        response = client.get(api_url, params={"folder": FOLDER})
        response.raise_for_status()
        dashboards = response.json().get("dashboards", [])

        for definition in load_dashboards():
            found = next((item for item in dashboards if item.get("title") == definition["title"]), None)
            if found is None:
                response = client.post(api_url, params={"folder": FOLDER}, json=definition)
                response.raise_for_status()
                actions.append(f"Created: {definition['title']}")
                continue

            dashboard_id = found.get("dashboard_id") or found.get("dashboardId")
            if not dashboard_id:
                raise RuntimeError(f"OpenObserve did not return an id for {definition['title']}")
            dashboard_hash = found.get("hash")
            if not dashboard_hash:
                detail = client.get(f"{api_url}/{quote(str(dashboard_id), safe='')}", params={"folder": FOLDER})
                detail.raise_for_status()
                dashboard_hash = detail.json().get("hash")
            if not dashboard_hash:
                raise RuntimeError(f"OpenObserve did not return a concurrency hash for {definition['title']}")

            payload = deepcopy(definition)
            payload["dashboardId"] = dashboard_id
            response = client.put(f"{api_url}/{quote(str(dashboard_id), safe='')}",
                                  params={"folder": FOLDER, "hash": dashboard_hash}, json=payload)
            response.raise_for_status()
            actions.append(f"Updated: {definition['title']}")
    finally:
        if own_client:
            client.close()
    return actions


def main() -> None:
    """Provision using the same OpenObserve environment variables as event shipping."""
    actions = provision(os.environ.get("AIFLOW4_O2_URL", ""), os.environ.get("O2_BASIC", ""))
    print("\n".join(actions))


if __name__ == "__main__":
    main()
