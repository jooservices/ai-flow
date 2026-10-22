"""JAIF-234: the documentation site, the runbooks that follow the incident kinds in code, the console Help links
and the CI check that keeps documentation current."""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest

from aiflow.v4.core import docsite

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "guide"


@pytest.fixture(scope="module")
def site(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("site")
    docsite.build(out, "v9.9.9")
    return out


def test_runbooks_match_incident_kinds_raised_in_code():
    kinds = docsite.incident_kinds()
    # the kinds named by the ticket are really found
    assert {"ticket_stuck", "config_pin", "flow_idle", "jira_auth", "openobserve_delivery"} <= kinds
    pages = {p.stem for p in (GUIDE / "runbooks").glob("*.md")} - {"index"}
    assert pages == kinds, f"missing: {sorted(kinds - pages)}; unknown: {sorted(pages - kinds)}"
    index = (GUIDE / "runbooks" / "index.md").read_text()
    for kind in kinds:
        assert f"[{kind}]({kind}.md)" in index


def test_every_runbook_has_symptom_cause_and_fix():
    for kind in docsite.incident_kinds():
        text = (GUIDE / "runbooks" / f"{kind}.md").read_text()
        for part in ("## Symptom", "## Cause", "## Fix"):
            assert part in text, f"{kind} lacks {part}"


def test_every_console_screen_has_help_link_hint_and_target_page(site):
    page = (ROOT / "aiflow/v4/control/ui/console.html").read_text()
    views = docsite.console_views()
    assert len(views) >= 10
    assert 'id="help-link"' in page and "helpUrl(S.view)" in page and "HELP_BASE = '/help/screens/'" in page
    hints = re.search(r"var HINTS = \{(.*?)\n\};", page, re.S).group(1)
    for view in views:
        assert re.search(rf"\b{view}: '", hints), f"no first-use hint for {view}"
        assert (GUIDE / "screens" / f"{view}.md").is_file()
        assert (site / "screens" / f"{view}.html").is_file()


def test_help_route_serves_the_screen_pages(monkeypatch, site):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from aiflow.v4.control.ui import mount_ui

    monkeypatch.setenv(docsite.SITE_ENV, str(site))
    app = FastAPI()
    mount_ui(app, None)
    client = TestClient(app)
    for view in docsite.console_views():
        r = client.get(f"/help/screens/{view}.html")
        assert r.status_code == 200 and "AI Flow v9.9.9 documentation" in r.text
    assert client.get("/help/").status_code == 200
    assert client.get("/help/../../etc/passwd").status_code == 404
    assert client.get("/help/%2e%2e/%2e%2e/etc/passwd").status_code == 404


def test_every_page_states_what_it_assumes_and_roles_have_a_guide():
    pages = list(GUIDE.rglob("*.md"))
    assert pages
    for p in pages:
        head = "\n".join(p.read_text().splitlines()[:6])
        assert "**Assumes:**" in head, f"{p.relative_to(ROOT)} does not state its assumptions"
    for role in docsite.role_names():
        assert (GUIDE / "user" / f"{role}.md").is_file()
    for name in ("install", "upgrade", "backup", "security"):
        assert (GUIDE / "admin" / f"{name}.md").is_file()


def test_docs_contain_no_secrets():
    for p in GUIDE.rglob("*.md"):
        assert not re.search(r"(?i)(ghp_|xox[bp]-|sk-[a-z0-9]{10}|BEGIN [A-Z ]*PRIVATE KEY)", p.read_text())


def test_site_builds_with_api_reference_generated_from_code(site):
    api = (site / "reference" / "api.html").read_text()
    assert "/v1/state" in api and "generated from the code" in api
    assert not (GUIDE / "reference").exists()  # never written by hand
    assert (site / "index.html").is_file() and (site / "runbooks" / "ticket_stuck.html").is_file()
    assert 'href="../runbooks/upgrade_blocked.html"' in (site / "admin" / "upgrade.html").read_text()


def test_build_fails_on_a_broken_link(tmp_path):
    guide = tmp_path / "guide"
    guide.mkdir()
    (guide / "index.md").write_text("# Home\n\n**Assumes:** x\n\n[gone](missing.md)\n")
    with pytest.raises(ValueError, match="broken link"):
        docsite.build(tmp_path / "out", guide=guide, with_api=False)


def test_ci_check_fails_without_documentation_and_passes_with_it():
    ui = "aiflow/v4/control/ui/console.html"
    api = "aiflow/v4/control/api.py"
    routes = {api: {"/v1/knowledge/{kind}", "/v1/cc"}, "aiflow/v4/control/login.py": {"/v1/auth/login"}}
    texts = {"docs/v4/knowledge.md": "Lists items with `GET /v1/knowledge/{kind}`.",
             "docs/v4/other.md": "Nothing about routes.",
             "docs/guide/admin/security.md": "Sign in with POST /v1/auth/login."}

    def gaps(*files, **kw):
        return docsite.documentation_gaps(list(files), read=lambda f: texts.get(f, ""), routes=routes, **kw)

    assert gaps(ui)
    assert gaps(api)
    assert gaps(api, "tests/v4/test_api.py")
    assert gaps(ui, "docs/guide/screens/cc.md") == []
    # A knowledge page cannot cover a change to a different API route in the same module.
    assert gaps(api, "docs/v4/knowledge.md")
    assert gaps("aiflow/v4/worker/dev.py") == []
    assert gaps(ui, "docs/RUNBOOK.md")  # the old flow's notes do not count
    # unrelated documentation does not satisfy a screen or API change
    assert gaps(ui, "docs/v4/knowledge.md")
    assert gaps(api, "docs/v4/other.md")
    assert gaps(api, "docs/guide/screens/cc.md")
    assert gaps(ui, "docs/guide/screens/cc.md", views={"cc", "sa"})  # sa page missing
    assert gaps(ui, "docs/guide/screens/cc.md", views={"cc"}) == []
    # every module that registers routes is watched, not only api.py
    login = "aiflow/v4/control/login.py"
    assert gaps(login, "docs/v4/knowledge.md")
    assert gaps(login, "docs/guide/admin/security.md") == []


def test_api_change_requires_documentation_for_the_changed_route_not_another_route():
    api = "aiflow/v4/control/api.py"
    routes = {api: {"/v1/knowledge/{kind}", "/v1/cc"}}
    changed_routes = {api: {"GET /v1/cc"}}
    texts = {
        "docs/v4/knowledge.md": "Lists items with `GET /v1/knowledge/{kind}`.",
        "docs/v4/cc.md": "Returns the command center snapshot with `GET /v1/cc`.",
    }

    def gaps(*files):
        return docsite.documentation_gaps(list(files), read=lambda f: texts.get(f, ""), routes=routes,
                                          changed_routes=changed_routes)

    assert gaps(api, "docs/v4/knowledge.md")
    assert gaps(api, "docs/v4/cc.md") == []


def test_ci_check_maps_changed_api_lines_to_their_route_contract(monkeypatch):
    api = "aiflow/v4/control/api.py"
    monkeypatch.setattr(docsite, "api_route_contract_spans", lambda: {
        api: {"GET /v1/cc": [(10, 15)], "GET /v1/knowledge/{kind}": [(20, 25)]}
    })

    class Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if command[1] == "diff":
            return Result("@@ -12,1 +12,1 @@\n")
        return Result("")

    monkeypatch.setattr(docsite.subprocess, "run", fake_run)
    assert docsite.changed_api_routes("origin/develop", [api]) == {api: {"GET /v1/cc"}}


def test_changed_master_action_route_is_named_in_documentation():
    api = "aiflow/v4/control/api.py"
    docs = "docs/v4/master.md"
    routes = docsite.api_routes_by_module()
    assert "/v1/master/actions" in routes[api]
    assert "POST /v1/master/actions" in (ROOT / docs).read_text()
    assert docsite.documentation_gaps([api, docs], routes=routes,
                                      changed_routes={api: {"POST /v1/master/actions"}}) == []


def test_route_modules_are_derived_from_the_registered_routes():
    modules = docsite.api_routes_by_module()
    assert {"aiflow/v4/control/api.py", "aiflow/v4/control/login.py",
            "aiflow/v4/control/jira_connect.py"} <= set(modules)


def test_every_console_renderer_has_a_documentation_mapping():
    functions = docsite._function_spans((ROOT / "aiflow/v4/control/ui/console.html").read_text().splitlines())
    renderers = {name for name in functions if name.startswith("render")}
    assert renderers <= set(docsite.SCREEN_RENDERER_VIEWS)
    views = set(docsite.console_views())
    assert all(mapped is None or set(mapped) <= views for mapped in docsite.SCREEN_RENDERER_VIEWS.values())


def test_ci_check_maps_shared_help_changes_and_screen_hint_edits(monkeypatch):
    ui = "aiflow/v4/control/ui/console.html"
    source_lines = (ROOT / ui).read_text().splitlines()
    shared_lines = [
        next(i for i, line in enumerate(source_lines, 1) if line.startswith(".firstuse{")),
        next(i for i, line in enumerate(source_lines, 1) if 'id="help-link"' in line),
        next(i for i, line in enumerate(source_lines, 1) if 'id="firstuse"' in line),
        next(i for i, line in enumerate(source_lines, 1) if line.startswith("var HELP_BASE =")),
        next(i for i, line in enumerate(source_lines, 1) if "short hint for first use" in line),
        next(i for i, line in enumerate(source_lines, 1) if line.startswith("function renderHelp()")),
        next(i for i, line in enumerate(source_lines, 1) if "hint-dismiss" in line),
    ]
    hint_line = next(i for i, line in enumerate(source_lines, 1) if line.startswith("  cc: '"))

    class Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    current_lines = shared_lines + [hint_line]

    def fake_run(command, **kwargs):
        if "show" in command:
            return Result("\n".join(source_lines) + "\n")
        hunks = current_lines if "--name-only" not in command else []
        return Result("".join(f"@@ -{line},1 +{line},1 @@\n" for line in hunks))

    monkeypatch.setattr(docsite.subprocess, "run", fake_run)
    views = docsite.changed_views("origin/develop")
    assert views == set(docsite.console_views())
    all_pages = ["aiflow/v4/control/ui/console.html", *(f"docs/guide/screens/{v}.md" for v in views)]
    assert docsite.documentation_gaps(all_pages, views=views) == []
    assert docsite.documentation_gaps([ui, "docs/guide/screens/cc.md"], views=views)

    current_lines = [hint_line]
    assert docsite.changed_views("origin/develop") == {"cc"}


def test_traefik_routes_console_help_to_control_api():
    compose = (ROOT / "deploy/v4/control/compose.yaml").read_text()
    api_rule = next(line for line in compose.splitlines() if "traefik.http.routers.api.rule:" in line)
    assert "PathPrefix(`/help`)" in api_rule


def test_ci_check_maps_javascript_renderer_to_its_screen_page(monkeypatch, capsys):
    ui = "aiflow/v4/control/ui/console.html"
    source_lines = (ROOT / ui).read_text().splitlines()
    renderer_line = next(i for i, line in enumerate(source_lines, 1) if line.startswith("function renderProjects("))
    docs = "docs/v4/master.md"
    calls = {"docs": [docs]}

    class Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if "--name-only" in command:
            return Result("\n".join([ui, *calls["docs"]]) + "\n")
        if "show" in command:
            return Result("\n".join(source_lines) + "\n")
        return Result(f"@@ -{renderer_line},1 +{renderer_line},1 @@\n")

    monkeypatch.setattr(docsite.subprocess, "run", fake_run)
    assert docsite.main(["check", "--base", "origin/develop"]) == 1
    assert "docs/guide/screens/projects.md" in capsys.readouterr().err

    calls["docs"] = ["docs/guide/screens/projects.md"]
    assert docsite.main(["check", "--base", "origin/develop"]) == 0
    assert "documentation check passed" in capsys.readouterr().out


def test_ci_check_fails_closed_when_ui_hunk_cannot_be_mapped(monkeypatch, capsys):
    ui = "aiflow/v4/control/ui/console.html"
    source_lines = (ROOT / ui).read_text().splitlines()
    global_line = next(i for i, line in enumerate(source_lines, 1) if line.startswith("var S ="))

    class Result:
        returncode = 0

        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if "--name-only" in command:
            return Result(ui + "\ndocs/guide/screens/projects.md\n")
        if "show" in command:
            return Result("\n".join(source_lines) + "\n")
        return Result(f"@@ -{global_line},1 +{global_line},1 @@\n")

    monkeypatch.setattr(docsite.subprocess, "run", fake_run)
    assert docsite.main(["check", "--base", "origin/develop"]) == 1
    assert "could not be mapped to a screen" in capsys.readouterr().err


def test_master_cannot_open_incidents_directly(client, ctx):
    from test_master import action, claim_master, ready
    cc = ready(client, "cc", "claude-01")
    master = claim_master(client, cc, "claude-01").json()
    denied = action(client, cc, master, action="incident_open", intent="record a problem", kind="node_down",
                    subject="x", severity="warning", title="t", detail="d")
    assert denied.status_code == 403 and "not allowed" in denied.text
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='node_down'")["n"] == 0


def test_ci_check_command_exit_codes():
    def run(*files):
        return subprocess.run([sys.executable, "-m", "aiflow.v4.core.docsite", "check", "--files", *files],
                              cwd=ROOT, capture_output=True, text=True)
    bad = run("aiflow/v4/control/ui/console.html")
    assert bad.returncode == 1 and "documentation check failed" in bad.stderr
    assert run("aiflow/v4/control/ui/console.html", "docs/guide/screens/cc.md").returncode == 0


def test_ci_workflow_runs_the_check():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    assert "aiflow.v4.core.docsite check" in ci and "aiflow.v4.core.docsite build" in ci


def test_release_artifact_includes_the_site(tmp_path):
    env = dict(os.environ, PYTHON=sys.executable)
    tarball = tmp_path / "aiflow4-v9.9.9.tar.gz"
    subprocess.run(["git", "archive", "--format=tar.gz", f"--output={tarball}", "HEAD",
                    "aiflow", "pyproject.toml", "uv.lock", "prompts", "deploy"], cwd=ROOT, check=True)
    subprocess.run(["bash", str(ROOT / "deploy/v4/ci/add-docs-site.sh"), "v9.9.9", str(tarball)],
                   cwd=ROOT, env=env, check=True, capture_output=True)
    with tarfile.open(tarball) as tar:
        names = tar.getnames()
    assert "docs-site/index.html" in names and "docs-site/runbooks/ticket_stuck.html" in names
    assert "docs-site/reference/api.html" in names
    assert any(n.startswith("aiflow/v4/") for n in names) and "deploy/v4/README.md" in names
    release = (ROOT / ".github/workflows/release-v4.yml").read_text()
    assert release.index("add-docs-site.sh") < release.index("sha256sum")


def test_topic_is_linked_from_deploy_readme():
    readme = (ROOT / "deploy/v4/README.md").read_text().split("## Topics", 1)[1].split("\n## ", 1)[0]
    assert "(../../docs/v4/documentation-site.md)" in readme
