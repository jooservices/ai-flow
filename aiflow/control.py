"""The control: ONE service per machine that makes the running workers match the configuration, and runs the operator.

    every 30 seconds:  read the TOML files -> desired workers (shared 1..N, <KEY>-1..n per project)
                       -> start the missing ones, retire the surplus ones softly, disable the retired ones that have left
                       -> a changed configuration asks every worker to restart after its step, and the control restarts itself
                       -> find incidents and run the operator (AI read-only), notify the owner

The workers are separate systemd units (aiflow-work@<id>): when the control stops they keep working. Retiring never kills an AI:
the worker leaves after its current step with exit code 3, which its unit does not restart."""
from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

from .config import CONFIG_DIR, ConfigError
from .projects import desired_ids

DAY = 86400
MAX_CHANGES = 4  # unit changes per cycle: a wrong file can never rewrite the whole machine at once
PREFIX = "aiflow-work@"


@dataclass
class Plan:
    start: list[str] = field(default_factory=list)    # wanted, not enabled
    keep: list[str] = field(default_factory=list)     # wanted again before it left: drop the retire flag
    retire: list[str] = field(default_factory=list)   # enabled, no longer wanted: ask to leave after its step
    disable: list[str] = field(default_factory=list)  # retired and gone (or never ran): remove the unit

    def empty(self) -> bool:
        return not (self.start or self.keep or self.retire or self.disable)


def make_plan(desired: list[str], present: set[str], active: set[str], flagged: set[str]) -> Plan:
    """Pure. `present`: enabled or running instances; `active`: running; `flagged`: with a retire flag."""
    wanted = set(desired)
    return Plan(
        start=[i for i in desired if i not in present],
        keep=sorted(i for i in flagged if i in wanted),
        retire=sorted(i for i in present - wanted if i in active and i not in flagged),
        disable=sorted(i for i in present - wanted if i not in active))


class Backend(Protocol):
    def instances(self) -> tuple[set[str], set[str]]: ...  # (enabled or running, running)
    def enable(self, worker: str) -> None: ...
    def disable(self, worker: str) -> None: ...


class Systemd:
    """The workers as systemd user units `aiflow-work@<id>.service` (the Mac would get a launchd backend)."""

    def _run(self, *args: str) -> str:
        done = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60)
        if done.returncode != 0:
            raise RuntimeError((done.stdout + done.stderr).strip()[:300])
        return done.stdout

    @staticmethod
    def _names(text: str) -> set[str]:
        found = set()
        for line in text.splitlines():
            word = line.strip().split(" ")[0] if line.strip() else ""
            if word.startswith(PREFIX) and word.endswith(".service"):
                found.add(word[len(PREFIX):-len(".service")])
        return found

    def instances(self) -> tuple[set[str], set[str]]:
        enabled = self._names(self._run("list-dependencies", "default.target", "--plain", "--no-legend", "--no-pager"))
        running = self._names(self._run("list-units", f"{PREFIX}*", "--state=active,activating", "--plain", "--no-legend", "--no-pager"))
        return enabled | running, running

    def enable(self, worker: str) -> None:
        self._run("enable", "--now", f"{PREFIX}{worker}.service")

    def disable(self, worker: str) -> None:
        self._run("disable", f"{PREFIX}{worker}.service")


