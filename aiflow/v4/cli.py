"""aiflow4: one command for every AI Flow v4 process.

  aiflow4 migrate                       apply registry migrations
  aiflow4 control-api [--port 8080]     control API + dashboards
  aiflow4 control-loops                 Jira sync, watchdog
  aiflow4 node join --join-token-file F [--control URL]
  aiflow4 node-agent                    heartbeat, metrics, worker supervision (root)
  aiflow4 worker --account ID           one AI account (root; AI runs as aiflow-worker)
  aiflow4 version
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import VERSION
from .core.settings import Settings


class RepoCache:
    """Control repo snapshot, reloaded at most every `ttl` seconds; `git pull` in the background."""

    def __init__(self, path: str, ttl: float = 30.0, pull_every: float = 60.0) -> None:
        from .core.controlrepo import ControlRepo
        self.path, self.ttl, self.pull_every = path, ttl, pull_every
        self._cls = ControlRepo
        self._snap = ControlRepo.load(path)
        self._at = time.time()
        self._lock = threading.Lock()
        if pull_every and (Path(path) / ".git").exists():
            threading.Thread(target=self._puller, daemon=True, name="control-repo-pull").start()

    def _puller(self) -> None:
        while True:
            time.sleep(self.pull_every)
            subprocess.run(["git", "-C", self.path, "pull", "-q", "--ff-only"], capture_output=True, timeout=60)

    def __call__(self):
        with self._lock:
            if time.time() - self._at > self.ttl:
                try:
                    self._snap = self._cls.load(self.path)
                except Exception as e:  # noqa: BLE001
                    logging.getLogger("aiflow4").warning("control repo reload failed: %s", e)
                self._at = time.time()
            return self._snap


def build_ctx(s: Settings):
    from .control.api import Ctx
    from .control.db import DB
    from .control.events import Events
    from .core.fencing import Leases
    db = DB(s.database_url)
    jira = github = None
    if s.jira_email and s.jira_token:
        from .control.jira import Jira
        jira = Jira(s.jira_site, s.jira_email, s.jira_token)
    if Path(s.github_app_dir, "app.pem").exists():
        from .control.github import GitHubApp
        github = GitHubApp(s.github_app_dir, s.github_org)
    return Ctx(settings=s, db=db, leases=Leases.from_url(s.redis_url, s.redis_prefix),
               events=Events(db, s.o2_url, s.o2_basic), repo=RepoCache(s.control_repo), jira=jira, github=github)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=os.environ.get("AIFLOW4_LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="aiflow4", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("version")
    sub.add_parser("migrate")
    p = sub.add_parser("control-api")
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--port", type=int, default=8080)
    sub.add_parser("control-loops")
    pn = sub.add_parser("node")
    pn.add_argument("action", choices=["join"])
    pn.add_argument("--join-token-file", required=True)
    pn.add_argument("--control", default="")
    pn.add_argument("--ip", default="")
    pa = sub.add_parser("node-agent")
    pa.add_argument("--slots", type=int, default=2)
    pa.add_argument("--no-workers", action="store_true")
    pw = sub.add_parser("worker")
    pw.add_argument("--account", required=True)
    a = ap.parse_args(argv)
    s = Settings.from_env()

    if a.cmd == "version":
        print(VERSION)
        return 0
    if a.cmd == "migrate":
        from .control.db import DB
        db = DB(s.database_url)
        print("applied:", db.migrate() or "nothing")
        return 0
    if a.cmd == "control-api":
        import uvicorn
        from .control.api import create_app
        from .control.ui import mount_ui
        ctx = build_ctx(s)
        ctx.db.migrate()
        ctx.events.start()
        app = create_app(ctx)
        mount_ui(app, ctx)
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning", proxy_headers=True)
        return 0
    if a.cmd == "control-loops":
        from .control.loops import run_loops
        ctx = build_ctx(s)
        ctx.db.migrate()
        run_loops(ctx)
        return 0
    if a.cmd == "node":
        import httpx
        control = a.control or s.control_url
        join_token = Path(a.join_token_file).read_text().strip()
        r = httpx.post(f"{control.rstrip('/')}/v1/nodes/join", json={"join_token": join_token, "ip": a.ip},
                       timeout=30)
        r.raise_for_status()
        tok = Path(s.node_token_file)
        tok.parent.mkdir(parents=True, exist_ok=True)
        old = os.umask(0o077)
        tok.write_text(r.json()["node_token"])
        os.umask(old)
        print(f"joined {r.json()['node']}; token in {tok}")
        return 0
    if a.cmd == "node-agent":
        from .node.agent import run_agent
        token = Path(s.node_token_file).read_text().strip()
        run_agent(s.control_url, s.node_name or os.uname().nodename, token, slots=a.slots, manage_workers=not a.no_workers)
        return 0
    if a.cmd == "worker":
        from .worker.loop import Worker
        token = Path(s.node_token_file).read_text().strip()
        Worker(a.account, s.control_url, token, s.node_name or os.uname().nodename, s.srv_root,
               heartbeat_s=s.heartbeat_s, poll_s=s.poll_s).run_forever()
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
