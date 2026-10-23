#!/usr/bin/env python3
"""Private Jira and GitHub fakes for the signed clean-install acceptance containers."""
from __future__ import annotations

import argparse
import base64
import json
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


STATUSES = ["Backlog", "Intake", "To Do", "In Progress", "In Verify", "Needs owner", "Done"]
REPOSITORIES = [
    {"full_name": "acceptance/clean-install", "name": "clean-install", "default_branch": "main"},
    {"full_name": "acceptance/aiflow-control", "name": "aiflow-control", "default_branch": "main"},
]


class Handler(BaseHTTPRequestHandler):
    server_version = "AIFlowAcceptanceProviders/1"

    def log_message(self, fmt: str, *args: object) -> None:
        # Request headers and bodies can contain test credentials or installation tokens.
        return

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        size = int(self.headers.get("Content-Length", "0"))
        value = json.loads(self.rfile.read(size) or b"{}")
        return value if isinstance(value, dict) else {}

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/rest/api/3/project/search":
            self._json(200, {"values": [{"key": "CLEAN", "name": "Clean Install Acceptance"}], "isLast": True})
        elif path == "/rest/api/3/project/CLEAN/statuses":
            self._json(200, [{"name": "Story", "statuses": [{"name": name} for name in STATUSES]}])
        elif path == "/installation/repositories":
            self._json(200, {"total_count": len(REPOSITORIES), "repositories": REPOSITORIES})
        elif path == "/repos/acceptance/aiflow-control":
            self._json(200, {"name": "aiflow-control", "default_branch": "main"})
        elif path == "/repos/acceptance/clean-install/branches/main":
            self._json(200, {"name": "main", "commit": {"sha": "acceptance-main"}})
        elif path == "/repos/acceptance/aiflow-control/contents/projects/CLEAN.yaml":
            self._json(404, {"message": "Not Found"})
        else:
            self._json(404, {"message": "Not Found"})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/app/installations/1/access_tokens":
            self._body()
            self._json(201, {"token": "acceptance-installation-token", "expires_at": "2099-01-01T00:00:00Z"})
        else:
            self._json(404, {"message": "Not Found"})

    def do_PUT(self) -> None:
        path = urlsplit(self.path).path
        if path != "/repos/acceptance/aiflow-control/contents/projects/CLEAN.yaml":
            self._json(404, {"message": "Not Found"})
            return
        body = self._body()
        try:
            content = base64.b64decode(body["content"], validate=True).decode("utf-8")
        except (KeyError, ValueError, UnicodeDecodeError):
            self._json(422, {"message": "Invalid content"})
            return
        project = self.server.project_repo / "projects" / "CLEAN.yaml"
        project.parent.mkdir(parents=True, exist_ok=True)
        project.write_text(content, encoding="utf-8")
        self._json(200, {"content": {"path": "projects/CLEAN.yaml"}, "commit": {"sha": "acceptance-commit"}})


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cert", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args()

    server = ThreadingHTTPServer(("0.0.0.0", 443), Handler)
    server.project_repo = Path(args.repo)  # type: ignore[attr-defined]
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(args.cert, args.key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
