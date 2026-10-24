"""Where and how something runs belongs to the project configuration, never to a ticket (docs/v4/environment.md).

Two halves live here:

* the environment section of a project (`environment:` in aiflow-control/projects/<KEY>.yaml): the test services,
  base URLs, databases, browsers and secrets (by name) a run may use, and which runs may use them. The run receives
  the items as environment variables and its notes list their names;
* the server-wording rule: text that names a host, an IP address, a machine, a network or an access instruction is
  not a requirement. The intake check rejects it, the intake AI removes it, and the open-ticket scan finds it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Any

KINDS = ("service", "base_url", "database", "browser", "secret")
RUN_STEPS = ("dev", "verify", "post_merge")
DEFAULT_RUN_STEPS = ("dev", "verify")
NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
MAX_ITEMS = 100
MAX_VALUE = 500
# a non-secret value may name a host (that is what the configuration is for) but never carry a credential
CREDENTIAL_IN_VALUE = re.compile(r"://[^/\s:@]+:[^/\s@]+@")
MISSING_ENV_MARKER = re.compile(r"MISSING-ENV:\s*([^\n]+?)(?:\s+[-–—]\s+|$)", re.I)


@dataclass
class EnvItem:
    name: str
    kind: str
    value: str = ""
    runs: tuple[str, ...] = RUN_STEPS
    description: str = ""

    @property
    def secret(self) -> bool:
        return self.kind == "secret"

    def public(self) -> dict[str, Any]:
        """What leaves the control plane: a secret never carries a value, only its name."""
        return {"name": self.name, "kind": self.kind, "value": "" if self.secret else self.value,
                "runs": list(self.runs), "description": self.description}

    def fingerprint(self) -> str:
        return "|".join((self.name, self.kind, self.value, ",".join(self.runs)))


def parse_items(raw: Any) -> tuple[list[EnvItem], list[str]]:
    """The environment section as written in the project file -> (valid items, one message per problem)."""
    items: list[EnvItem] = []
    errors: list[str] = []
    if raw in (None, "", [], {}):
        return items, errors
    if isinstance(raw, dict):  # {NAME: {kind: ..}} is accepted too
        raw = [{"name": name, **(value if isinstance(value, dict) else {"value": value})} for name, value in raw.items()]
    if not isinstance(raw, list):
        return items, ["the environment section must be a list of items"]
    seen: set[str] = set()
    for index, entry in enumerate(raw[:MAX_ITEMS + 1], 1):
        if index > MAX_ITEMS:
            errors.append(f"at most {MAX_ITEMS} environment items are allowed")
            break
        if not isinstance(entry, dict):
            errors.append(f"item {index} must be a mapping with a name and a kind")
            continue
        name = str(entry.get("name") or "").strip()
        kind = str(entry.get("kind") or "").strip().lower()
        label = name or f"item {index}"
        if not NAME_RE.fullmatch(name):
            errors.append(f"{label}: the name must be an environment variable name such as WP_BASE_URL")
            continue
        if name in seen:
            errors.append(f"{name}: listed more than once")
            continue
        if kind not in KINDS:
            errors.append(f"{name}: the kind must be one of {', '.join(KINDS)}")
            continue
        runs = entry.get("runs")
        runs = DEFAULT_RUN_STEPS if runs in (None, "", []) else tuple(
            dict.fromkeys(str(r).strip().lower() for r in (runs if isinstance(runs, (list, tuple)) else [runs])))
        if not set(runs) <= set(RUN_STEPS):
            errors.append(f"{name}: runs may only be {', '.join(RUN_STEPS)}")
            continue
        value = str(entry.get("value") if entry.get("value") is not None else "").strip()
        if kind == "secret" and value:
            errors.append(f"{name}: a secret is listed by name only; its value lives on the machine, never in the file")
            continue
        if len(value) > MAX_VALUE or "\n" in value or "\r" in value:
            errors.append(f"{name}: the value must be one line of at most {MAX_VALUE} characters")
            continue
        if CREDENTIAL_IN_VALUE.search(value):
            errors.append(f"{name}: the value carries a credential; list the credential as a secret by name instead")
            continue
        if kind != "secret" and not value:
            errors.append(f"{name}: a {kind} item needs a value")
            continue
        seen.add(name)
        items.append(EnvItem(name=name, kind=kind, value=value, runs=runs,
                             description=str(entry.get("description") or "").strip()[:200]))
    return items, errors


def items_of(raw: Any) -> list[EnvItem]:
    return parse_items(raw)[0]


def for_step(items: list[EnvItem], step: str) -> list[EnvItem]:
    return [i for i in items if step in i.runs]


def run_environment(items: list[EnvItem], step: str, node_env: dict[str, str] | None = None) -> dict[str, str]:
    """Environment variables of a run: configured values, and secrets by name from the machine's own file."""
    node_env = node_env or {}
    out: dict[str, str] = {}
    for item in for_step(items, step):
        value = node_env.get(item.name, "") if item.secret else item.value
        if value:
            out[item.name] = value
    return out


