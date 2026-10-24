"""Worker for one AI account (systemd unit aiflow4-worker@<account>).

The worker process runs as root so it can read the node token; the AI CLI and git run as the unprivileged AI
user (setpriv), so nothing the AI does can reach the token or the control plane directly. It publishes its
state in /run/aiflow4/workers/<account>.json for the node agent's heartbeat.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ..control.support_bundle import MAX_LOG_BYTES, cap_log_bytes

from ..adapters import RunSpec, get_adapter
from .client import Conflict, ControlClient
from .dev import Claimed, run_dev
from .triage import run_triage
from .impact import run_impact
from .intake import run_intake
from .verify import run_verify
from .master import run_master
from .version_release import run_version_release
from .ba_chat import run_turn as run_ba_chat_turn

log = logging.getLogger("aiflow4.worker")
STATE_DIR = Path("/run/aiflow4/workers")
RUN_AS = ["setpriv", "--reuid=aiflow-worker", "--regid=aiflow-worker", "--init-groups", "--"]
PROBE_PROMPT = "This is an availability check. Do not use any tools. Reply with the single word OK."


def attach_support_log(result: dict, srv_root: str) -> None:
    """Attach a bounded copy of this run's log so the control plane can build ticket bundles across nodes."""
    details = result.get("details")
    log_path = details.get("log") if isinstance(details, dict) else None
    if not isinstance(log_path, str) or not log_path:
        return
    root = (Path(srv_root) / "logs" / "runs").resolve()
    try:
        path = Path(log_path).resolve(strict=True)
        if not path.is_relative_to(root) or not path.is_file():
            return
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > MAX_LOG_BYTES:
                stream.seek(-MAX_LOG_BYTES, 2)
            raw = stream.read(MAX_LOG_BYTES + 1)
        truncated = size > MAX_LOG_BYTES or len(raw) > MAX_LOG_BYTES
        log_text, was_capped = cap_log_bytes(raw.decode("utf-8", errors="replace"))
        details["support_log_text"] = log_text
        details["support_log_truncated"] = truncated or was_capped
    except OSError as exc:
        log.debug("run log unavailable for support collection: %s", type(exc).__name__)


