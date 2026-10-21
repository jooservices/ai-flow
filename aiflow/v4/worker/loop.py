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

from .client import Conflict, ControlClient
from .dev import Claimed, run_dev

log = logging.getLogger("aiflow4.worker")
STATE_DIR = Path("/run/aiflow4/workers")
RUN_AS = ["setpriv", "--reuid=aiflow-worker", "--regid=aiflow-worker", "--init-groups", "--"]


class Worker:
    def __init__(self, account: str, control_url: str, node_token: str, node_name: str, srv_root: str,
                 heartbeat_s: int = 30, poll_s: int = 20, run_as: list[str] | None = None) -> None:
        self.account = account
        self.api = ControlClient(control_url, node_token)
        self.api.node_name = node_name  # type: ignore[attr-defined]
        self.control_url = control_url
        self.node = node_name
        self.srv = srv_root
        self.hb_s = heartbeat_s
        self.poll_s = poll_s
        self.run_as = RUN_AS if run_as is None else run_as
        self.stop = threading.Event()
        self.state: dict = {"account": account, "state": "idle", "since": time.time()}

    def publish(self, **kv) -> None:
        self.state.update(kv)
        self.state["pid"] = os.getpid()
        self.state["updated"] = time.time()
        try:
            STATE_DIR.mkdir(parents=True, exist_ok=True)
            tmp = STATE_DIR / f".{self.account}.json"
            tmp.write_text(json.dumps(self.state))
            tmp.replace(STATE_DIR / f"{self.account}.json")
        except OSError as e:
            log.debug("state file: %s", e)

    def once(self) -> bool:
        """One poll: claim the best candidate and run it. True when a run happened."""
        cands = self.api.call("GET", "/v1/work/candidates", params={"account": self.account})["tickets"]
        if not cands:
            return False
        for cand in cands:
            try:
                r = self.api.call("POST", "/v1/work/claim", {"ticket": cand["key"], "account": self.account, "step": "dev"})
            except Conflict:
                continue
            if not r.get("claimed"):
                continue
            c = Claimed(run_id=r["run_id"], fence=r["fence"], run_token=r["run_token"], account=self.account,
                        provider=r["provider"], model=r.get("model", ""), effort=r.get("effort", ""),
                        ticket=r["ticket"], project=r["project"], identity=tuple(r["identity"]))
            self.publish(state="working", ticket=c.ticket["key"], run_id=c.run_id, since=time.time())
            log.info("claimed %s (fence %s)", c.ticket["key"], c.fence)
            res = run_dev(self.api, c, self.srv, self.control_url, self.hb_s, self.run_as)
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

    def run_forever(self) -> None:
        signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        self.publish(state="idle")
        while not self.stop.is_set():
            try:
                if not self.once():
                    self.stop.wait(self.poll_s)
            except Exception as e:  # noqa: BLE001
                log.warning("poll failed: %s", e)
                self.publish(state="error", error=str(e)[:200])
                self.stop.wait(max(self.poll_s, 30))
        self.publish(state="stopped")
