from aiflow.v4.core.controlrepo import ControlRepo, Project
from aiflow.v4.core.toolchains import (BASE_TOOLS, CATALOG_TOOLS, NODE_TOOLS, STACK_TOOLS, missing_tools, tools_for_providers,
                                       pinned_version, toolchain_gaps, tools_for_stack, tools_for_stacks,
                                       version_matches)
from conftest import join_node


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def reported_tools(ctx, node):
    row = ctx.db.one("SELECT tools FROM nodes WHERE name=%s", (node,))
    return dict(row["tools"] or {})


def test_stack_values_map_to_node_and_project_tools():
    assert STACK_TOOLS["php-8.3"] == ("php", "composer", "wp")
    assert STACK_TOOLS["python-3.12"] == ("python3", "uv")
    assert tools_for_stack("php-8.3") == list(BASE_TOOLS) + ["php", "composer", "wp"]
    assert "python3" in tools_for_stack("python-3.12")
    assert tools_for_stack("unknown") == list(BASE_TOOLS)
    assert tools_for_stacks(["php-8.3", "python-3.12"]) == list(NODE_TOOLS) + list(CATALOG_TOOLS)
    assert tools_for_stack("node-24") == list(BASE_TOOLS) + ["node"]
    assert tools_for_stack("playwright") == list(BASE_TOOLS) + ["node", "playwright", "chromium", "firefox", "webkit"]
    assert tools_for_stack("wordpress") == list(BASE_TOOLS) + ["safe-container-runtime", "node", "playwright", "chromium", "firefox", "webkit"]
    # AI CLIs are required by the providers of a node's accounts, not by project stacks
    assert tools_for_providers(["codex", "claude"]) == ["codex", "codex-code-mode-host", "claude"]


def test_project_tools_follow_the_stack_and_missing_is_explicit():
    project = Project(key="TESTWP", repo="jooservices/test-aiflow-wp", stack="php-8.3")
    assert project.tools == list(BASE_TOOLS) + ["php", "composer", "wp"]
    assert missing_tools({"php": "PHP 8.3.0", "composer": "missing"}, project.tools) == ["git", "uv", "composer", "wp"]


def test_wordpress_stack_uses_the_safe_container_runtime_and_requires_every_browser_engine():
    required = tools_for_stack("wordpress")
    versions = {"tools": {"node": "24.x", "playwright": "1.56"}}
    healthy = {"git": "git version 2.43.0", "uv": "uv 0.13.4", "safe-container-runtime": "safe-container-runtime 1",
               "node": "node v24.22.0", "playwright": "Version 1.56.1",
               "chromium": "Chromium Playwright 1.56.1", "firefox": "Firefox Playwright 1.56.1",
               "webkit": "WebKit Playwright 1.56.1"}
    assert "podman" not in required
    assert set(required) == {"git", "uv", "safe-container-runtime", "node", "playwright", "chromium", "firefox", "webkit"}
    assert toolchain_gaps(healthy, required, versions, ["wordpress"], require_pins=True) == []
    healthy["webkit"] = "missing"
    assert toolchain_gaps(healthy, required, versions, ["wordpress"], require_pins=True) == ["webkit"]


def test_null_repository_containers_inherits_the_project_setting():
    project = Project(key="TESTWP", repo="jooservices/test-aiflow-wp", containers=True,
                      repositories=[{"repo": "jooservices/other", "containers": None},
                                    {"repo": "jooservices/legacy", "containers": False}])
    assert project.settings_for_repo("jooservices/other")["containers"] is True
    assert project.settings_for_repo("jooservices/legacy")["containers"] is False
    off = Project(key="X", repo="a/b", containers=False, repositories=[{"repo": "a/c", "containers": None}])
    assert off.settings_for_repo("a/c")["containers"] is False


def test_container_declaration_adds_the_safe_runtime_and_can_be_overridden_per_repository():
    project = Project(key="TESTWP", repo="jooservices/test-aiflow-wp", containers=True,
                      repositories=[{"repo": "jooservices/legacy", "containers": False}])
    assert "safe-container-runtime" in project.tools
    assert project.settings_for_repo("jooservices/test-aiflow-wp")["containers"] is True
    assert project.settings_for_repo("jooservices/legacy")["containers"] is False
    assert project.uses_containers is True


