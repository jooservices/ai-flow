"""Security scan helpers for CI and releases (docs/v4/security.md).

`deploy/v4/ci/security-scan.sh` scans dependencies, the repository and the container image with Trivy on every pull
request and every release. The blocking level is a setting (`SECURITY_SEVERITY_THRESHOLD`, a repository variable), not a
constant: a finding at or above it fails the check. This module turns the level into the severity list Trivy takes and
turns the scan results into the per-release security report (`SECURITY-REPORT.md`) that lists every check and its result.

    python -m aiflow.v4.core.security_report severities HIGH
    python -m aiflow.v4.core.security_report report --version v4.1.0 --revision <sha> --threshold HIGH \
        --check "Dependencies (uv.lock)=out/dependencies.json" --check "Container image=out/image.json" \
        --note "Unit and integration tests=passed"
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ORDER = ["UNKNOWN", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
DISCLOSURE = ".github/SECURITY.md"


def normalize(level: str) -> str:
    level = (level or "").strip().upper()
    if level not in ORDER:
        raise ValueError(f"unknown severity level {level!r}; choose one of {', '.join(ORDER)}")
    return level


def severities_at_or_above(threshold: str) -> list[str]:
    """['HIGH', 'CRITICAL'] for HIGH: the severities that block."""
    return ORDER[ORDER.index(normalize(threshold)):]


def count_findings(result: dict) -> dict[str, int]:
    """Findings per severity in one Trivy JSON result (vulnerabilities, secrets and misconfigurations)."""
    counts = {level: 0 for level in ORDER}
    for target in result.get("Results") or []:
        for key in ("Vulnerabilities", "Secrets", "Misconfigurations"):
            for finding in target.get(key) or []:
                level = str(finding.get("Severity") or "UNKNOWN").upper()
                counts[level if level in counts else "UNKNOWN"] += 1
    return counts


def blocking(counts: dict[str, int], threshold: str) -> int:
    return sum(counts[level] for level in severities_at_or_above(threshold))


def build_report(version: str, revision: str, threshold: str, checks: list[tuple[str, dict | None]],
                 notes: list[tuple[str, str]] = ()) -> str:
    """Markdown report: the blocking level, one row per check with its result, and the disclosure policy."""
    lines = [f"# Security report for {version}", "",
             f"- Revision: `{revision or 'unknown'}`",
             f"- Blocking severity: **{normalize(threshold)}** and above (setting `SECURITY_SEVERITY_THRESHOLD`)",
             f"- Coordinated disclosure policy: [{DISCLOSURE}](https://github.com/jooservices/ai-flow/blob/master/{DISCLOSURE})",
             "", "| Check | Result | " + " | ".join(ORDER[::-1]) + " |", "|---|---|" + "---|" * len(ORDER)]
    failed = False
    for name, result in checks:
        if result is None:
            lines.append(f"| {name} | NOT RUN | " + " | ".join("-" for _ in ORDER) + " |")
            failed = True
            continue
        counts = count_findings(result)
        bad = blocking(counts, threshold)
        failed = failed or bool(bad)
        lines.append(f"| {name} | {'FAIL' if bad else 'PASS'} | " + " | ".join(str(counts[level]) for level in ORDER[::-1]) + " |")
    for name, outcome in notes:
        ok = outcome.strip().lower() in {"passed", "pass", "success"}
        failed = failed or not ok
        lines.append(f"| {name} | {'PASS' if ok else 'FAIL'} | " + " | ".join("-" for _ in ORDER) + " |")
    lines += ["", f"**Overall: {'FAIL' if failed else 'PASS'}**", ""]
    return "\n".join(lines)


def blocking_findings(result: dict, threshold: str) -> list[str]:
    """One line per finding at or above the level (id, package, installed and fixed version, target), for the CI log."""
    levels = set(severities_at_or_above(threshold))
    lines = []
    for target in result.get("Results") or []:
        for key in ("Vulnerabilities", "Secrets", "Misconfigurations"):
            for finding in target.get(key) or []:
                level = str(finding.get("Severity") or "UNKNOWN").upper()
                if level not in levels:
                    continue
                ident = finding.get("VulnerabilityID") or finding.get("ID") or finding.get("RuleID") or "?"
                package = finding.get("PkgName") or finding.get("Title") or ""
                lines.append(f"{level} {ident} {package} installed={finding.get('InstalledVersion') or '-'} "
                             f"fixed={finding.get('FixedVersion') or 'none'} in {target.get('Target', '?')}")
    return sorted(set(lines))


def _load(path: str) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="security_report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sev = sub.add_parser("severities")
    sev.add_argument("threshold")
    fnd = sub.add_parser("findings")
    fnd.add_argument("threshold")
    fnd.add_argument("result", help="trivy-result.json")
    rep = sub.add_parser("report")
    rep.add_argument("--version", required=True)
    rep.add_argument("--revision", default="")
    rep.add_argument("--threshold", default="HIGH")
    rep.add_argument("--check", action="append", default=[], help="NAME=trivy-result.json")
    rep.add_argument("--note", action="append", default=[], help="NAME=passed|failed (a check run elsewhere)")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "severities":
            print(",".join(severities_at_or_above(args.threshold)))
            return 0
        if args.cmd == "findings":
            result = _load(args.result)
            if result is None:
                print(f"{args.result}: no scan result", file=sys.stderr)
                return 0
            print("\n".join(blocking_findings(result, args.threshold)))
            return 0
        checks = [(name, _load(path)) for name, _, path in (c.partition("=") for c in args.check)]
        notes = [(name, outcome) for name, _, outcome in (n.partition("=") for n in args.note)]
        print(build_report(args.version, args.revision, args.threshold, checks, notes))
        return 0
    except ValueError as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