def names_for_notes(items: list[EnvItem], step: str, provided: dict[str, str]) -> list[dict[str, Any]]:
    return [{"name": i.name, "kind": i.kind, "provided": i.name in provided} for i in for_step(items, step)]


def notes_block(items: list[EnvItem], step: str, provided: dict[str, str]) -> str:
    """'Environment of this run' section of the prompt: names only, never a secret value."""
    lines = ["\nEnvironment of this run (provided by the flow from the project configuration):"]
    rows = names_for_notes(items, step, provided)
    if rows:
        for row in rows:
            state = "set" if row["provided"] else "NOT PROVIDED on this machine"
            lines.append(f"- {row['name']} ({row['kind']}, {state})")
    else:
        lines.append("- none configured for this run")
    lines.append("Read these variables by name; they are already set. Never ask for, look up or write a server, host,\n"
                 "address, network or account into code, tests, docs or the ticket. A test reads its base URL, database\n"
                 "or secret from the variable. If you need something that is not listed, or a listed item is NOT PROVIDED,\n"
                 "stop and report `RESULT: BLOCKED: MISSING-ENV: <NAME>[, <NAME>] - <what could not run>` with a name from\n"
                 "this list; AI Flow provides it. Acceptance criteria that can only be proven on one particular machine\n"
                 "are verified by AI Flow after the merge (canary, release checks); do not stop for them, and never ask the\n"
                 "owner about servers.")
    return "\n".join(lines) + "\n"


def parse_missing_environment(text: str, items: list[EnvItem]) -> tuple[list[str], list[str], bool]:
    """`MISSING-ENV: A, B - why` -> (names that are in the configuration, other words, marker present).
    Only a configured name counts; free text is never an environment item."""
    marker = MISSING_ENV_MARKER.search(text or "")
    if not marker:
        return [], [], False
    known = {i.name for i in items}
    words = [w for w in re.split(r"\s*(?:,|\band\b|\s)\s*", marker.group(1).strip()) if w]
    names = list(dict.fromkeys(w.upper() for w in words if w.upper() in known))
    unknown = list(dict.fromkeys(w for w in words if w.upper() not in known))
    return names, unknown, True


# --- the server-wording rule -------------------------------------------------------------------------------------
_OCTET = r"(?:25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)"
_HOST_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_MACHINE_NOUN = r"(?:vms?|servers?|machines?|hosts?|boxes|box|computers?|nodes?|laptops?|pcs?)"
RULES: tuple[tuple[str, re.Pattern[str]], ...] = tuple((kind, re.compile(rx, re.I)) for kind, rx in (
    ("ip address", rf"(?<![\w.]){_OCTET}(?:\.{_OCTET}){{3}}(?:/\d{{1,2}})?(?::\d{{2,5}})?(?![\w]|\.\d)"),
    ("host", rf"(?<![\w.@-])(?:{_HOST_LABEL}\.)+(?!(?:md|py|html?|css|js|json|yaml|yml|toml|txt|sh|rst|xml|in)\b)[a-z]{{2,63}}(?![\w-]|\.[a-z0-9])"),
    ("host", r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:local|lan|internal|intranet|corp|home|localdomain)\b"),
    ("host", r"\blocalhost:\d{2,5}\b|\bssh://\S+|\bhost\s?name\s*(?:[:=]|is\b|called\b)"),
    # A local hostname is still a host when it appears in a URL without an explicit port.
    ("host", r"\bhttps?://localhost\b"),
    ("machine", rf"\b(?:shared|dev|development|lab|staging|stage|production|prod|jump|build|ci|qa|uat|remote|physical|"
               rf"virtual)\s+{_MACHINE_NOUN}\b"),
    ("machine", r"\bshared\s+(?:docker|database|db|redis|postgres(?:ql)?|mysql|mariadb|wordpress|environment|"
               r"integration environment|site)\b"),
    ("machine", r"\bWSL2?\b|\b(?:my|your|the owner'?s)\s+(?:laptop|pc|computer|machine)\b"),
    # A generic instruction to use a machine is still a run-location requirement, even with no machine name.
    ("machine", r"\b(?:use|run|test|execute|deploy|install|build|start|connect(?:\s+to)?|access)\s+"
                r"(?:(?:the|a|an)\s+)?(?:server|machine|vm|box|computer|node)\b"),
    # Local names such as node4 often have no DNS suffix. Match them when they are used as a run destination.
    ("host", r"\b(?:on|at|to|against|via|from)\s+(?:the\s+)?"
             r"(?:node|host|server|vm|machine|box|worker|runner|agent)[-_]?\d+\b"),
    # Generic machine references still leave a run-location question, even when no hostname follows.
    ("machine", r"\b(?:run|test|execute|deploy|install|build|start)\b.{0,80}\b(?:on|at|against|to)\s+(?:the\s+)?(?:server|machine)\b"),
    ("host", r"\b(?:use|connect(?:\s+to)?|access|run|test|deploy)\s+(?:the\s+)?host"
             r"(?!\s+(?:or|and|for|is|to|with|on|as|at|should|could|would)\b)"
             r"(?:\s+[a-z0-9][a-z0-9.-]*)?\b"),
    ("host", r"\bhost\s+(?!or\b|and\b|for\b|is\b|to\b|with\b|on\b|as\b|at\b|should\b|could\b|would\b)"
             r"[a-z0-9][a-z0-9.-]*\b"),
    ("network", r"\b(?:vpn|vlan|subnet|bastion|intranet|dmz)\b|\bjump\s?host\b|\bport[- ]forward\w*|\bfirewall rule\b"),
    ("access instruction", r"\b(?:ssh|rdp|scp|sftp|telnet)\s+(?:in)?to\b|\b(?:ssh|rdp)\s+(?:access|key|session|tunnel)\b|"
                           r"\bssh\s+[\w.-]+@"),
    ("access instruction", rf"\b(?:log\s?in|login|sign\s?in|connect|remote\s+in)\s+(?:in)?to\s+(?:the\s+|a\s+|your\s+)?"
                           rf"(?:server|vm|host|machine|box|node|jump\s?host|bastion|(?:shared|dev|lab|staging|prod\w*)\s+\w+)\b"),
    ("access instruction", r"\b(?:ask|get|request)\s+(?:for\s+)?(?:access to|credentials|the password|a password)\b"),
    ("access instruction", r"\b(?:password|passwd|pwd|secret|token|api[_ -]?key)\s*[:=]\s*[^\s,;]{3,}"),
    ("jira action", r"\b(?:write|post|add|leave|put)\s+(?:a|an|the)\s+jira\s+(?:comment|note)\b|"
                    r"\b(?:comment|post|write)\s+(?:on|in|to)\s+(?:the\s+)?jira\b|"
                    r"\b(?:transition|move)\s+(?:the\s+)?jira\s+(?:ticket|issue)\b"),
))
REPLACEMENT = "[server detail removed]"
_IPV6_CANDIDATE = re.compile(r"(?<![0-9a-f:.])[0-9a-f:.]{2,}(?![0-9a-f:.])", re.I)


