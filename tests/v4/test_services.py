"""Test services (PostgreSQL with pgvector, Redis) are prepared by the node agent, reported and gate project work."""
from types import SimpleNamespace

from aiflow.v4.core.controlrepo import Project
from aiflow.v4.core.services import SERVICES, normalize_services, service_gaps
from aiflow.v4.node import agent
from aiflow.v4.node import services as node_services
from conftest import join_node


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


class FakeDocker:
    """A tiny Docker: containers by name with a status; records every call."""

    def __init__(self, containers=None, fail_start=False, fail_run=False, healthy=True):
        self.containers = dict(containers or {})
        self.calls: list[list[str]] = []
        self.fail_start, self.fail_run, self.healthy = fail_start, fail_run, healthy

    def __call__(self, cmd, **kwargs):
        assert cmd[0] == "docker"
        args = cmd[1:]
        self.calls.append(args)
        ok = SimpleNamespace(returncode=0, stdout="", stderr="")
        bad = SimpleNamespace(returncode=1, stdout="", stderr="boom")
        if args[0] == "inspect":
            name = args[3]
            if name not in self.containers:
                return SimpleNamespace(returncode=1, stdout="", stderr="No such object")
            return SimpleNamespace(returncode=0, stdout=self.containers[name] + "\n", stderr="")
        if args[0] == "start":
            if self.fail_start:
                return bad
            self.containers[args[1]] = "running"
            return ok
        if args[0] == "rm":
            self.containers.pop(args[2], None)
            return ok
        if args[0] == "run":
            if self.fail_run:
                return bad
            self.containers[args[args.index("--name") + 1]] = "running"
            return ok
        if args[0] == "exec":
            if not self.healthy:
                return bad
            return SimpleNamespace(returncode=0, stdout="PONG" if "redis-cli" in args else "ok", stderr="")
        raise AssertionError(args)

    def run_args(self, container):
        return next(a for a in self.calls if a[0] == "run" and container in a)


def prepare(docker, names=("postgres", "redis")):
    return node_services.prepare_services(list(names), docker, wait_s=0, sleep=lambda s: None)


def test_declared_services_are_normalized():
    assert normalize_services(["PostgreSQL", "redis", "postgres", "mysql"]) == ["postgres", "redis"]
    assert normalize_services("pgvector") == ["postgres"]
    assert normalize_services(None) == []
    assert Project(key="X", repo="o/x", services=["postgres"]).required_services == ["postgres"]


def test_missing_services_are_created_at_the_test_defaults_with_a_small_footprint():
    docker = FakeDocker()
    result = prepare(docker)
    assert {k: (v["state"], v["healthy"]) for k, v in result.items()} == {
        "postgres": ("recreated", True), "redis": ("recreated", True)}
    assert result["postgres"]["endpoint"] == "127.0.0.1:15499" and result["redis"]["endpoint"] == "127.0.0.1:16399"
    pg = docker.run_args("aiflow4-test-pg")
    assert "127.0.0.1:15499:5432" in pg and "pgvector/pgvector:0.8.7-pg16" in pg
    assert {"POSTGRES_USER=aiflow", "POSTGRES_PASSWORD=test"} <= set(pg)
    assert pg[pg.index("--restart") + 1] == "unless-stopped" and pg[pg.index("--memory") + 1] == "512m"
    redis = docker.run_args("aiflow4-test-redis")
    assert "127.0.0.1:16399:6379" in redis and redis[redis.index("--restart") + 1] == "unless-stopped"
    assert "128m" in redis
    # the vector extension is created where per-session test databases come from
    extension = [a for a in docker.calls if a[:2] == ["exec", "aiflow4-test-pg"] and "psql" in a]
    assert {a[a.index("-d") + 1] for a in extension} == {"aiflow4", "template1"}
    assert all("CREATE EXTENSION IF NOT EXISTS vector" in a for a in extension)


def test_running_service_is_left_alone_and_stopped_one_is_started():
    docker = FakeDocker({"aiflow4-test-pg": "running", "aiflow4-test-redis": "exited"})
    result = prepare(docker)
    assert result["postgres"]["state"] == "running" and result["redis"]["state"] == "started"
    assert not any(a[0] == "run" for a in docker.calls)


