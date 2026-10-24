"""One setting for a project's test services: `databases` is read as `services`, the Projects screen saves it, the
machines prepare and report it, and the flow doctor declares a service a ticket is parked on (docs/v4/test-services.md)."""
import yaml
from conftest import add_ticket, join_node
from test_projects import owner_sign_in, prepare_project_control, project_body, save_project

from aiflow.v4.control import tooling
from aiflow.v4.core.controlrepo import ControlRepo, Project

PROJECT_FILE = "key: TESTWP\nrepo: jooservices/test-aiflow-wp\nbase: develop\nchecks: [pytest]\n"
HEALTHY = {name: {"state": "running", "healthy": True, "detail": "ok", "endpoint": "127.0.0.1:1"}
           for name in ("postgres", "redis")}


def write_project(control_repo, extra):
    (control_repo / "projects" / "TESTWP.yaml").write_text(PROJECT_FILE + extra, encoding="utf-8")


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def candidates(client, headers):
    return {t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                         headers=headers).json()["tickets"]}


def test_project_file_with_only_databases_migrates_to_services(control_repo):
    write_project(control_repo, "databases: [postgres, redis]\n")
    project = ControlRepo.load(control_repo).projects["TESTWP"]
    assert project.services == ["postgres", "redis"] and project.required_services == ["postgres", "redis"]
    assert project.databases == []

    both = Project(key="X", repo="o/x", services=["redis"], databases=["PostgreSQL", "redis", "mysql"])
    assert both.required_services == ["redis", "postgres"]
    assert both.databases == ["mysql"]  # only names that are no test service stay behind
    assert Project(key="Y", repo="o/y", databases="pg").required_services == ["postgres"]


def test_project_with_services_makes_machines_prepare_and_report_them(client, ctx, control_repo):
    write_project(control_repo, "databases: [postgres, redis]\n")  # the JAIF file of 2026-10-24
    ctx.repo = lambda: ControlRepo.load(control_repo)
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-950")

    r = client.post("/v1/nodes/cc/heartbeat", json={"tools": {}}, headers=cc)
    assert r.json()["desired"]["project_services"] == {"TESTWP": ["postgres", "redis"]}
    assert "TESTWP-950" not in candidates(client, cc)  # not reported yet
    client.post("/v1/nodes/cc/heartbeat", json={"tools": {}, "services": HEALTHY}, headers=cc)
    assert "TESTWP-950" in candidates(client, cc)


def test_projects_screen_saves_one_services_field_and_the_listing_shows_it(client, ctx, control_repo):
    write_project(control_repo, "databases: [redis]\n")
    prepare_project_control(ctx, control_repo)
    owner_sign_in(client, ctx)
    assert {p["key"]: p["services"] for p in client.get("/v1/projects").json()["projects"]}["TESTWP"] == ["redis"]

    saved = client.post("/v1/projects/TESTWP/services", json={"services": ["PostgreSQL", "redis"]})
    assert saved.status_code == 200 and saved.json()["services"] == ["postgres", "redis"]
    written = yaml.safe_load(ctx.github.commits[-1]["content"])
    assert written["services"] == ["postgres", "redis"] and "databases" not in written
    assert ctx.repo().projects["TESTWP"].required_services == ["postgres", "redis"]
    assert client.post("/v1/projects/TESTWP/services", json={"services": ["mysql"]}).status_code == 422
    assert client.post("/v1/projects/NOPE/services", json={"services": []}).status_code == 404

    save_project(client, ctx, control_repo, project_body(services=["redis", "postgres"]))
    assert yaml.safe_load(ctx.github.commits[-1]["content"])["services"] == ["redis", "postgres"]


def test_park_on_an_undeclared_service_is_declared_by_the_flow_doctor(client, ctx, control_repo):
    write_project(control_repo, "")
    prepare_project_control(ctx, control_repo)
    add_ticket(ctx.db, "TESTWP-951")
    ctx.db.run("INSERT INTO tool_waits (ticket_key,project,requirement,machine,run_id)"
               " VALUES ('TESTWP-951','TESTWP','postgres','cc','r1'), ('TESTWP-951','TESTWP','redis','cc','r1')")
    assert tooling.undeclared_service_waits(ctx) == {"TESTWP": ["postgres", "redis"]}

    assert tooling.declare_undeclared_services(ctx) == ["TESTWP"]
    assert ctx.repo().projects["TESTWP"].required_services == ["postgres", "redis"]
    assert tooling.undeclared_service_waits(ctx) == {}

    # the machine now prepares and reports them, which ends the park
    cc = join(client, "cc")
    heartbeat = client.post("/v1/nodes/cc/heartbeat", json={"tools": {}, "services": HEALTHY}, headers=cc)
    assert heartbeat.json()["desired"]["project_services"] == {"TESTWP": ["postgres", "redis"]}
    assert ctx.db.one("SELECT count(*) AS n FROM tool_waits WHERE resolved_at IS NULL")["n"] == 0
    assert "TESTWP-951" in candidates(client, cc)


def test_park_on_an_undeclared_service_raises_one_alert_when_it_cannot_be_declared(ctx, control_repo):
    write_project(control_repo, "")
    prepare_project_control(ctx, control_repo)
    ctx.github = None  # GitHub is not connected: the project file cannot be written
    add_ticket(ctx.db, "TESTWP-952")
    add_ticket(ctx.db, "TESTWP-953")
    for key in ("TESTWP-952", "TESTWP-953"):
        ctx.db.run("INSERT INTO tool_waits (ticket_key,project,requirement,machine,run_id)"
                   " VALUES (%s,'TESTWP','postgres','cc','r1')", (key,))

    assert tooling.declare_undeclared_services(ctx) == []
    assert tooling.declare_undeclared_services(ctx) == []
    alerts = ctx.db.all("SELECT subject, detail FROM incidents WHERE kind='service_undeclared' AND closed_at IS NULL")
    assert len(alerts) == 1 and alerts[0]["subject"] == "TESTWP" and "Projects screen" in alerts[0]["detail"]

    write_project(control_repo, "services: [postgres]\n")  # declared by hand: the alert closes
    assert tooling.declare_undeclared_services(ctx) == []
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='service_undeclared' AND closed_at IS NULL")["n"] == 0