class Worker:
    def __init__(self, account: str, control_url: str, node_token: str, node_name: str, srv_root: str,
                 heartbeat_s: int = 30, poll_s: int = 20, run_as: list[str] | None = None) -> None:
        self.instance = account                       # "codex-01" or "codex-01.2" (second session of the account)
        base, _, n = account.rpartition(".")
        self.account = base if base and n.isdigit() else account
        self.api = ControlClient(control_url, node_token)
        self.api.node_name = node_name  # type: ignore[attr-defined]
        self.control_url = control_url
        self.node = node_name
        self.srv = srv_root
        self.hb_s = heartbeat_s
        self.poll_s = poll_s
        self.run_as = RUN_AS if run_as is None else run_as
        self.stop = threading.Event()
        self.state: dict = {"account": self.account, "state": "idle", "since": time.time()}
        self.probe_every_s = int(os.environ.get("AIFLOW4_PROBE_EVERY_S", "1800"))
        self.next_probe = 0.0
        self.probe_ok = False
        if self.instance != self.account:  # extra sessions rely on the first one's checks and on cooldowns
            self.next_probe, self.probe_ok = float("inf"), True

    def publish(self, **kv) -> None:
        self.state.update(kv)
        self.state["pid"] = os.getpid()
        self.state["updated"] = time.time()
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_DIR / f".{self.instance}.json"
            tmp.write_text(json.dumps({**self.state, "instance": self.instance}))
            tmp.replace(STATE_DIR / f"{self.instance}.json")
        except OSError as e:
            log.debug("state file: %s", e)

    def probe(self) -> bool:
        """Tiny prompt through the account's CLI: is it signed in, does it have quota (Claude reports both windows)."""
        spec_info = self.api.call("GET", f"/v1/accounts/{self.account}/spec")
        work = Path(self.srv) / "work" / ".probe" / self.instance
        work.mkdir(parents=True, exist_ok=True)
        if self.run_as:
            try:
                shutil.chown(work, "aiflow-worker", "aiflow-worker")
            except (LookupError, PermissionError):
                pass
        if not (work / ".git").exists():  # codex refuses to run outside a git repository
            subprocess.run(self.run_as + ["git", "init", "-q", str(work)], capture_output=True, timeout=30)
        spec = RunSpec(prompt=PROBE_PROMPT, cwd=str(work), account_home=str(Path(self.srv) / "accounts" / self.account),
                       model=spec_info.get("model", ""), effort="", timeout_s=180, idle_s=150, control_url=self.control_url)
        adapter = get_adapter(spec_info["provider"])
        base_argv = adapter.argv
        adapter.argv = lambda sp: self.run_as + base_argv(sp)
        self.publish(state="probing")
        res = adapter.run(spec)
        if not (res.final_text or "").strip() and not res.error_kind:
            # A clean process exit without an answer is a provider failure, not evidence of sign-out.
            res.error_kind = "provider"
            res.error_message = "no answer"
        ok = not res.error_kind and bool((res.final_text or "").strip())
        detail = (res.error_message or res.final_text or "").strip()
        summary = (res.final_text or "").strip()[:300] if ok else f"{res.error_kind or 'no answer'}: {detail[:250]}"
        try:
            self.api.call("POST", f"/v1/accounts/{self.account}/probe", {
                "ok": ok, "error_kind": res.error_kind, "reset_at": res.reset_at.isoformat() if res.reset_at else None,
                "credential_expires_at": res.credential_expires_at.isoformat() if res.credential_expires_at else None,
                "quota": res.quota, "summary": summary, "duration_ms": int(res.duration_s * 1000),
                "tokens_in": res.tokens_in, "tokens_out": res.tokens_out, "cost_usd": res.cost_usd,
                "model": spec.model})
        except Exception as e:  # noqa: BLE001
            log.warning("probe result not recorded: %s", e)
        now = time.time()
        if ok:
            self.next_probe = now + self.probe_every_s
        else:  # retry at the provider's reset time when known, else in 30 minutes (5 minutes at the earliest)
            wait = (res.reset_at - datetime.now(timezone.utc)).total_seconds() + 30 if res.reset_at else 1800
            self.next_probe = now + max(300, min(wait, 6 * 3600))
        self.probe_ok = ok
        self.publish(state="idle" if ok else "unavailable", probe={"ok": ok, "error_kind": res.error_kind, "at": now,
                                                                   "next": self.next_probe,
                                                                   "credential_expires_at": res.credential_expires_at.isoformat()
                                                                   if res.credential_expires_at else None})
        log.info("probe %s: %s", self.account, "ok" if ok else summary)
        return ok

    def once(self) -> bool:
        """One poll: claim the best candidate and run it. True when a run happened."""
        if time.time() >= self.next_probe:
            self.probe()
        if not self.probe_ok:
            return False
        for release in self.api.call("GET", "/v1/version-releases/candidates",
                                     params={"account": self.account}).get("versions", []):
            try:
                claimed = self.api.call("POST", "/v1/version-releases/claim",
                                        {"project": release["project"], "version_id": release["version_id"],
                                         "repository": release.get("repository", ""),
                                         "account": self.account})
            except Conflict:
                continue
            if not claimed.get("claimed"):
                continue
            c = Claimed(run_id=claimed["run_id"], fence=claimed["fence"], run_token=claimed["run_token"],
                        account=self.account, provider=claimed["provider"], model="", effort="",
                        ticket=claimed["ticket"], project=claimed["project"], identity=tuple(claimed["identity"]),
                        step="version_release", context_pack=claimed.get("context_pack") or {})
            self.publish(state="working", ticket=c.ticket["key"], run_id=c.run_id,
                         step="version_release", since=time.time())
            result = run_version_release(self.api, c, claimed["version"], claimed["tracker_key"],
                                         self.srv, self.hb_s, self.run_as)
            try:
                self.api.call("POST", f"/v1/runs/{c.run_id}/result",
                              {"fence": c.fence, **result})
            except Exception as e:  # noqa: BLE001
                log.warning("version release result not recorded: %s", e)
            try:
                self.api.call("POST", f"/v1/runs/{c.run_id}/release", {"fence": c.fence})
            except Exception as e:  # noqa: BLE001
                log.warning("version release lease cleanup failed: %s", e)
            self.publish(state="idle", ticket=None, run_id=None, last_outcome=result.get("outcome"), since=time.time())
            return True
        chat_job = self.api.call("GET", "/v1/ba-chat/candidate", params={"account": self.account}).get("job")
        if chat_job:
            self.publish(state="working", ticket="BA chat", run_id=chat_job["job_id"], step="ba_chat", since=time.time())
            try:
                result = run_ba_chat_turn(self.api, chat_job, self.account, self.srv, self.control_url, self.run_as)
            except Exception as e:  # noqa: BLE001 - the result API receives only a safe error kind
                log.warning("BA chat turn failed account=%s error=%s", self.account, type(e).__name__)
                result = {"error_kind": "worker_error", "output": {}}
            try:
                self.api.call("POST", f"/v1/ba-chat/{chat_job['job_id']}/result",
                              {"account": self.account, "claim_token": chat_job["claim_token"],
                               "output": result.get("output") or {},
                               "error_kind": result.get("error_kind") or ""})
            except Exception as e:  # noqa: BLE001
                log.warning("BA chat result was not recorded account=%s error=%s", self.account, type(e).__name__)
            self.publish(state="idle", ticket=None, run_id=None, last_outcome="ba_chat", since=time.time())
            return True
        master = self.api.call("GET", "/v1/master/candidate", params={"account": self.account}).get("candidate")
        cands = ([master] if master else []) + self.api.call(
            "GET", "/v1/work/candidates", params={"account": self.account})["tickets"]
        if not cands:
            return False
        for cand in cands:
            step = cand.get("step", "dev")
            try:
                r = self.api.call("POST", "/v1/work/claim", {"ticket": cand["key"], "account": self.account, "step": step})
            except Conflict:
                continue
            if not r.get("claimed"):
                continue
            c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token=r["run_token"], account=self.account,
                        provider=r["provider"], model=r.get("model", ""), effort=r.get("effort", ""),
                        ticket=r["ticket"], project=r["project"], identity=tuple(r["identity"]), step=step,
                        findings=r.get("findings") or "", branch=r.get("branch") or "", pr=r.get("pr"),
                        last_dev_summary=r.get("last_dev_summary") or "",
                        verify_findings=r.get("verify_findings") or "", recent_comments=r.get("recent_comments") or [],
                        triage_answer=r.get("triage_answer") or "", triage_question=r.get("triage_question") or "",
                        related=r.get("related") or [], project_tickets=r.get("project_tickets") or [],
                        context_pack=r.get("context_pack") or {},
                        machine_tools=r.get("machine_tools") or {}, owner_register=r.get("owner_register") or [],
                        escalation_class=r.get("escalation_class") or "")
            self.publish(state="working", ticket=c.ticket["key"], run_id=c.run_id, step=step, since=time.time())
            log.info("claimed %s for %s (fence %s)", c.ticket["key"], step, c.fence)
            if step == "verify":
                res = run_verify(self.api, c, c.pr or {}, self.srv, self.control_url, self.hb_s, self.run_as)
            elif step == "triage":
                res = run_triage(self.api, c, self.srv, self.control_url, self.hb_s, self.run_as)
            elif step == "master":
                res = run_master(self.api, c, self.srv, self.control_url, self.hb_s, self.run_as)
            elif step == "intake":
                res = run_intake(self.api, c, self.srv, self.control_url, self.hb_s, self.run_as)
            elif step == "impact":
                res = run_impact(self.api, c, self.srv, self.control_url, self.hb_s, self.run_as)
            else:
                res = run_dev(self.api, c, self.srv, self.control_url, self.hb_s, self.run_as)
            attach_support_log(res, self.srv)
            try:
                self.api.call("POST", f"/v1/runs/{c.run_id}/result", res)
            except Exception as e:  # noqa: BLE001
                log.warning("result not recorded: %s", e)
            try:
                self.api.call("POST", f"/v1/runs/{c.run_id}/release", {"fence": c.fence})
            except Exception as e:  # noqa: BLE001
                log.warning("release failed: %s", e)
            log.info("%s finished: %s", c.ticket["key"], res.get("outcome"))
            if res.get("outcome") == "quota":
                self.probe_ok = False
                reset = res.get("reset_at")
                wait = 1800.0
                if reset:
                    try:
                        wait = (datetime.fromisoformat(reset) - datetime.now(timezone.utc)).total_seconds() + 30
                    except ValueError:
                        pass
                self.next_probe = time.time() + max(300, min(wait, 6 * 3600))
            self.publish(state="idle", ticket=None, run_id=None, last_outcome=res.get("outcome"), since=time.time())
            return True
        return False

    def release(self) -> str:
        try:
            return str((Path(self.srv) / "current").resolve())
        except OSError:
            return ""

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        self.publish(state="idle")
        started_on = self.release()
        while not self.stop.is_set():
            if started_on and self.release() != started_on:  # a new release is installed: exit between runs,
                log.info("new release installed; restarting")  # systemd starts the worker on the new code
                break
            try:
                ran = self.once()
                if self.state.get("state") == "error":
                    self.publish(state="idle" if self.probe_ok else "unavailable", error=None)
                if not ran:
                    self.stop.wait(self.poll_s)
            except Exception as e:  # noqa: BLE001
                log.warning("poll failed: %s", e)
                self.publish(state="error", error=str(e)[:200])
                self.stop.wait(max(self.poll_s, 30))
        self.publish(state="stopped")