def _wording_spans(text: str) -> list[tuple[int, int, str]]:
    """Find all rule matches, validating IPv6 candidates with the standard library parser."""
    text = text or ""
    found = [(m.start(), m.end(), kind) for kind, rx in RULES for m in rx.finditer(text)]
    for match in _IPV6_CANDIDATE.finditer(text):
        candidate = match.group().rstrip(".")  # a final sentence period is punctuation, not part of the address
        if candidate.count(":") < 2:
            continue
        try:
            ip = ip_address(candidate)
        except ValueError:
            continue
        if ip.version == 6:
            found.append((match.start(), match.start() + len(candidate), "ip address"))
    return sorted(found)


def find_server_wording(text: str) -> list[dict[str, str]]:
    """Every place the text names a host, an IP address, a machine, a network, an access instruction or a Jira
    action: [{"kind", "text"}] in reading order (empty when the text is a requirement only)."""
    found = _wording_spans(text)
    out, last_end = [], -1
    for start, end, kind in found:  # overlapping hits count once
        if start < last_end:
            continue
        out.append({"kind": kind, "text": (text or "")[start:end][:120]})
        last_end = end
    return out


SERVER_QUESTION = re.compile(
    r"\b(?:servers?|hosts?|vms?|machines?|ip address(?:es)?|address(?:es)?|network|vpn|ssh|credentials|"
    r"logins?|passwords?|base urls?|endpoints? to use)\b|"
    r"\bwhere\s+(?:(?:should|can|do|would)\s+i\s+)?(?:run|test|deploy|connect|access)\b|"
    r"\b(?:which|what)\s+(?:address|url|host|server|machine|network)\b",
    re.I,
)


def is_server_question(text: str) -> bool:
    """A question a developer AI puts to the owner about where or how something runs. It is never a business decision."""
    return bool(find_server_wording(text) or SERVER_QUESTION.search(text or ""))


def redact(text: str) -> str:
    """The text with every server wording replaced by a fixed marker (for what is kept in a Jira comment)."""
    spans = [(start, end) for start, end, _kind in _wording_spans(text)]
    spans.sort()
    merged: list[list[int]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    out, pos = [], 0
    for start, end in merged:
        out.append((text or "")[pos:start])
        out.append(REPLACEMENT)
        pos = end
    out.append((text or "")[pos:])
    return "".join(out)


def kinds_of(findings: list[dict[str, str]]) -> list[str]:
    return list(dict.fromkeys(f["kind"] for f in findings))


def rejection_message(findings: list[dict[str, str]]) -> str:
    """What the author is told. It names the kinds and the quoted words so they can be found."""
    quoted = "; ".join(f"{f['kind']}: \"{f['text']}\"" for f in findings[:8])
    return (f"the text names {', '.join(kinds_of(findings))} ({quoted}). A ticket states what the product must do and"
            " how to know it is done; describe the requirement instead. Which computer, server, network, folder,"
            " account or tool to use belongs to the project configuration (environment section) and the run"
            " environment, not to the ticket")