def test_project_container_yaml_roundtrips_to_required_node_tool(control_repo):
    (control_repo / "projects" / "TESTWP.yaml").write_text(
        "key: TESTWP\nrepo: jooservices/test-aiflow-wp\ncontainers: true\n")
    project = ControlRepo.load(control_repo).projects["TESTWP"]
    assert project.containers is True
    assert "safe-container-runtime" in project.tools
    assert missing_tools({"git": "git 2.43", "uv": "uv 0.8"}, project.tools) == ["safe-container-runtime"]


def test_control_repo_reads_the_project_stack_and_node_tool_union(control_repo):
    (control_repo / "projects" / "TESTWP.yaml").write_text(
        "key: TESTWP\nrepo: jooservices/test-aiflow-wp\nstack: php-8.3\n")
    repo = ControlRepo.load(control_repo)
    assert repo.projects["TESTWP"].stack == "php-8.3"
    assert repo.projects["TESTWP"].tools == list(BASE_TOOLS) + ["php", "composer", "wp"]
    assert repo.tools_for_node("cc") == list(BASE_TOOLS) + ["claude", "opencode", "php", "composer", "wp"]


def test_project_yaml_can_declare_multiple_stacks_and_versions_lock_pins_each_tool(control_repo):
    (control_repo / "projects" / "TESTWP.yaml").write_text(
        "key: TESTWP\nrepo: jooservices/test-aiflow-wp\nstack: [php-8.3, node-24, playwright]\n")
    (control_repo / "versions.lock").write_text(
        "schema: 1\ntools:\n  php: 8.3\n  composer: 2.8\n  wp: 2.12\n  node: 24.x\n  playwright: 1.56\n")
    repo = ControlRepo.load(control_repo)
    project = repo.projects["TESTWP"]
    assert project.stacks == ["php-8.3", "node-24", "playwright"]
    assert project.tools == list(BASE_TOOLS) + ["php", "composer", "wp", "node", "playwright", "chromium", "firefox", "webkit"]
    assert [pinned_version(repo.versions, tool, project.stacks)
            for tool in ("php", "composer", "wp", "node", "chromium", "firefox", "webkit")] == [
                "8.3", "2.8", "2.12", "24.x", "1.56", "1.56", "1.56"]

    healthy = {"git": "git version 2.43", "uv": "uv 0.5.0", "php": "PHP 8.3.12", "composer": "Composer 2.8.9",
               "wp": "WP-CLI 2.12.0", "node": "node v24.8.0", "playwright": "Version 1.56.1",
               "chromium": "Chromium Playwright 1.56.1", "firefox": "Firefox Playwright 1.56.1",
               "webkit": "WebKit Playwright 1.56.1"}
    assert toolchain_gaps(healthy, project.tools, repo.versions, project.stacks, require_pins=True) == []
    healthy["composer"] = "Composer 2.7.9"
    healthy["firefox"] = "missing"
    assert toolchain_gaps(healthy, project.tools, repo.versions, project.stacks,
                          require_pins=True) == ["composer", "firefox"]
    assert set(toolchain_gaps(healthy, project.tools, {"tools": {"php": "8.3"}}, project.stacks,
                              require_pins=True)) == {"composer", "wp", "node", "playwright", "chromium", "firefox", "webkit"}
    assert version_matches("node v24.8.0", "24.x")
    assert version_matches("WP-CLI 2.12.1", "2.12")
    assert not version_matches("WP-CLI 2.11.9", "2.12")


def test_node_can_declare_extra_installable_tools_and_versions_use_lock_then_stack(control_repo):
    (control_repo / "nodes" / "cc.yaml").write_text("name: cc\nprojects: [TESTWP]\ntools: [docker]\n")
    (control_repo / "projects" / "TESTWP.yaml").write_text(
        "key: TESTWP\nrepo: jooservices/test-aiflow-wp\nstack: php-8.3\n")
    repo = ControlRepo.load(control_repo)
    assert "docker" in repo.tools_for_node("cc")
    assert pinned_version(repo.versions, "claude") == "2.1.305"
    assert pinned_version({"tools": {"codex": "0.4.1"}}, "codex-code-mode-host") == "0.4.1"
    assert pinned_version({"tools": {"codex": "0.147.0", "codex_code_mode_host": "0.144.0"}},
                          "codex-code-mode-host") == "0.147.0"
    assert pinned_version({}, "php", [repo.projects["TESTWP"].stack]) == "8.3"


