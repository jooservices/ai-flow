from aiflow.v4.core.controlrepo import ControlRepo, Project
from aiflow.v4.core.toolchains import NODE_TOOLS, STACK_TOOLS, missing_tools, tools_for_stack, tools_for_stacks


def join(client, name):
    r = client.post("/v1/nodes/join", json={"name": name, "join_token": "join-secret"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def test_stack_values_map_to_node_and_project_tools():
    assert STACK_TOOLS["php-8.3"] == ("php", "composer", "wp")
    assert STACK_TOOLS["python-3.12"] == ("python3", "uv")
    assert tools_for_stack("php-8.3") == list(NODE_TOOLS) + ["php", "composer", "wp"]
    assert "python3" in tools_for_stack("python-3.12")
    assert tools_for_stack("unknown") == list(NODE_TOOLS)
    assert tools_for_stacks(["php-8.3", "python-3.12"]) == list(NODE_TOOLS) + ["php", "composer", "wp", "python3"]


def test_project_tools_follow_the_stack_and_missing_is_explicit():
    project = Project(key="TESTWP", repo="jooservices/test-aiflow-wp", stack="php-8.3")
    assert project.tools == list(NODE_TOOLS) + ["php", "composer", "wp"]
    assert missing_tools({"php": "PHP 8.3.0", "composer": "missing"}, project.tools) == \
        ["claude", "codex", "codex-code-mode-host", "grok", "opencode", "docker", "git", "node", "uv", "gh",
         "composer", "wp"]


def test_control_repo_reads_the_project_stack_and_node_tool_union(control_repo):
    (control_repo / "projects" / "TESTWP.yaml").write_text(
        "key: TESTWP\nrepo: jooservices/test-aiflow-wp\nstack: php-8.3\n")
    repo = ControlRepo.load(control_repo)
    assert repo.projects["TESTWP"].stack == "php-8.3"
    assert repo.projects["TESTWP"].tools == list(NODE_TOOLS) + ["php", "composer", "wp"]
    assert repo.tools_for_node("cc") == list(NODE_TOOLS) + ["php", "composer", "wp"]


def test_watchdog_warns_when_a_node_misses_tools_for_a_project(client, ctx):
    from aiflow.v4.control.loops import watchdog

    ctx.repo().projects["TESTWP"].stack = "php-8.3"
    cc = join(client, "cc")
    tools = {name: f"{name} 1.0" for name in NODE_TOOLS}
    tools.update({"php": "PHP 8.3.0", "composer": "missing", "wp": "WP-CLI 2.10"})
    response = client.post("/v1/nodes/cc/heartbeat", json={"slots_total": 2, "tools": tools}, headers=cc)

    assert response.json()["desired"]["tools"] == list(NODE_TOOLS) + ["php", "composer", "wp"]
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


def test_watchdog_keeps_tool_incident_while_the_node_is_offline(client, ctx):
    from aiflow.v4.control.loops import watchdog

    ctx.repo().projects["TESTWP"].stack = "php-8.3"
    cc = join(client, "cc")
    tools = {name: f"{name} 1.0" for name in NODE_TOOLS}
    tools.update({"php": "PHP 8.3.0", "composer": "missing", "wp": "WP-CLI 2.10"})
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
    cc = join(client, "cc")
    tools = {name: f"{name} 1.0" for name in NODE_TOOLS}
    tools.update({"php": "PHP 8.3.0", "composer": "missing", "wp": "WP-CLI 2.10"})
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
