"""aiflow4: one command for every AI Flow v4 process.

  aiflow4 migrate                       apply registry migrations
  aiflow4 control-api [--port 8080]     control API + dashboards
  aiflow4 control-loops                 Jira sync, watchdog
  aiflow4 canary evaluate ...            evaluate the active auto-deploy canary
  aiflow4 canary record-rollback ...      record a canary rollback in the registry and Jira
  aiflow4 canary record-promotion ...     record an under-observed canary promotion
  aiflow4 canary retry-tickets           retry pending rollback flow-bug tickets
  aiflow4 notify --severity S --title T --detail D   deliver through saved notification channels
  aiflow4 ba-chat setup --name N --chat-id ID --project KEY  configure Telegram BA chat (token read privately)
  aiflow4 upgrade-check --target vX.Y.Z  check migrations and version skew before a control upgrade (exit 9: blocked)
  aiflow4 node join [--name N] (--join-token-file F | --join-token-stdin) [--control URL]
  aiflow4 node-agent                    heartbeat, metrics, worker supervision (root)
  aiflow4 worker --account ID           one AI account (root; AI runs as aiflow-worker)
  aiflow4 install control --version vX.Y.Z --domain D [--admin-email E]   install, re-run or upgrade the control plane (root)
  aiflow4 console-user seed --email E --password-file F   create a Command Center user; the password goes to F (0600)
  aiflow4 install control --rollback    return the control plane to the previous release (root)
  aiflow4 scale-proof --release vX.Y.Z [--out F] [--submit URL]   run the 100/50/200 scale proof (CI); exit 1: a budget is exceeded
  aiflow4 secret set NAME [--stdin]     store an owner-supplied token with hidden input (root)
  aiflow4 backup restore --from FILE --key-file FILE [--s3-endpoint URL]
                                        rebuild a new control plane from an encrypted backup
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
    from .control import shared_accounts
    ctx.repo = lambda: shared_accounts.overlay(repo_cache(), db)  # accounts managed in Command Center
    ctx._shared_accounts = True
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


def _scale_proof(a: argparse.Namespace) -> int:
    """Run the scenario in a scratch database, print every budget, write and submit the report.

    Exit 0: within every budget. 1: a budget is exceeded or behaviour was incorrect (the release must not ship).
    2: the scenario could not run. 3: the result could not be attached on the control plane."""
    import tempfile
    from .control import scale_harness
    if not a.database_url or not a.redis_url:
        print("scale-proof needs --database-url and --redis-url (or AIFLOW4_TEST_DATABASE_URL / AIFLOW4_TEST_REDIS_URL): "
              "it creates and drops a database of its own", file=sys.stderr)
        return 2
    try:
        with scale_harness.scratch(a.database_url, a.redis_url) as (db, leases), \
                tempfile.TemporaryDirectory(prefix="aiflow4-scale-") as workdir:
            report, _ctx = scale_harness.run(db, leases, Path(workdir), release=a.release,
                                             log=lambda m: print(m, flush=True))
    except Exception as exc:  # noqa: BLE001
        print(f"scale proof could not run: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    for b in report["budgets"]:
        print(("ok      " if b["passed"] else "EXCEEDED") + " " + b["message"])
    for failure in report["failures"]:
        print("FAILED  " + failure)
    if a.out:
        Path(a.out).write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    code = 0 if report["passed"] else 1
    if a.submit:
        import httpx
        try:
            r = httpx.post(a.submit.rstrip("/") + "/v1/scale-proof/results", timeout=60,
                           headers={"Authorization": f"Bearer {os.environ.get('AIFLOW4_OWNER_TOKEN', '')}"},
                           json={"release": a.release, "source": "ci", "report": report})
            r.raise_for_status()
            print(f"attached to release {a.release} as run #{r.json().get('run_id')}; tickets: "
                  + (", ".join(t["ticket"] for t in r.json().get("tickets", []) if t.get("ticket")) or "none"))
        except Exception as exc:  # noqa: BLE001 - never print the request: it carries the token
            print(f"could not attach the result to {a.submit}: {type(exc).__name__}", file=sys.stderr)
            code = code or 3
    return code


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
    pnotify = sub.add_parser("notify", help="send a system alert using saved notification channels")
    pnotify.add_argument("--severity", choices=("info", "warning", "critical"), required=True)
    pnotify.add_argument("--title", required=True)
    pnotify.add_argument("--detail", default="")
    pnotify.add_argument("--link", default="")
    pba = sub.add_parser("ba-chat", help="configure the developer Telegram BA chat channel")
    ba_sub = pba.add_subparsers(dest="ba_action", required=True)
    pba_setup = ba_sub.add_parser("setup", help="add or update a Telegram BA chat channel")
    pba_setup.add_argument("--name", required=True)
    pba_setup.add_argument("--chat-id", required=True)
    pba_setup.add_argument("--project", required=True)
    pba_setup.add_argument("--token-stdin", action="store_true", help="read the bot token from stdin")
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
    psp = sub.add_parser("scale-proof", help="run the scale proof scenario against the budgets (docs/v4/scale-proof.md)")
    psp.add_argument("--release", required=True, help="the release the result is attached to, e.g. v4.3.0")
    psp.add_argument("--out", default="", help="write the JSON report here")
    psp.add_argument("--submit", default="", metavar="CONTROL_URL",
                     help="attach the result to the release on this control plane (token: AIFLOW4_OWNER_TOKEN)")
    psp.add_argument("--database-url", default=os.environ.get("AIFLOW4_TEST_DATABASE_URL", ""))
    psp.add_argument("--redis-url", default=os.environ.get("AIFLOW4_TEST_REDIS_URL", ""))
    ps = sub.add_parser("secret")
    ps.add_argument("action", choices=["set", "list"])
    ps.add_argument("name", nargs="?")
    ps.add_argument("--stdin", action="store_true", help="read the value from a pipe instead of a hidden prompt")
    pb = sub.add_parser("backup", help="encrypted Command Center backup operations")
    backup_sub = pb.add_subparsers(dest="backup_action", required=True)
    pbr = backup_sub.add_parser("restore", help="restore a backup onto a new control machine")
    pbr.add_argument("--from", dest="source", required=True, help="local encrypted backup or s3://bucket/key")
    pbr.add_argument("--key-file", required=True, help="the one-time backup key downloaded from Command Center")
    pbr.add_argument("--s3-endpoint", default="", help="S3-compatible endpoint for an s3:// source")
    pbr.add_argument("--s3-region", default="us-east-1")
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
    if a.cmd == "scale-proof":
        return _scale_proof(a)
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
    if a.cmd == "backup" and a.backup_action == "restore":
        from .core import backups
        try:
            key = backups.read_key(a.key_file)
            result = backups.restore_archive(a.source, key, s.database_url, s,
                                             s3_endpoint=a.s3_endpoint, s3_region=a.s3_region)
        except backups.BackupError as exc:
            print(f"restore failed: {exc}", file=sys.stderr)
            return 1
        print(f"Backup: {result.get('backup', a.source)}")
        for name in ("database", "stored_secrets", "control_repository", "knowledge"):
            component = result["components"].get(name, {"status": "failed", "detail": "no outcome was recorded"})
            label = {"database": "Database", "stored_secrets": "Stored secrets",
                     "control_repository": "Control repository settings", "knowledge": "Knowledge"}[name]
            print(f"{label}: {component['status']} — {component['detail']}")
        return 0 if result.get("ok") else 1
    if a.cmd == "migrate":
        from .control.db import DB
        db = DB(s.database_url)
        print("applied:", db.migrate() or "nothing")
        return 0
    if a.cmd == "control-api":
        import uvicorn
        from .control.api import create_app
        from .control.ui import mount_ui
        wait_for_control_stores(s)
        ctx = build_ctx(s)
        ctx.db.migrate()
        ctx.events.start()
        app = create_app(ctx)
        mount_ui(app, ctx)
        uvicorn.run(app, host=a.host, port=a.port, log_level="warning", proxy_headers=True)
        return 0
    if a.cmd == "control-loops":
        from .control.loops import run_loops
        wait_for_control_stores(s)
        ctx = build_ctx(s)
        ctx.db.migrate()
        run_loops(ctx)
        return 0
    if a.cmd == "notify":
        from types import SimpleNamespace
        from .control.db import DB
        from .control import notify
        db = DB(s.database_url, max_size=2)
        try:
            notify.send(a.severity, a.title, a.detail, a.link, ctx=SimpleNamespace(db=db, settings=s))
        finally:
            db.close()
        return 0
    if a.cmd == "ba-chat" and a.ba_action == "setup":
        import getpass
        from .control.db import DB
        from .control import ba_chat, notify
        from .core.controlrepo import ControlRepo
        project_key = a.project.strip().upper()
        if not ba_chat.PROJECT_KEY.fullmatch(project_key) or project_key not in ControlRepo.load(s.control_repo).projects:
            print("choose a configured Jira project", file=sys.stderr)
            return 2
        if not a.token_stdin and not sys.stdin.isatty():
            print("use --token-stdin for non-interactive setup", file=sys.stderr)
            return 2
        bot_token = (sys.stdin.read() if a.token_stdin else getpass.getpass("Telegram bot token: ")).strip()
        if not bot_token:
            print("Telegram bot token is required", file=sys.stderr)
            return 2
        db = DB(s.database_url, max_size=2)
        try:
            db.migrate()
            existing = db.one("SELECT id FROM notification_channels WHERE kind='telegram' AND name=%s", (a.name,))
            notify.save_channel(db, {"id": existing["id"] if existing else None, "kind": "telegram",
                "name": a.name, "enabled": True,
                "config": {"bot_token": bot_token, "chat_id": a.chat_id, "ba_chat_enabled": True,
                           "project_key": project_key}})
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        finally:
            db.close()
        print("Telegram BA chat channel configured; credentials are hidden.")
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


def wait_for_control_stores(settings) -> None:
    """Wait for PostgreSQL and Redis on boot so the control plane starts after its durable stores."""
    import time

    import psycopg
    import redis

    while True:
        try:
            with psycopg.connect(settings.database_url, connect_timeout=5) as connection:
                connection.execute("SELECT 1")
            redis.Redis.from_url(settings.redis_url, socket_connect_timeout=5, socket_timeout=5).ping()
            return
        except (psycopg.Error, redis.exceptions.RedisError, OSError) as exc:
            logging.getLogger("aiflow4.startup").warning(
                "control stores are not ready (%s); retrying automatically", type(exc).__name__)
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
