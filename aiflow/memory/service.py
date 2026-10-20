"""The memory service: the only process that holds the PostgreSQL credential.

    POST /v1/search             client   {query, namespace, project, repo, kinds, limit, touch}
    GET  /v1/entries/<id>       client
    POST /v1/entries            client   propose an entry (stored `fresh`); admin may set `status`
    POST /v1/entries/<id>/feedback   client   {jira_key, helpful}
    POST /v1/entries/<id>/verify | promote | decay    admin
    POST /v1/curate             admin
    POST /v1/scoped/<operation> admin   the Workspaces notes (add, get, search, list, update, status, stale, consolidate, stats, projects, project_update,
                                        device_add, device_list, device_update, import_*): JSON body = the arguments of the operation; answer {"result": ...}
    GET  /v1/stats              client
    GET  /health                nobody (no data)

Tokens live in a git-ignored JSON file: [{"name": "supervisor", "token": "...", "role": "client" | "admin", "namespaces": ["JOOservices"] | ["*"]}].
A client can only read and write inside its namespaces (plus the global scope); only an admin changes the life of an entry."""
from __future__ import annotations

import hmac
import json
import os
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

from . import guard
from .scoped import OPERATIONS, Scoped
from .store import NotFound, Store

ID = r"[0-9a-fA-F-]{36}"
MAX_BODY = 64 * 1024


class Tokens:
    def __init__(self, entries: list[dict[str, Any]]) -> None:
        self.entries = [e for e in entries if e.get("token") and e.get("role") in ("client", "admin")]

    @classmethod
    def load(cls, path: Path) -> "Tokens":
        return cls(json.loads(path.read_text(encoding="utf-8")) if path.is_file() else [])

    def find(self, header: str) -> dict[str, Any] | None:
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        found = None
        for entry in self.entries:  # compare against all, constant time, so a wrong token leaks nothing by timing
            if hmac.compare_digest(str(entry["token"]), token):
                found = entry
        return found


def allowed(who: dict[str, Any], namespace: str) -> bool:
    spaces = who.get("namespaces", [])
    return namespace == "" or "*" in spaces or namespace in spaces