def test_stopped_service_that_will_not_start_is_recreated():
    docker = FakeDocker({"aiflow4-test-redis": "dead"}, fail_start=True)
    result = prepare(docker, ["redis"])
    assert result["redis"]["state"] == "recreated" and result["redis"]["healthy"]
    assert ["rm", "-f", "aiflow4-test-redis"] in docker.calls


def test_unhealthy_service_and_docker_failures_are_reported_not_raised():
    result = prepare(FakeDocker({"aiflow4-test-pg": "running"}, healthy=False), ["postgres"])
    assert result["postgres"]["healthy"] is False and result["postgres"]["state"] == "unhealthy"
    result = prepare(FakeDocker(fail_run=True), ["redis"])
    assert not result["redis"]["healthy"] and "could not create" in result["redis"]["detail"]

    def no_docker(cmd, **kwargs):
        raise FileNotFoundError("docker")

    result = prepare(no_docker, ["redis"])
    assert result["redis"]["detail"] == "Docker is not available on this node"


def test_service_gaps_name_unreported_and_unhealthy_services():
    reported = {"postgres": {"healthy": True}, "redis": {"healthy": False, "detail": "Redis does not answer PING"}}
    assert service_gaps(reported, ["postgres", "redis"]) == {"redis": "Redis does not answer PING"}
    assert service_gaps({}, ["postgres"]) == {"postgres": "not reported by the node agent yet"}
    assert service_gaps(None, []) == {}
    assert set(SERVICES) == {"postgres", "redis"}


def test_agent_prepares_declared_services_and_reports_them_in_the_heartbeat(monkeypatch):
    import threading

    heartbeats = []
    stop = threading.Event()

    class FakeApi:
        def __init__(self, *args, **kwargs):
            pass

        def call(self, method, path, body=None, **kwargs):
            if not path.endswith("/heartbeat"):
                return {}
            heartbeats.append(body)
            if len(heartbeats) == 2:
                stop.set()
            return {"desired": {"accounts": [], "workers": [], "projects": ["WEB"],
                                "project_services": {"WEB": ["postgres", "redis"]}}}

    seen = []

    def fake_prepare(names, *args, **kwargs):
        seen.append(names)
        return {n: {"state": "recreated", "healthy": True, "detail": "ok", "endpoint": "x"} for n in names}

    monkeypatch.setattr(agent, "ControlClient", FakeApi)
    monkeypatch.setattr(agent, "metrics", lambda prev: ({"hostname": "node1"}, (1, 1)))
    monkeypatch.setattr(agent, "worker_states", lambda: [])
    monkeypatch.setattr(agent, "tool_versions", lambda names=None: {})
    monkeypatch.setattr(node_services, "prepare_services", fake_prepare)
    monkeypatch.setenv("AIFLOW4_POLICY_RELAY", "0")

    class Immediate(threading.Thread):
        def start(self):
            self.run()

    monkeypatch.setattr(agent.threading, "Thread", Immediate)
    agent.run_agent("http://control", "node1", "token", every_s=0, stop=stop, manage_workers=False)

    assert seen == [["postgres", "redis"]]
    assert heartbeats[0]["services"] == {}
    assert heartbeats[1]["services"]["postgres"]["state"] == "recreated"
    assert heartbeats[1]["services"]["redis"]["healthy"] is True