def test_control_repo_reads_sa_thresholds(control_repo):
    (control_repo / "sa.yaml").write_text(
        "thresholds:\n  slots_used_percent: 75\n  slots_duration_minutes: 20\n  role_cooldown_minutes: 12\n  idle_days: 5\n")
    repo = ControlRepo.load(control_repo)
    assert repo.sa["thresholds"] == {"slots_used_percent": 75, "slots_duration_minutes": 20,
                                     "role_cooldown_minutes": 12, "idle_days": 5}


def test_watchdog_warns_when_a_node_misses_tools_for_a_project(client, ctx):
    from aiflow.v4.control.loops import watchdog

    ctx.repo().projects["TESTWP"].stack = "php-8.3"
    ctx.repo().versions["tools"].update({"php": "8.3", "composer": "2.8", "wp": "2.12"})
    cc = join(client, "cc")
    tools = {name: f"{name} 1.0" for name in NODE_TOOLS}
    tools.update({"php": "PHP 8.3.0", "composer": "missing", "wp": "WP-CLI 2.12.0"})
    response = client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "tools": tools}, headers=cc)

    assert response.json()["desired"]["tools"] == list(BASE_TOOLS) + ["claude", "opencode", "php", "composer", "wp"]
    node = next(n for n in client.get("/v1/state").json()["nodes"] if n["name"] == "cc")
    assert node["missing_tools"] == ["composer"]

    watchdog(ctx)
    incident = ctx.db.one("SELECT severity, title, detail FROM incidents"
                          " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")
    assert incident["severity"] == "warning"
    assert incident["title"] == "node cc misses tools for TESTWP"
    assert "composer" in incident["detail"]

    tools["composer"] = "Composer 2.8.0"
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "tools": tools}, headers=cc)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 0


def test_unhealthy_stack_blocks_only_its_project_and_watchdog_warns(client, ctx):
    from aiflow.v4.control.loops import watchdog
    from tests.v4.conftest import add_ticket

    repo = ctx.repo()
    repo.projects["TESTWP"].stack = "php-8.3"
    repo.projects["OTHER"] = Project(key="OTHER", repo="jooservices/other")
    repo.nodes["cc"].projects = ["TESTWP", "OTHER"]
    ctx.github.repositories.append({"full_name": "jooservices/other", "name": "other", "default_branch": "develop"})
    repo.versions["tools"] = {"php": "8.3", "composer": "2.8", "wp": "2.12"}
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-930")
    add_ticket(ctx.db, "OTHER-930")

    tools = {"git": "git 2.43", "uv": "uv 0.5", "claude": "Claude Code 2.1.305",
             "opencode": "opencode 1.0.0", "php": "PHP 8.3.9", "composer": "Composer 2.7.9",
             "wp": "WP-CLI 2.12.0"}
    client.post("/v1/nodes/cc/heartbeat", json={"tools": tools}, headers=cc)
    keys = [item["key"] for item in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                                 headers=cc).json()["tickets"]]
    assert keys == ["OTHER-930"]

    watchdog(ctx)
    incident = ctx.db.one("SELECT severity, detail FROM incidents WHERE kind='node_tools'"
                          " AND subject='cc:TESTWP' AND closed_at IS NULL")
    assert incident["severity"] == "warning" and "composer" in incident["detail"]

    tools["composer"] = "Composer 2.8.4"
    client.post("/v1/nodes/cc/heartbeat", json={"tools": tools}, headers=cc)
    watchdog(ctx)
    keys = [item["key"] for item in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                                 headers=cc).json()["tickets"]]
    assert set(keys) == {"TESTWP-930", "OTHER-930"}
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='node_tools'"
                      " AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 0


