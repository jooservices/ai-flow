"""Audit of the control API routes against the company HTTP standard (JAIF-414).

The standard is ``.claude/skills/http-api-design/references/standard.md`` of the Workspaces repository: resource-oriented
paths with plural nouns, no verbs in ordinary paths (an explicit operation endpoint ``POST /{collection}/{id}/{operation}``
is allowed when it is documented), safe GET, one success envelope, RFC 9457 problem+json errors, a version prefix and an
OpenAPI contract. This module lists every route with its findings so the migration to ``/v2`` can be done in small tickets.
The result is committed as ``docs/v4/api-rest-audit.md`` and a test fails when a route is added without an audit entry.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_VERB_TEXT = """
accept acknowledge activate add answer apply approve attach cancel check claim close complete connect create deactivate decide
delete detach disable disconnect download enable escalate execute export finish generate heartbeat import install join leave
login logout merge pause probe raise reject reload release remove reopen reset restart restore resume retry revoke rollback
rotate run scan send start stop submit sweep sync test transition trigger unlock update upgrade validate verify
"""
VERBS = frozenset(_VERB_TEXT.split())
PARAM = re.compile(r"\{[^}]+\}")
EXEMPT = ("/healthz",)
OPERATION_OK = "operation endpoint (allowed by the standard when documented)"


@dataclass
class Row:
    method: str
    path: str
    findings: list[str] = field(default_factory=list)
    clients: bool = False
    proposal: str = ""


def flatten(routes: Iterable) -> Iterable[tuple[str, str]]:
    """(method, path) of every concrete route, looking inside routers included through the router registry."""
    for route in routes:
        if hasattr(route, "methods"):
            for method in sorted(route.methods or ()):
                if method not in ("HEAD", "OPTIONS"):
                    yield method, route.path
        else:
            prefix = getattr(getattr(route, "include_context", None), "prefix", "") or ""
            for method, path in flatten(route.original_router.routes):
                yield method, prefix + path


def segments(path: str) -> list[str]:
    return [s for s in path.split("/") if s]


def findings_for(method: str, path: str) -> list[str]:
    if path in EXEMPT:
        return []
    segs = segments(path)
    found: list[str] = []
    static = [s for s in segs if not PARAM.fullmatch(s)]
    verbs = [s for s in static if s in VERBS or s.split("-")[0] in VERBS]
    for verb in verbs:
        is_last = segs[-1] == verb
        after_param = len(segs) >= 2 and segs[-2] != verb and PARAM.fullmatch(segs[-2]) is not None
        if method == "POST" and is_last and after_param:
            found.append(OPERATION_OK)
        elif method == "GET":
            found.append(f"unsafe or verb GET: `{verb}` in the path")
        else:
            found.append(f"verb in path: `{verb}`")
    if not path.startswith("/v1/"):
        found.append("no version prefix")
    if re.search(r"\.[a-z0-9]{2,5}($|/)", path):
        found.append("file extension in the path (use content negotiation or a download resource)")
    for before, after in zip(segs, segs[1:], strict=False):
        if PARAM.fullmatch(after) and not PARAM.fullmatch(before) and before != "v1" and not before.endswith("s"):
            found.append(f"collection `{before}` is not plural")
    return list(dict.fromkeys(found))


def is_violation(finding: str) -> bool:
    return finding != OPERATION_OK


def proposal_for(method: str, path: str, findings: list[str]) -> str:
    if not any(is_violation(f) for f in findings):
        return "keep (v2 only adds the envelope and problem+json)"
    v2 = path.replace("/v1/", "/v2/", 1)
    segs = segments(path)
    if method == "POST" and len(segs) == 3 and not PARAM.fullmatch(segs[-1]):
        return f"POST /v2/{segs[1]} creates the resource (`{segs[-1]}` becomes the creation or a documented operation)"
    if method == "GET":
        return f"GET {v2.rsplit('/', 1)[0]}/{{...}} as a read model; if it has effects, a documented POST operation"
    return f"review: resource-oriented form of `{method} {v2}`"


def client_pattern(path: str) -> re.Pattern[str]:
    return re.compile('"' + PARAM.sub(r'[^/"\\s]+', re.escape(path).replace(r"\{", "{").replace(r"\}", "}")))


def used_by_clients(path: str, client_sources: str) -> bool:
    pattern = re.escape(path)
    pattern = re.sub(r"\\\{[^}]*\\\}", r'[^/"\\s]+', pattern)
    return re.search('"' + pattern, client_sources) is not None


def audit(routes: Iterable, root: Path) -> list[Row]:
    sources = "\n".join(p.read_text(encoding="utf-8") for sub in ("worker", "node")
                        for p in sorted((root / "aiflow" / "v4" / sub).rglob("*.py")))
    rows = []
    for method, path in sorted(set(flatten(routes)), key=lambda x: (x[1], x[0])):
        found = findings_for(method, path)
        rows.append(Row(method, path, found, used_by_clients(path, sources), proposal_for(method, path, found)))
    return rows


GLOBAL = """\
## Findings that apply to every route

| Finding | Standard | Today |
| --- | --- | --- |
| G1 Success envelope | `{success, code, message, data, meta, errors, trace_id}` | Routes return plain objects. |
| G2 Error format | `application/problem+json` (RFC 9457) | Errors use FastAPI's `{"detail": ...}` JSON. |
| G3 Machine-readable contract | OpenAPI kept complete by a test | The app serves a generated schema; no completeness test. |
| G4 Versioning | breaking changes in a new major version, `/v1` kept until clients move | One version, `/v1`. |

The facts in G1 and G2 are asserted by `tests/v4/test_rest_audit.py`, so this table cannot go stale unnoticed.
"""


def render(rows: list[Row]) -> str:
    violations = [r for r in rows if any(is_violation(f) for f in r.findings)]
    operations = [r for r in rows if OPERATION_OK in r.findings]
    clients = [r for r in violations if r.clients]
    lines = [
        "# Control API audit against the company HTTP standard",
        "",
        "Generated by `control/rest_audit.py` (`AIFLOW_WRITE_AUDIT=1 uv run pytest tests/v4/test_rest_audit.py` rewrites it);",
        "the standard is `http-api-design` (resource-oriented paths, plural nouns, no verbs in ordinary paths, one envelope,",
        "RFC 9457 errors, versioning, OpenAPI). The proposals are starting points for the small migration tickets of JAIF-413.",
        "",
        f"- Routes: **{len(rows)}**. With a finding to fix: **{len(violations)}**. Documented operation endpoints: "
        f"**{len(operations)}**. Fixing a route that a worker or node calls needs a coordinated cutover: **{len(clients)}**.",
        "",
        GLOBAL,
        "## Routes",
        "",
        "| Method | Path | Findings | Worker or node calls it | Proposal for `/v2` |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in rows:
        lines.append(f"| {r.method} | `{r.path}` | {'; '.join(r.findings) or '-'} | {'yes' if r.clients else 'no'} | {r.proposal} |")
    return "\n".join(lines) + "\n"
