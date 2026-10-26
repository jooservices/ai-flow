"""Master AI rescue (JAIF-92): when Claude takes a long Blocked or over-time ticket before a person is involved.

Pure rules: `evaluate` takes the facts of one ticket and the `[master]` settings of `aiflow.toml` and returns what to do, it reads
and writes nothing. The operator (`operator.py`) queues the result as a `rescue` incident and performs it.

    Blocked Config | Attempts | Rework | Unclear for its wait  -> rescue    (Quota and Offline are never handed to Claude)
    In Progress for one stretch, or total work time over limit -> rescue    (CI time is not work time)
    money, credentials, deleting data, release, requirements,
    merging by hand, runtime setup, installed software          -> owner, at once
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .config import ConfigError

HANDOVER = "Handover"            # the status defined by JAIF-102
RESCUE_LABEL = "master-rescue"
RESCUE_AGENT = "Claude"
CONTEXT_UNAVAILABLE = "ticket context unavailable"
PENDING_RESCUE_STATES = frozenset({"active", "handover_pending", "owner_pending", "doctor_pending", "completing"})
WORK_STATUSES = ("In Progress", "In Review")  # their time is work time; the pull-request gate (CI) is not
WAIT_DEFAULTS = {"Config": 10, "Attempts": 0, "Rework": 0, "Unclear": 30}
NEVER_RESCUED = ("Quota", "Offline")  # they return by themselves when an AI is back
NUMBERS = {"in_progress_minutes": 60, "total_work_minutes": 120, "master_minutes": 60, "total_cap_minutes": 180}
TEXTS = {"model": "", "taken_by_field": "Taken by"}
_REQUIREMENT_ACTION = (r"add|added|adds|adding|create|created|creates|creating|change|changed|changes|changing|"
                       r"update|updated|updates|updating|modify|modified|modifies|modifying|revise|revised|revises|"
                       r"revising|alter|altered|alters|altering|rewrite|rewrote|rewritten|rewriting")
_REQUIREMENT_TARGET = r"requirements?|product direction|acceptance criteria"
_REQUIREMENT_CHANGE = (rf"\b(?:{_REQUIREMENT_ACTION})\b.{{0,40}}\b(?:{_REQUIREMENT_TARGET})\b|"
                       rf"\b(?:{_REQUIREMENT_TARGET})\b.{{0,40}}\b(?:{_REQUIREMENT_ACTION})\b")
_INSTALLED_ACTION = (r"install(?:s|ed|ing)?|uninstall(?:s|ed|ing)?|reinstall(?:s|ed|ing)?|"
                     r"upgrad(?:e|es|ed|ing)|downgrad(?:e|es|ed|ing)|updat(?:e|es|ed|ing)|"
                     r"modif(?:y|ies|ied|ying)|chang(?:e|es|ed|ing)|remov(?:e|es|ed|ing)|"
                     r"patch(?:es|ed|ing)?|pin(?:s|ned|ning)?|roll[ -]?back(?:s|ing)?")
_INSTALLED_TARGET = r"(?:installed\s+)?(?:software|packages?|tools?|binar(y|ies)|cli|dependencies|runtimes?)"
OWNER_ONLY = (
    ("spending money", r"\b(pay|payment|refund|billing|invoice|purchase|spend(ing)?|subscription|money|costs?|prices?|paid plan|fees?)\b|"
                       r"\bcharg(?:e|es|ed|ing)\b.{0,30}\b(card|account|customer|invoice|payment|fee|subscription|money)\b|"
                       r"\b(card|account|customer|invoice|payment|fee|subscription|money)\b.{0,30}\bcharg(?:e|es|ed|ing)\b"),
    ("credentials, tokens or secrets", r"\b(credentials?|secrets?|tokens?|passwords?|api[ _-]?keys?|ssh[ _-]?keys?|private key)\b"),
    ("deleting data", r"\b(delet(e|es|ed|ing)|remov(e|es|ed|ing)|destroy(s|ed|ing)?|drop(ped|ping|s)?|purge(d|s|ing)?|wipe(d|s|ing)?)\b"
                       r".{0,40}\b((production|prod)\s+)?(data|dbs?|databases?|tables?|repositor(y|ies)|branch(es)?|records?)\b|force-push"),
    ("a release", r"\breleas(e|es|ed|ing)\b|\bproduction deploy|\bpublish(?:es|ed|ing)? (?:the )?(?:release|version)\b"),
    ("changing the requirements", _REQUIREMENT_CHANGE),
    ("merging by hand", r"\bmerg(e|ing)\b.{0,15}\b(by hand|manually)\b|\bmanual(ly)? merge"),
    ("runtime setup", r"\b(set[ -]?up|setup|configur(?:e|es|ed|ing|ation)|provision(?:s|ed|ing)?|bootstrap(?:s|ped|ping)?|initiali[sz](?:e|es|ed|ing|ation))\b.{0,40}\b(runtime|server|host|network|systemd|environment)\b|"
                      r"\b(runtime|server|host|network|systemd|environment)\b.{0,40}\b(set[ -]?up|setup|configur(?:e|es|ed|ing|ation)|provision(?:s|ed|ing)?|bootstrap(?:s|ped|ping)?|initiali[sz](?:e|es|ed|ing|ation))\b|\bprovision"),
    ("changing installed software", rf"\b(?:{_INSTALLED_ACTION})\b.{{0,40}}\b{_INSTALLED_TARGET}\b|missing tool|not installed"),
)
OWNER_PATTERNS = tuple((topic, re.compile(pattern, re.I)) for topic, pattern in OWNER_ONLY)


@dataclass(frozen=True)
class MasterSettings:
    waits: dict[str, int] = field(default_factory=lambda: dict(WAIT_DEFAULTS))  # Blocked kind -> minutes before the rescue
    in_progress_minutes: int = 60    # In Progress this long in one stretch
    total_work_minutes: int = 120    # total work time, CI excluded
    master_minutes: int = 60         # Claude's own rescue budget
    total_cap_minutes: int = 180     # work limit + rescue budget; a ticket never gets more
    model: str = ""                  # Claude:model[:effort] of the rescue; empty = the model configured for Claude in agent_models
    taken_by_field: str = "Taken by"


def parse(table: dict[str, Any], where: str) -> MasterSettings:
    """The `[master]` table of aiflow.toml; anything not understood stops the worker with a clear message."""
    if not isinstance(table, dict):
        raise ConfigError(f"{where}: must be a TOML table.")
    waits = dict(WAIT_DEFAULTS)
    values: dict[str, Any] = {}
    known = [f"{kind.lower()}_wait_minutes" for kind in WAIT_DEFAULTS] + list(NUMBERS) + list(TEXTS)
    for name, value in table.items():
        if not _parse_wait(name, value, waits, where):
            _parse_setting(name, value, values, known, where)
    settings = MasterSettings(waits=waits, **values)
    _validate_settings(settings, where)
    return settings


def _parse_wait(name: str, value: Any, waits: dict[str, int], where: str) -> bool:
    if not name.endswith("_wait_minutes"):
        return False
    kind = name.removesuffix("_wait_minutes").capitalize()
    if kind in NEVER_RESCUED:
        raise ConfigError(f"{where}: `{name}` is not allowed: Quota and Offline are never handed to Claude.")
    if kind not in WAIT_DEFAULTS:
        return False
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{where}: `{name}` must be a whole number of 0 or more.")
    waits[kind] = value
    return True


def _parse_setting(name: str, value: Any, values: dict[str, Any], known: list[str], where: str) -> None:
    if name in NUMBERS:
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ConfigError(f"{where}: `{name}` must be a whole number of at least 1.")
        values[name] = value
    elif name in TEXTS:
        if not isinstance(value, str) or (name == "taken_by_field" and not value.strip()):
            raise ConfigError(f"{where}: `{name}` must be text.")
        values[name] = value.strip()
    else:
        raise ConfigError(f"{where}: unknown setting `{name}` (known: {', '.join(known)}).")


def _validate_settings(settings: MasterSettings, where: str) -> None:
    if settings.model:
        parts = settings.model.split(":")
        if (len(parts) not in (2, 3) or parts[0].strip() != RESCUE_AGENT
                or any(not part.strip() for part in parts[1:])):
            raise ConfigError(f"{where}: `model` must name Claude and a model, as Claude:model[:effort].")
    if settings.total_cap_minutes < settings.total_work_minutes + settings.master_minutes:
        raise ConfigError(f"{where}: `total_cap_minutes` ({settings.total_cap_minutes}) must cover `total_work_minutes` "
                          f"+ `master_minutes` ({settings.total_work_minutes + settings.master_minutes}).")


def check_line(settings: MasterSettings) -> str:
    """The `[master]` settings as `aiflow-work check` shows them."""
    waits = ", ".join(f"{kind} {minutes} min" for kind, minutes in settings.waits.items())
    return (f"  master rescue: waits before Claude takes a Blocked ticket: {waits} (Quota and Offline never); In Progress "
            f"{settings.in_progress_minutes} min in one stretch or {settings.total_work_minutes} min of work; Claude budget "
            f"{settings.master_minutes} min, total cap {settings.total_cap_minutes} min")


def owner_only_topic(*texts: Any) -> str:
    """The first owner-only topic the texts mention, or an empty string."""
    text = "\n".join(str(item) for item in texts if item)
    return next((topic for topic, pattern in OWNER_PATTERNS if pattern.search(text)), "")


@dataclass(frozen=True)
class Verdict:
    mode: str                  # rescue | owner
    trigger: str               # why the ticket qualifies
    topic: str = ""            # the owner-only topic, when mode is owner
    remaining_minutes: int = 0  # Claude's budget for this ticket


def remaining_budget(settings: MasterSettings, worked_minutes: float) -> int:
    """Claude's minutes: `master_minutes`, but never beyond the ticket's total cap."""
    return max(0, min(settings.master_minutes, int(settings.total_cap_minutes - worked_minutes)))


def trigger_of(ticket: dict[str, Any], settings: MasterSettings, minutes_in_status: float) -> str:
    """Why this ticket qualifies for a rescue ('' when it does not)."""
    status, kind = ticket["status"], ticket.get("blocked_kind", "")
    if status == "Blocked":
        if kind in NEVER_RESCUED:
            return ""
        wait = settings.waits.get(kind)
        if wait is not None and minutes_in_status >= wait:
            return f"Blocked ({kind}) for {int(minutes_in_status)} min, wait {wait} min"
    if status == "In Progress" and ticket.get("stretch_minutes", 0) >= settings.in_progress_minutes:
        return f"In Progress for {int(ticket['stretch_minutes'])} min in one stretch (limit {settings.in_progress_minutes})"
    if ticket.get("worked_minutes", 0) >= settings.total_work_minutes:
        return f"{int(ticket['worked_minutes'])} min of total work time (limit {settings.total_work_minutes})"
    return ""


def evaluate(ticket: dict[str, Any], settings: MasterSettings | None, minutes_in_status: float) -> Verdict | None:
    """What the master does with one ticket: None, a rescue by Claude, or the owner. One rescue per ticket."""
    if settings is None:
        return None
    if RESCUE_LABEL in ticket.get("labels", []) or ticket.get("rescued"):
        return None
    if not ticket.get("context_complete", False):
        return Verdict("owner", "Required Jira description or recent comments could not be read", CONTEXT_UNAVAILABLE)
    trigger = trigger_of(ticket, settings, minutes_in_status)
    comments = ticket.get("last_comments", [])
    if not isinstance(comments, list):
        comments = []
    comment_texts = [comment.get("body", "") for comment in comments if isinstance(comment, dict)]
    topic = owner_only_topic(ticket.get("summary", ""), ticket.get("reason", ""), ticket.get("description", ""), *comment_texts)
    if topic:
        return Verdict("owner", trigger or f"Owner-only work identified: {topic}", topic)
    if not trigger:
        return None
    remaining = remaining_budget(settings, ticket.get("worked_minutes", 0))
    if remaining <= 0:
        return Verdict("owner", trigger, "no rescue budget left under the ticket's total cap")
    return Verdict("rescue", trigger, remaining_minutes=remaining)