def config_hash(config_dir: Path) -> str:
    """One hash of every file the supervisor reads from the configuration directory."""
    digest = hashlib.sha256()
    for path in sorted([config_dir / "aiflow.toml", config_dir / "operator.toml", *(config_dir / "aiflow").glob("*.toml")]):
        if path.is_file():
            digest.update(path.name.encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


class Control:
    def __init__(self, backend: Backend, redis: Any, load: Callable[[], Any], config_dir: Path = CONFIG_DIR,
                 operator: Callable[[Any], Any] | None = None, log: Callable[[str], None] = print) -> None:
        self.backend, self.redis, self.load, self.config_dir, self.operator, self.log = backend, redis, load, config_dir, operator, log
        self.started = time.time()
        self.hash = ""

    def fail(self, message: str) -> None:
        self.log(f"control: {message}")
        self.redis.set("control:error", message[:400], 3600)  # the health check reports it

    def cycle(self, dry_run: bool = False) -> tuple[Plan, bool]:
        """One pass. Returns (the plan, True when the configuration changed and the control should restart itself)."""
        try:
            cfg = self.load()
        except ConfigError as error:
            self.fail(f"the configuration is wrong, nothing is changed: {error}")
            return Plan(), False
        machine = cfg.machine
        self.redis.set(f"hb:{machine}:control", str(int(time.time())), 300)
        self.redis.set(f"control:seen:{machine}", "1", 30 * DAY)
        plan = Plan()
        if cfg.registry.from_files:
            plan = self.reconcile(cfg, dry_run)
        changed = False
        digest = config_hash(self.config_dir)
        known = self.hash or self.redis.get("control:confighash") or ""
        if known and known != digest and not dry_run:
            self.log("control: the configuration changed; every worker restarts after its step")
            self.redis.set("restart", str(int(time.time())), 7 * DAY)
            changed = True
        if not dry_run:
            self.redis.set_persistent("control:confighash", digest)
        self.hash = digest
        if not changed and not dry_run and self.operator is not None:
            self.operator(cfg)
        return plan, changed

    def reconcile(self, cfg: Any, dry_run: bool) -> Plan:
        desired = desired_ids(cfg.registry, cfg.machine)
        present, active = self.backend.instances()
        flagged = {i for i in present if self.redis.get(f"retire:{cfg.machine}:{i}") is not None}
        if not desired and present:
            self.fail("the configuration asks for no worker at all; refusing to stop every worker (set workers to 1 or more)")
            return Plan()
        plan = make_plan(desired, present, active, flagged)
        if dry_run or plan.empty():
            return plan
        budget = MAX_CHANGES
        for worker in plan.start:
            if budget <= 0:
                break
            budget -= 1
            self.redis.delete(f"retire:{cfg.machine}:{worker}")
            self.backend.enable(worker)
            self.log(f"control: started worker {worker}")
        for worker in plan.keep:
            self.redis.delete(f"retire:{cfg.machine}:{worker}")
            self.log(f"control: worker {worker} is wanted again")
        for worker in plan.retire:
            if budget <= 0:
                break
            budget -= 1
            self.redis.set(f"retire:{cfg.machine}:{worker}", "1", DAY)
            self.log(f"control: worker {worker} will leave after its current step")
        for worker in plan.disable:
            if budget <= 0:
                break
            budget -= 1
            self.backend.disable(worker)
            self.redis.delete(f"retire:{cfg.machine}:{worker}")
            self.log(f"control: disabled worker {worker}")
        self.redis.delete("control:error")
        return plan


def main(argv: list[str] | None = None) -> int:
    import argparse
    from . import config
    from .jira import Jira
    from .lock import Redis
    from .operator import Operator
    from .supervisor import Supervisor, log
    parser = argparse.ArgumentParser(prog="aiflow-control", description="Make the workers match the configuration and run the operator.")
    parser.add_argument("--once", action="store_true", help="one pass, then exit")
    parser.add_argument("--dry-run", action="store_true", help="print what would change and change nothing (implies --once)")
    parser.add_argument("--interval", type=int, default=30, help="seconds between passes")
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
    except ConfigError as error:
        print(f"CONFIG ERROR: {error}", file=sys.stderr)
        return 2
    redis = Redis(cfg.redis_host, cfg.redis_port, cfg.redis_db, cfg.redis_prefix, cfg.redis_password, cfg.redis_user)
    from dataclasses import replace
    holder = replace(cfg, label="control")
    supervisor = Supervisor(holder, Jira(cfg.root), redis)
    operator = Operator(supervisor, holder.operator_policy)
    control = Control(Systemd(), redis, config.load, operator=lambda _cfg: operator.cycle(), log=log)
    if args.dry_run or args.once:
        plan, _ = control.cycle(dry_run=args.dry_run)
        print(f"start {plan.start or '-'} | keep {plan.keep or '-'} | retire {plan.retire or '-'} | disable {plan.disable or '-'}")
        return 0
    while True:
        try:
            _, changed = control.cycle()
        except Exception as error:  # a failed pass never stops the control
            log(f"control: pass failed: {type(error).__name__}: {error}")
            changed = False
        if changed:
            return 0  # systemd starts it again with the new configuration
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
