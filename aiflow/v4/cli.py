"""aiflow4: one command for every AI Flow v4 process.

  aiflow4 migrate                       apply registry migrations
  aiflow4 control-api [--port 8080]     control API + dashboards
  aiflow4 control-loops                 Jira sync, watchdog
  aiflow4 canary evaluate ...            evaluate the active auto-deploy canary
  aiflow4 canary record-rollback ...      record a canary rollback in the registry and Jira
  aiflow4 canary record-promotion ...     record an under-observed canary promotion
  aiflow4 canary retry-tickets           retry pending rollback flow-bug tickets
  aiflow4 upgrade-check --target vX.Y.Z  check migrations and version skew before a control upgrade (exit 9: blocked)
  aiflow4 node join [--name N] (--join-token-file F | --join-token-stdin) [--control URL]
  aiflow4 node-agent                    heartbeat, metrics, worker supervision (root)
  aiflow4 worker --account ID           one AI account (root; AI runs as aiflow-worker)
  aiflow4 install control --version vX.Y.Z --domain D [--admin-email E]   install, re-run or upgrade the control plane (root)
  aiflow4 console-user seed --email E --password-file F   create a Command Center user; the password goes to F (0600)
  aiflow4 install control --rollback    return the control plane to the previous release (root)
  aiflow4 secret set NAME [--stdin]     store an owner-supplied token with hidden input (root)
  aiflow4 version
"""
from __future__ import annotations

import argparse
import json
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
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001
                logging.getLogger("aiflow4").warning("control repo pull failed: %s", e)

    def refresh(self) -> None:
        """Fetch the latest committed control repo state and replace the in-memory snapshot."""
        if (Path(self.path) / ".git").exists():
            subprocess.run(["git", "-C", self.path, "pull", "-q", "--ff-only"], capture_output=True,
                           timeout=60, check=True)
        with self._lock:
            self._snap = self._cls.load(self.path)
            self._at = time.time()

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
    from .control.jira import Jira
    from .control.jira_connect import JiraAuth, SecretStore
    db = DB(s.database_url)
    github = None
    # Every Jira call asks this source: the credential connected on the Integrations screen first, then
    # JIRA_EMAIL / JIRA_API_TOKEN from owner.env (docs/v4/jira-connect.md).
    jira_auth = JiraAuth(s, SecretStore(s.jira_secret_dir))
    jira = Jira(s.jira_site, auth=jira_auth)
    if Path(s.github_app_dir, "app.pem").exists():
        from .control.github import GitHubApp
        github = GitHubApp(s.github_app_dir, s.github_org)
    repo_cache = RepoCache(s.control_repo)
    events = Events(db, s.o2_url, s.o2_basic, source=s.o2_source)
    ctx = Ctx(settings=s, db=db, leases=Leases.from_url(s.redis_url, s.redis_prefix),
              events=events, repo=repo_cache, jira=jira, github=github,
              refresh_repo=repo_cache.refresh, jira_auth=jira_auth)
    events.bind_incident_context(ctx)
    jira_auth.bind(ctx)
    return ctx


