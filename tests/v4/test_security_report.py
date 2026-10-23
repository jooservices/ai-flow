"""Security scans and the release security report (JAIF-231 AC5, AC6)."""
import json
from pathlib import Path

import pytest

from aiflow.v4.core import security_report as sr

ROOT = Path(__file__).parents[2]


def result(*severities):
    return {"Results": [{"Vulnerabilities": [{"Severity": s} for s in severities]}]}


def test_the_blocking_level_is_a_setting_that_selects_the_severities():
    assert sr.severities_at_or_above("high") == ["HIGH", "CRITICAL"]
    assert sr.severities_at_or_above("LOW") == ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    assert sr.severities_at_or_above("CRITICAL") == ["CRITICAL"]
    with pytest.raises(ValueError):
        sr.severities_at_or_above("severe")
    assert sr.main(["severities", "MEDIUM"]) == 0 and sr.main(["severities", "nope"]) == 2


def test_a_finding_above_the_level_fails_and_below_it_passes():
    counts = sr.count_findings(result("MEDIUM", "LOW", "HIGH"))
    assert counts["HIGH"] == 1 and counts["MEDIUM"] == 1
    assert sr.blocking(counts, "HIGH") == 1 and sr.blocking(counts, "CRITICAL") == 0 and sr.blocking(counts, "LOW") == 3
    secrets_and_config = {"Results": [{"Secrets": [{"Severity": "CRITICAL"}], "Misconfigurations": [{"Severity": "LOW"}]}]}
    assert sr.blocking(sr.count_findings(secrets_and_config), "HIGH") == 1


def test_the_release_report_lists_every_check_and_its_result():
    report = sr.build_report("v4.2.0", "abc123", "HIGH", [("Dependencies", result("LOW")), ("Container image", result("CRITICAL")),
                                                          ("Repository", None)], [("Tests", "passed")])
    assert "# Security report for v4.2.0" in report and "`abc123`" in report and "**HIGH** and above" in report
    assert "| Dependencies | PASS |" in report and "| Container image | FAIL |" in report
    assert "| Repository | NOT RUN |" in report and "| Tests | PASS |" in report and "**Overall: FAIL**" in report
    assert ".github/SECURITY.md" in report
    clean = sr.build_report("v4.2.0", "", "HIGH", [("Dependencies", result("LOW"))])
    assert "**Overall: PASS**" in clean


def test_report_cli_reads_trivy_json(tmp_path, capsys):
    good = tmp_path / "deps.json"
    good.write_text(json.dumps(result("HIGH")))
    assert sr.main(["report", "--version", "v1.0.0", "--threshold", "CRITICAL", "--check", f"Deps={good}",
                    "--check", f"Missing={tmp_path / 'none.json'}"]) == 0
    out = capsys.readouterr().out
    assert "| Deps | PASS |" in out and "| Missing | NOT RUN |" in out


def test_pull_request_ci_scans_with_a_configurable_threshold():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "pull_request:" in ci and "security-scan.sh" in ci
    assert "vars.SECURITY_SEVERITY_THRESHOLD" in ci
    script = (ROOT / "deploy/v4/ci/security-scan.sh").read_text()
    assert "--exit-code 1" in script and "SECURITY_SEVERITY_THRESHOLD" in script
    assert all(scan in script for scan in ("uv.lock", "secret,misconfig", "image"))
    # only the image scan ignores findings that have no fix; the dependency scan does not
    assert script.count("--ignore-unfixed --exit-code") == 1
    assert script.index("--ignore-unfixed --exit-code") > script.index("container image $IMAGE")


def test_release_workflow_generates_and_uploads_the_security_report():
    release = (ROOT / ".github/workflows/release-v4.yml").read_text()
    assert "security_report report" in release and "SECURITY-REPORT.md" in release
    assert release.index("security_report report") < release.index("gh release upload")
    build = release.index("docker build")
    scan = release.index("security-scan.sh dist/security-report")
    push = release.index('docker push "$IMAGE"')
    sign = release.index('cosign sign --yes "$IMAGE"')
    assert build < scan < push < sign < release.index("gh release upload")


def test_the_disclosure_policy_is_published():
    policy = (ROOT / ".github/SECURITY.md").read_text()
    assert "advisories/new" in policy and "coordinated disclosure" in policy.lower()
    readme = (ROOT / "deploy/v4/README.md").read_text()
    assert "docs/v4/security.md" in readme


def test_blocking_findings_are_listed_for_the_ci_log(tmp_path, capsys):
    scan = {"Results": [{"Target": "debian", "Vulnerabilities": [
        {"Severity": "HIGH", "VulnerabilityID": "CVE-1", "PkgName": "libx", "InstalledVersion": "1"},
        {"Severity": "LOW", "VulnerabilityID": "CVE-2", "PkgName": "liby"}]}]}
    assert sr.blocking_findings(scan, "HIGH") == ["HIGH CVE-1 libx installed=1 fixed=none in debian"]
    path = tmp_path / "image.json"
    path.write_text(json.dumps(scan))
    assert sr.main(["findings", "HIGH", str(path)]) == 0
    assert "CVE-1" in capsys.readouterr().out
    assert sr.main(["findings", "HIGH", str(tmp_path / "none.json")]) == 0
