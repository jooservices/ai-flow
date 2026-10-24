"""HTTP client of the control API, used by node-agent and workers (node token, retries on network errors)."""
from __future__ import annotations

import time
from typing import Any

import httpx


class Conflict(Exception):
    """409 from the control API: lease lost or fencing token stale."""


class ControlClient:
    def __init__(self, base_url: str, node_token: str, timeout: float = 30.0) -> None:
        self.c = httpx.Client(base_url=base_url.rstrip("/"), timeout=timeout,
                              headers={"Authorization": f"Bearer {node_token}"})

    def call(self, method: str, path: str, json: Any = None, params: dict | None = None, retries: int = 4,
             allow_not_found: bool = False) -> Any:
        last: Exception | None = None
        for attempt in range(retries):
            try:
                r = self.c.request(method, path, json=json, params=params)
            except httpx.HTTPError as e:
                last = e
                time.sleep(min(20, 2 ** attempt))
                continue
            if r.status_code == 404 and allow_not_found:
                return None
            if r.status_code == 409:
                raise Conflict(r.text[:300])
            if r.status_code >= 500 or (r.status_code == 404 and r.text.strip() == "404 page not found"):
                last = RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:200]}")
                time.sleep(min(20, 2 ** attempt))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
            return r.json() if r.content else None
        raise RuntimeError(f"control API unreachable: {last}")