def test_dynamic_node_receives_stack_preflight_and_is_gated_until_healthy(client, ctx):
    from tests.v4.conftest import add_ticket

    repo = ctx.repo()
    repo.projects["TESTWP"].stack = "php-8.3"
    repo.versions["tools"] = {"php": "8.3", "composer": "2.8", "wp": "2.12"}
    repo.account("claude-01").node = "fresh-node"
    ctx.db.run("UPDATE accounts SET node=%s WHERE id=%s", ("fresh-node", "claude-01"))
    add_ticket(ctx.db, "TESTWP-931")

    headers = join(client, "fresh-node")
    tools = {"git": "git 2.43", "uv": "uv 0.5", "claude": "Claude Code 2.1.305", "php": "PHP 8.3.10",
             "composer": "Composer 2.7.9", "wp": "WP-CLI 2.12.0"}
    response = client.post("/v1/nodes/fresh-node/heartbeat", json={"tools": tools}, headers=headers)
    desired = response.json()["desired"]
    assert desired["projects"] == ["TESTWP"]
    assert desired["project_stacks"] == {"TESTWP": "php-8.3"}
    assert "composer" in desired["tools"]
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert candidates.json()["tickets"] == []

    tools["composer"] = "Composer 2.8.4"
    client.post("/v1/nodes/fresh-node/heartbeat", json={"tools": tools}, headers=headers)
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert [ticket["key"] for ticket in candidates.json()["tickets"]] == ["TESTWP-931"]


def test_project_requiring_containers_is_not_offered_until_runtime_is_healthy(client, ctx):
    from tests.v4.conftest import add_ticket

    ctx.repo().projects["TESTWP"].containers = True
    headers = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-932")
    tools = reported_tools(ctx, "cc")
    tools["safe-container-runtime"] = "missing"
    client.post("/v1/nodes/cc/heartbeat", json={"tools": tools}, headers=headers)
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert candidates.json()["tickets"] == []

    tools["safe-container-runtime"] = "rootless Podman 5.4; podman-compose 1.2; project quota"
    client.post("/v1/nodes/cc/heartbeat", json={"tools": tools}, headers=headers)
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert [ticket["key"] for ticket in candidates.json()["tickets"]] == ["TESTWP-932"]
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-932", "account": "claude-01"}, headers=headers)
    assert claim.status_code == 200 and claim.json()["project"]["containers"] is True
    run_headers = {"Authorization": f"Bearer {claim.json()['run_token']}"}
    allowed = client.post("/v1/policy/decide", json={"tool_name": "Bash",
                               "tool_input": {"command": "docker compose up -d"},
                               "cwd": "/srv/aiflow/work/TESTWP/TESTWP-932"}, headers=run_headers)
    assert allowed.json()["hookSpecificOutput"]["permissionDecision"] == "allow"
    denied = client.post("/v1/policy/decide", json={"tool_name": "Bash",
                              "tool_input": {"command": "docker run --rm -v /:/host alpine"},
                              "cwd": "/srv/aiflow/work/TESTWP/TESTWP-932"}, headers=run_headers)
    assert denied.json()["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_missing_runtime_gates_only_tickets_mapped_to_container_repository(client, ctx):
    from tests.v4.conftest import add_ticket

    project = ctx.repo().projects["TESTWP"]
    container_repo = "jooservices/container-checks"
    ctx.github.repositories.append({"full_name": container_repo, "name": "container-checks",
                                   "default_branch": "develop"})
    project.repositories = [{"repo": container_repo, "containers": True}]
    project.epic_repositories = {"TESTWP-932": container_repo}
    headers = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-932")
    add_ticket(ctx.db, "TESTWP-933")
    add_ticket(ctx.db, "TESTWP-934")
    ctx.db.run("UPDATE tickets SET type='Epic' WHERE key='TESTWP-932'")
    ctx.db.run("UPDATE tickets SET parent_key='TESTWP-932' WHERE key='TESTWP-933'")

    tools = reported_tools(ctx, "cc")
    tools["safe-container-runtime"] = "missing"
    client.post("/v1/nodes/cc/heartbeat", json={"tools": tools}, headers=headers)
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert [ticket["key"] for ticket in candidates.json()["tickets"]] == ["TESTWP-934"]

    tools["safe-container-runtime"] = "rootless Podman 5.4; podman-compose 1.2; project quota"
    client.post("/v1/nodes/cc/heartbeat", json={"tools": tools}, headers=headers)
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert {ticket["key"] for ticket in candidates.json()["tickets"]} == {
        "TESTWP-932", "TESTWP-933", "TESTWP-934"}


def test_missing_runtime_gates_resumed_work_using_the_previous_dev_repository_settings(client, ctx):
    import uuid

    from aiflow.v4.control.db import J
    from tests.v4.conftest import add_ticket

    project = ctx.repo().projects["TESTWP"]
    current_repository = "jooservices/current-checks"
    previous_repository = "jooservices/container-checks"
    ctx.github.repositories.extend([
        {"full_name": current_repository, "name": "current-checks", "default_branch": "develop"},
        {"full_name": previous_repository, "name": "container-checks", "default_branch": "develop"},
    ])
    project.repo = current_repository
    project.containers = False
    project.repositories = [{"repo": current_repository, "containers": False},
                            {"repo": previous_repository, "containers": True}]
    headers = join(client, "cc")

    def prior_container_dev(ticket: str) -> None:
        run_id = str(uuid.uuid4())
        settings = {"repo": previous_repository, "base": "main", "containers": True}
        ctx.db.run("INSERT INTO runs (id,ticket_key,project,step,account_id,provider,node,fence,status,outcome,"
                   "pr_url,details) VALUES (%s,%s,'TESTWP','dev','codex-01','codex','cc',1,'finished','done',%s,%s)",
                   (run_id, ticket, f"https://github.com/{previous_repository}/pull/7",
                    J({"repository": previous_repository, "repository_settings": settings})))

    add_ticket(ctx.db, "TESTWP-935", status="In Verify")
    add_ticket(ctx.db, "TESTWP-936", labels=("aiflow-ready", "rework"))
    add_ticket(ctx.db, "TESTWP-937")
    prior_container_dev("TESTWP-935")
    prior_container_dev("TESTWP-936")

    tools = reported_tools(ctx, "cc")
    tools["safe-container-runtime"] = "missing"
    client.post("/v1/nodes/cc/heartbeat", json={"tools": tools}, headers=headers)
    candidates = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=headers)
    assert [ticket["key"] for ticket in candidates.json()["tickets"]] == ["TESTWP-937"]