def _console_user(a, s) -> int:
    """`aiflow4 console-user seed|reset-password|list`: the generated password goes to a 0600 file, never to the output.

    The file is written before the new password hash is committed; when writing fails the change is rolled back."""
    from .control import login
    from .control.db import DB
    db = DB(s.database_url)
    try:
        db.migrate()
        if a.action == "list":
            for u in db.all("SELECT email, role, disabled, last_login_at FROM console_users ORDER BY email"):
                print(f"{u['email']}\t{u['role']}\t{'disabled' if u['disabled'] else 'active'}\t{u['last_login_at'] or 'never'}")
            return 0
        if not a.email or not a.password_file:
            print("--email and --password-file are required", file=sys.stderr)
            return 2

        def deliver(password: str) -> None:
            login.write_password_file(a.password_file, password)

        try:
            password = (login.seed_user(db, a.email, a.role, a.name, deliver=deliver,
                                        public_key=getattr(s, "license_public_key", "")) if a.action == "seed"
                        else login.reset_password(db, a.email, deliver=deliver,
                                                  public_key=getattr(s, "license_public_key", "")))
        except (ValueError, OSError) as exc:
            print(f"{exc}; nothing changed", file=sys.stderr)
            return 2
        if password is None:
            print(f"{login.normalize_email(a.email)} already exists; nothing changed")
            return 0
        print(f"{login.normalize_email(a.email)}: password written to {a.password_file} (mode 0600)")
        return 0
    finally:
        db.close()


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
    pn.add_argument("--name")
    join_token = pn.add_mutually_exclusive_group(required=True)
    join_token.add_argument("--join-token-file")
    join_token.add_argument("--join-token-stdin", action="store_true")
    pn.add_argument("--control", default="")
    pn.add_argument("--ip", default="")
    pa = sub.add_parser("node-agent")
    pa.add_argument("--slots", type=int, default=2)
    pa.add_argument("--no-workers", action="store_true")
    pc = sub.add_parser("canary")
    canary_sub = pc.add_subparsers(dest="canary_action", required=True)
    pe = canary_sub.add_parser("evaluate")
    pe.add_argument("--node", required=True)
    pe.add_argument("--target", required=True)
    pe.add_argument("--previous", required=True)
    pe.add_argument("--started-at", required=True)
    pe.add_argument("--minimum-age-s", type=int, default=1200)
    pe.add_argument("--minimum-runs", type=int, default=5)
    pe.add_argument("--maximum-age-s", type=int, default=3600)
    pe.add_argument("--failure-ratio", default="2")
    pe.add_argument("--minimum-infra-failures", type=int, default=3)
    pr = canary_sub.add_parser("record-rollback")
    pr.add_argument("--node", required=True)
    pr.add_argument("--target", required=True)
    pr.add_argument("--previous", required=True)
    pr.add_argument("--commit", required=True)
    pr.add_argument("--measurements", required=True)
    pp = canary_sub.add_parser("record-promotion")
    pp.add_argument("--node", required=True)
    pp.add_argument("--target", required=True)
    pp.add_argument("--measurements", required=True)
    pp.add_argument("--minimum-runs", type=int, default=5)
    canary_sub.add_parser("retry-tickets")
    pu = sub.add_parser("upgrade-check")
    pu.add_argument("--target", required=True)
    pu.add_argument("--current", default="")
    pu.add_argument("--migrations", default="")
    pu.add_argument("--baseline", default="")
    pw = sub.add_parser("worker")
    pw.add_argument("--account", required=True)
    pi = sub.add_parser("install")
    pi.add_argument("target", choices=["control"])
    pi.add_argument("--version", dest="release", default="", metavar="vX.Y.Z")
    pi.add_argument("--domain", default="")
    pi.add_argument("--rollback", action="store_true")
    pi.add_argument("--stack", default="", help="project stack whose OS toolchain server-installer adds, e.g. php-8.3")
    pi.add_argument("--si", action="append", default=[], metavar="SI_NAME=VALUE")
    pi.add_argument("--vpn", choices=["tailscale", "wireguard"], default="")
    pi.add_argument("--otel-collector", action="store_true")
    pi.add_argument("--health-timeout", type=int, default=300)
    pi.add_argument("--admin-email", default="", help="the first Super Administrator of Command Center (first install)")
    ps = sub.add_parser("secret")
    ps.add_argument("action", choices=["set", "list"])
    ps.add_argument("name", nargs="?")
    ps.add_argument("--stdin", action="store_true", help="read the value from a pipe instead of a hidden prompt")
    pu2 = sub.add_parser("console-user", help="create or reset a Command Center user (docs/v4/console-login.md)")
    pu2.add_argument("action", choices=["seed", "reset-password", "list"])
    pu2.add_argument("--email")
    pu2.add_argument("--role", default="super_admin")
    pu2.add_argument("--name", default="")
    pu2.add_argument("--password-file", help="where the generated password is written (mode 0600); it is never printed")
    a = ap.parse_args(argv)

    if a.cmd == "install":
        from .install.control import ControlInstaller, Options
        return ControlInstaller(Options(version=a.release, domain=a.domain, rollback=a.rollback, stack=a.stack,
                                        si=a.si, vpn=a.vpn, otel_collector=a.otel_collector,
                                        health_timeout=a.health_timeout, admin_email=a.admin_email)).run()
    if a.cmd == "secret":
        from .install.control import secret_command
        return secret_command(a.action, a.name, a.stdin)
    s = Settings.from_env()

    if a.cmd == "version":
        print(VERSION)
        return 0
    if a.cmd == "upgrade-check":
        # Run from the new release before the control plane switches to it; exit 9 blocks the upgrade.
        from types import SimpleNamespace
        from .control.db import DB, MIGRATIONS
        from .control.events import Events
        from .control.rollout import check_upgrade, record_check
        db = DB(s.database_url, max_size=2)
        try:
            result = check_upgrade(db, a.target, Path(a.migrations) if a.migrations else MIGRATIONS,
                                   Path(a.baseline) if a.baseline else None, a.current, s.version_max_minor_lag)
            record_check(SimpleNamespace(db=db, settings=s, events=Events(db, s.o2_url, s.o2_basic)), result)
            print(json.dumps(result, separators=(",", ":")))
        finally:
            db.close()
        return 0 if result["ok"] else 9
    if a.cmd == "console-user":
        return _console_user(a, s)
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
    if a.cmd == "canary":
        from datetime import datetime, timezone
        from .control.canary import (Thresholds, decide, measure_registry, record_promotion,
                                     record_rollback, retry_pending_flow_bugs)
        if a.canary_action == "evaluate":
            from .control.db import DB
            db = DB(s.database_url)
            try:
                installed = db.one("SELECT release_version FROM nodes WHERE name=%s", (a.node,))
                measurements = measure_registry(db, a.node, datetime.fromisoformat(a.started_at), a.previous)
                thresholds = Thresholds.from_values(a.minimum_age_s, a.minimum_runs, a.maximum_age_s,
                                                    a.failure_ratio, a.minimum_infra_failures)
                elapsed = max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(a.started_at)).total_seconds()))
                result = decide(measurements, elapsed, thresholds,
                                installed=bool(installed and installed["release_version"] == a.target))
                print(json.dumps(result, separators=(",", ":")))
            finally:
                db.close()
            return 0
        ctx = build_ctx(s)
        try:
            if a.canary_action == "retry-tickets":
                print(json.dumps(retry_pending_flow_bugs(ctx), separators=(",", ":")))
                return 0
            measurements = json.loads(a.measurements)
            if a.canary_action == "record-rollback":
                result = record_rollback(ctx, node=a.node, target=a.target, previous=a.previous,
                                         commit=a.commit, measurements=measurements)
            else:
                result = {"incident_opened": record_promotion(
                    ctx, target=a.target, node=a.node, measurements=measurements, under_observed=True,
                    required_runs=a.minimum_runs)}
            print(json.dumps(result, separators=(",", ":")))
        finally:
            ctx.db.close()
            ctx.leases.r.close()
        return 0
    if a.cmd == "node":
        import httpx
        control = a.control or s.control_url
        join_secret = sys.stdin.read().strip() if a.join_token_stdin else Path(a.join_token_file).read_text().strip()
        payload = {"join_token": join_secret, "ip": a.ip}
        if a.name:
            payload["name"] = a.name
        r = httpx.post(f"{control.rstrip('/')}/v1/nodes/join", json=payload,
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