class Handler(BaseHTTPRequestHandler):
    server_version = "aiflow-memory"
    store: Store
    scoped: Scoped
    tokens: Tokens
    log: Callable[[str], None] = staticmethod(print)

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - the base class signature
        pass

    def reply(self, status: int, body: Any) -> None:
        raw = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def payload(self) -> dict[str, Any]:
        size = int(self.headers.get("Content-Length") or 0)
        if size > MAX_BODY:
            raise ValueError("request too large")
        data = json.loads(self.rfile.read(size) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("the request body must be a JSON object")
        return data

    def handle_request(self, method: str) -> None:
        path = self.path.split("?")[0].rstrip("/")
        if method == "GET" and path == "/health":
            try:
                return self.reply(200, {"ok": True, "db": self.store.ping()})
            except Exception:
                return self.reply(200, {"ok": True, "db": False})
        who = self.tokens.find(self.headers.get("Authorization", ""))
        if who is None:
            return self.reply(401, {"error": "unknown token"})
        admin = who["role"] == "admin"
        actor = who.get("name", "?")
        try:
            if method == "POST" and path == "/v1/search":
                data = self.payload()
                namespace = str(data.get("namespace", ""))
                if not allowed(who, namespace):
                    return self.reply(403, {"error": "namespace not allowed for this token"})
                items = self.store.search(str(data.get("query", "")), namespace, str(data.get("project", "")), str(data.get("repo", "")),
                                          data.get("kinds"), data.get("statuses") if admin else None, int(data.get("limit", 8)), bool(data.get("touch")),
                                          int(data.get("body_chars", 600)))
                return self.reply(200, {"items": items})
            if method == "POST" and path == "/v1/entries":
                data = self.payload()
                if not allowed(who, str(data.get("namespace", ""))) or (data.get("scope") == "global" and not admin and "*" not in who.get("namespaces", [])):
                    return self.reply(403, {"error": "namespace not allowed for this token"})
                status = str(data.get("status", "fresh")) if admin else "fresh"
                entry_id, created = self.store.put(data, actor, status)
                return self.reply(201 if created else 200, {"id": entry_id, "created": created})
            if method == "GET" and path == "/v1/stats":
                return self.reply(200, self.store.stats())
            match = re.fullmatch(rf"/v1/entries/({ID})", path)
            if method == "GET" and match:
                return self.reply(200, self.store.get(match.group(1), who.get("namespaces")))
            match = re.fullmatch(rf"/v1/entries/({ID})/(feedback|verify|promote|decay)", path)
            if method == "POST" and match:
                entry_id, action = match.groups()
                item = self.store.get(entry_id, who.get("namespaces"))
                if action == "feedback":
                    data = self.payload()
                    self.store.feedback(entry_id, actor, str(data.get("jira_key", "")), bool(data.get("helpful")))
                elif not admin:
                    return self.reply(403, {"error": f"{action} needs an admin token"})
                elif action == "decay":
                    self.store.decay(entry_id, actor, str(self.payload().get("reason", "decayed by an admin")))
                else:
                    getattr(self.store, action)(entry_id, actor)
                return self.reply(200, {"id": item["id"], "ok": True})
            match = re.fullmatch(r"/v1/scoped/(\w+)", path)
            if method == "POST" and match:
                operation = match.group(1)
                if not admin:
                    return self.reply(403, {"error": "the scoped notes need an admin token"})
                if operation not in OPERATIONS:
                    return self.reply(404, {"error": "no such operation"})
                data = self.payload()
                if not allowed(who, str(data.get("namespace") or "")):
                    return self.reply(403, {"error": "namespace not allowed for this token"})
                try:
                    return self.reply(200, {"result": getattr(self.scoped, operation)(**data)})
                except TypeError as error:
                    return self.reply(400, {"error": f"bad arguments: {str(error)[:200]}"})
            if method == "POST" and path == "/v1/curate":
                return self.reply(200, self.store.curate(actor)) if admin else self.reply(403, {"error": "curate needs an admin token"})
            return self.reply(404, {"error": "no such route"})
        except guard.Rejected as error:
            return self.reply(422, {"error": str(error)})
        except NotFound as error:
            return self.reply(404, {"error": str(error)})
        except ValueError as error:
            return self.reply(400, {"error": str(error)[:300] or "bad request"})
        except TypeError as error:
            return self.reply(400, {"error": f"bad request: {type(error).__name__}"})
        except Exception as error:
            self.log(f"memory service: {type(error).__name__}: {str(error)[:200]}")
            return self.reply(503, {"error": "the memory database is not available"})

    def do_GET(self) -> None:  # noqa: N802
        self.handle_request("GET")

    def do_POST(self) -> None:  # noqa: N802
        self.handle_request("POST")


def make_server(store: Store, tokens: Tokens, host: str = "127.0.0.1", port: int = 0, log: Callable[[str], None] = print) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"store": store, "scoped": Scoped(store), "tokens": tokens, "log": staticmethod(log)})
    return ThreadingHTTPServer((host, port), handler)


def connector(env: dict[str, str]) -> Callable[[], Any]:
    import pg8000.dbapi  # only the service needs the driver (uv run --with pg8000)
    settings = dict(host=env.get("AIFLOW_MEMORY_PG_HOST", "127.0.0.1"), port=int(env.get("AIFLOW_MEMORY_PG_PORT", "5432")),
                    database=env.get("AIFLOW_MEMORY_PG_DB", "ai_memory"), user=env.get("AIFLOW_MEMORY_PG_USER", ""), password=env.get("AIFLOW_MEMORY_PG_PASSWORD", ""))
    if not settings["user"] or not settings["password"]:
        raise SystemExit("AIFLOW_MEMORY_PG_USER and AIFLOW_MEMORY_PG_PASSWORD are not set (Workspaces/.env)")
    return lambda: pg8000.dbapi.connect(**settings)


def main() -> int:
    from ..config import WORKSPACES_ROOT, CONFIG_DIR, read_env_file
    env = {**read_env_file(WORKSPACES_ROOT / ".env"), **os.environ}
    tokens = Tokens.load(Path(env.get("AIFLOW_MEMORY_TOKENS_FILE", str(CONFIG_DIR / "memory-tokens.json"))))
    if not tokens.entries:
        print("no token configured: create JOOservices/config/memory-tokens.json (see docs/MEMORY.md)", file=sys.stderr)
        return 2
    store = Store(connector(env))
    store.migrate()
    server = make_server(store, tokens, env.get("AIFLOW_MEMORY_HOST", "127.0.0.1"), int(env.get("AIFLOW_MEMORY_PORT", "8750")))
    print(f"memory service on {server.server_address[0]}:{server.server_address[1]} with {len(tokens.entries)} token(s)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