def test_watchdog_keeps_tool_incident_while_the_node_is_offline(client, ctx):
    from aiflow.v4.control.loops import watchdog

    ctx.repo().projects["TESTWP"].stack = "php-8.3"
    ctx.repo().versions["tools"].update({"php": "8.3", "composer": "2.8", "wp": "2.12"})
    cc = join(client, "cc")
    tools = {name: f"{name} 1.0" for name in NODE_TOOLS}
    tools.update({"php": "PHP 8.3.0", "composer": "missing", "wp": "WP-CLI 2.12.0"})
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "tools": tools}, headers=cc)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 1

    # No heartbeat means the tools are unknown: going offline must not close the incident.
    ctx.db.run("UPDATE nodes SET last_heartbeat = now() - interval '5 minutes' WHERE name='cc'")
    watchdog(ctx)
    assert ctx.db.one("SELECT status FROM nodes WHERE name='cc'")["status"] == "offline"
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 1

    # A later heartbeat that confirms the tool closes it.
    tools["composer"] = "Composer 2.8.0"
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "tools": tools}, headers=cc)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 0


def test_watchdog_closes_tool_incident_when_the_node_or_project_is_removed(client, ctx):
    from aiflow.v4.control.loops import watchdog

    repo = ctx.repo()
    repo.projects["TESTWP"].stack = "php-8.3"
    repo.versions["tools"].update({"php": "8.3", "composer": "2.8", "wp": "2.12"})
    cc = join(client, "cc")
    tools = {name: f"{name} 1.0" for name in NODE_TOOLS}
    tools.update({"php": "PHP 8.3.0", "composer": "missing", "wp": "WP-CLI 2.12.0"})
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "tools": tools}, headers=cc)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 1

    # The node no longer serves the project: the incident goes away.
    repo.nodes["cc"].projects = []
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 0

    # Serving it again with the tool still missing opens it again.
    repo.nodes["cc"].projects = ["TESTWP"]
    client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "tools": tools}, headers=cc)
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 1

    # Removing the node from the control repo closes it even when no heartbeat arrives.
    ctx.db.run("UPDATE nodes SET last_heartbeat = now() - interval '5 minutes' WHERE name='cc'")
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 1
    del repo.nodes["cc"]
    watchdog(ctx)
    assert ctx.db.one("SELECT count(*) AS n FROM incidents"
                      " WHERE kind='node_tools' AND subject='cc:TESTWP' AND closed_at IS NULL")["n"] == 0