def test_project_without_healthy_services_gets_no_work_and_command_center_shows_why(client, ctx):
    from aiflow.v4.control.loops import watchdog
    from aiflow.v4.control.state import snapshot
    from tests.v4.conftest import add_ticket

    repo = ctx.repo()
    repo.projects["TESTWP"].services = ["postgres", "redis"]
    repo.projects["OTHER"] = Project(key="OTHER", repo="jooservices/other")
    repo.nodes["cc"].projects = ["TESTWP", "OTHER"]
    ctx.github.repositories.append({"full_name": "jooservices/other", "name": "other", "default_branch": "develop"})
    cc = join(client, "cc")
    add_ticket(ctx.db, "TESTWP-940")
    add_ticket(ctx.db, "OTHER-940")

    def candidates():
        return {t["key"] for t in client.get("/v1/work/candidates", params={"account": "claude-01"},
                                             headers=cc).json()["tickets"]}

    r = client.post("/v1/nodes/cc/heartbeat", json={"services": {}}, headers=cc)
    assert r.json()["desired"]["project_services"] == {"TESTWP": ["postgres", "redis"]}
    assert candidates() == {"OTHER-940"}  # nothing reported yet: no work for the project that needs services

    sick = {"postgres": {"state": "running", "healthy": True, "detail": "ok", "endpoint": "127.0.0.1:15499"},
            "redis": {"state": "unhealthy", "healthy": False, "detail": "Redis does not answer PING (started)",
                      "endpoint": "127.0.0.1:16399"}}
    client.post("/v1/nodes/cc/heartbeat", json={"services": sick}, headers=cc)
    assert candidates() == {"OTHER-940"}
    watchdog(ctx)
    incident = ctx.db.one("SELECT severity, detail FROM incidents WHERE kind='node_services'"
                          " AND subject='cc:TESTWP' AND closed_at IS NULL")
    assert incident["severity"] == "warning" and "redis" in incident["detail"] and "PING" in incident["detail"]

    node = next(n for n in snapshot(ctx)["nodes"] if n["name"] == "cc")
    by_name = {s["name"]: s for s in node["services"]}
    assert by_name["postgres"]["healthy"] and by_name["postgres"]["endpoint"] == "127.0.0.1:15499"
    assert not by_name["redis"]["healthy"] and "PING" in by_name["redis"]["detail"]
    assert by_name["redis"]["projects"] == ["TESTWP"]

    healthy = {**sick, "redis": {**sick["redis"], "state": "started", "healthy": True, "detail": "answers PING"}}
    client.post("/v1/nodes/cc/heartbeat", json={"services": healthy}, headers=cc)
    watchdog(ctx)
    assert candidates() == {"TESTWP-940", "OTHER-940"}
    assert ctx.db.one("SELECT count(*) AS n FROM incidents WHERE kind='node_services' AND closed_at IS NULL")["n"] == 0


def test_node_health_preflight_requires_declared_services_to_be_healthy(client, ctx):
    ctx.repo().projects["TESTWP"].services = ["postgres", "redis"]
    token = client.post("/v1/nodes/join-tokens", json={
        "provision_key": "join-secret", "name": "node-service-health", "project": "TESTWP"})
    assert token.status_code == 200
    joined = client.post("/v1/nodes/join", json={"join_token": token.json()["token"]})
    assert joined.status_code == 200
    headers = {"Authorization": f"Bearer {joined.json()['node_token']}"}
    tools = {"git": "git version 2", "uv": "uv 0.1", "codex": "codex 1.0",
             "codex-code-mode-host": "present /usr/local/bin/codex-code-mode-host"}
    ok = {"state": "running", "healthy": True, "detail": "ok"}

    client.post("/v1/nodes/node-service-health/heartbeat", json={"tools": tools, "slots_total": 2}, headers=headers)
    health = client.get("/v1/nodes/node-service-health/health", headers=headers).json()
    assert health["missing_tools"] == [] and health["preflight_ok"] is False
    assert set(health["missing_services"]) == {"postgres", "redis"}

    sick = {"postgres": ok, "redis": {"state": "absent", "healthy": False, "detail": "image not found"}}
    client.post("/v1/nodes/node-service-health/heartbeat", json={"tools": tools, "slots_total": 2, "services": sick}, headers=headers)
    health = client.get("/v1/nodes/node-service-health/health", headers=headers).json()
    assert health["preflight_ok"] is False and health["missing_services"] == {"redis": "image not found"}

    client.post("/v1/nodes/node-service-health/heartbeat", json={"tools": tools, "slots_total": 2,
                                                                  "services": {"postgres": ok, "redis": ok}}, headers=headers)
    health = client.get("/v1/nodes/node-service-health/health", headers=headers).json()
    assert health["preflight_ok"] is True and health["missing_services"] == {}
