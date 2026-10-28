"""Worker for one AI account (systemd unit aiflow4-worker@<account>).

The worker process runs as root so it can read the node token; the AI CLI and git run as the unprivileged AI
user (setpriv), so nothing the AI does can reach the token or the control plane directly. It publishes its
state in /run/aiflow4/workers/<account>.json for the node agent's heartbeat.
"""
from __future__ import annotations

import json
import logging
import os
import signal
import threading
import time
from pathlib import Path

from ..control.support_bundle import MAX_LOG_BYTES, cap_log_bytes

from .quota_cli import read_quota_isolated
from .client import Conflict, ControlClient
from .dev import Claimed, run_dev
from .triage import run_triage
from .impact import run_impact
from .intake import run_intake
from .verify import run_verify
from .master import run_master
from .version_release import run_version_release
from .post_merge import run_post_merge
from .ba_chat import run_turn as run_ba_chat_turn

log = logging.getLogger("aiflow4.worker")
STATE_DIR = Path("/run/aiflow4/workers")
RUN_AS = ["setpriv", "--reuid=aiflow-worker", "--regid=aiflow-worker", "--init-groups", "--"]


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
        self.state_lock = threading.Lock()
        self.state: dict = {"account": self.account, "state": "idle", "since": time.time()}

    def publish(self, **kv) -> None:
        with self.state_lock:
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

    def refresh_quota(self) -> bool:
        """Run a fixed provider usage command under an account lease; no prompt or model run is started."""
        response = self.api.call("POST", f"/v1/accounts/{self.account}/quota-refresh/claim", allow_not_found=True)
        claim = (response or {}).get("job")
        if not claim:
            return False
        self.publish(quota_refresh={"state": "running", "at": claim["claimed_at"]})
        home = str(Path(self.srv) / "accounts" / self.account)
        try:
            quota, error, observed_at = read_quota_isolated(claim["provider"], home, self.run_as)
            self.api.call("POST", f"/v1/accounts/{self.account}/quota-refresh/result", {
                "lease_token": claim["lease_token"], "ok": bool(quota), "quota": quota,
                "error": error, "model": "", "observed_at": observed_at})
        except Exception as e:  # noqa: BLE001
            log.warning("quota refresh result not recorded account=%s error=%s", self.account, type(e).__name__)
            self.publish(quota_refresh={"state": "failed", "ok": False, "at": claim["claimed_at"],
                                        "error": "refresh result could not be recorded"})
            return False
        self.publish(quota_refresh={"state": "complete" if quota else "failed", "ok": bool(quota),
                                    "at": claim["claimed_at"], "error": error})
        log.info("quota refresh %s: %s", self.account, "ok" if quota else error)
        return bool(quota)

    def quota_refresh_loop(self) -> None:
        """Poll the DB-backed quota schedule independently of task assignment and execution."""
        while not self.stop.is_set():
            try:
                self.refresh_quota()
            except Exception as e:  # noqa: BLE001 - refresh is advisory and never stops task routing
                log.warning("quota refresh failed account=%s error=%s", self.account, type(e).__name__)
            if self.stop.wait(30):
                return

    def once(self) -> bool:
        """One poll: claim the best candidate and run it. True when a run happened."""
        release_candidates = self.api.call("GET", "/v1/version-releases/candidates",
                                            params={"account": self.account}, allow_not_found=True) or {}
        for release in release_candidates.get("versions", []):
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
            "GET", "/v1/work/candidates", params={"account": self.account, "worker_id": self.instance})["tickets"]
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
            elif step == "post_merge":
                res = run_post_merge(self.api, c, self.srv, self.hb_s, self.run_as)
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
        quota_thread = None
        if self.instance == self.account:  # one scheduler per logical account; extra sessions share its DB lease
            quota_thread = threading.Thread(target=self.quota_refresh_loop, name=f"quota-refresh-{self.instance}",
                                            daemon=True)
            quota_thread.start()
        started_on = self.release()
        while not self.stop.is_set():
            if started_on and self.release() != started_on:  # a new release is installed: exit between runs,
                log.info("new release installed; restarting")  # systemd starts the worker on the new code
                break
            try:
                ran = self.once()
                if self.state.get("state") == "error":
                    self.publish(state="idle", error=None)
                if not ran:
                    self.stop.wait(self.poll_s)
            except Exception as e:  # noqa: BLE001
                log.warning("poll failed: %s", e)
                self.publish(state="error", error=str(e)[:200])
                self.stop.wait(max(self.poll_s, 30))
        self.stop.set()
        if quota_thread:
            quota_thread.join(timeout=30)
        self.publish(state="stopped")
