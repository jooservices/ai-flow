"""`aiflow4 install control` and `aiflow4 secret` (TESTFLOW-70) against a fake host: server-installer, Docker,
aiflow4-deploy and HTTP probes are simulated, files are written under a temporary root."""
from __future__ import annotations

import io
import itertools
import os
import re
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

from aiflow.v4 import cli
from aiflow.v4.install import control as ci
from aiflow.v4.install import secrets as sec

ROOT = Path(__file__).resolve().parents[2]
IMAGE = ci.IMAGE
INFRA = {"postgres", "redis", "traefik", "openobserve", "gatus", "svc-mailpit"}


class FakeHost(ci.Host):
    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[tuple[list[str], dict[str, str], str | None]] = []
        self.containers: dict[str, dict] = {}  # name -> {"id", "image", "health"}
        self.images: set[str] = set()
        self.ids = itertools.count(1)
        self.broken: dict[str, set[str]] = {}  # version -> components that fail on it
        self.down: set[str] = set()  # components failing whatever runs
        self.deploy_rc: dict[str, int] = {}
        self.check_rc = 0
        self.si_rc = {"plan": 0, "apply": 0}
        self.clock = 0.0
        self.downloads: list[str] = []
        self.migrate_output = "applied: nothing"

    # -- the machine --
    def euid(self) -> int:
        return 0

    def machine(self) -> str:
        return "x86_64"

    def monotonic(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds

    def download(self, url: str, dest: Path) -> None:
        self.downloads.append(url)
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            data = b"#!/bin/bash\n"
            info = tarfile.TarInfo("server-installer-1.1.0/bin/server-installer")
            info.size, info.mode = len(data), 0o755
            tar.addfile(info, io.BytesIO(data))
        dest.write_bytes(buf.getvalue())

    def http_ok(self, url: str) -> bool:
        component = {"/ping": "traefik", "/o2/healthz": "openobserve", ":8081/health": "gatus"}
        for suffix, name in component.items():
            if url.endswith(suffix):
                return name not in self.failing() and f"aiflow4-{name}" in self.containers
        return "command-center" not in self.failing() and "aiflow4-control-api-a" in self.containers

    def failing(self) -> set[str]:
        cur = (self.p("/srv/aiflow/control/releases/CURRENT").read_text().strip()
               if self.p("/srv/aiflow/control/releases/CURRENT").is_file() else "")
        return self.down | self.broken.get(cur, set())

    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    # -- commands --
    def run(self, argv, *, env=None, stdin=None, stream=False, timeout=3600):
        self.calls.append((list(argv), dict(env or {}), stdin))
        name = Path(argv[0]).name
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        fail = lambda rc=1, err="": subprocess.CompletedProcess(argv, rc, "", err)  # noqa: E731
        if name == "dpkg-query":
            return ok("install ok installed")
        if name == "server-installer":
            return ok() if self.si_rc[argv[1]] == 0 else fail(self.si_rc[argv[1]])
        if name == "git":
            Path(argv[-1], ".git").mkdir(parents=True)
            return ok()
        if name == "aiflow4-deploy":
            return self.deploy(argv[1:], env or {})
        if name == "aiflow4-control-install":
            cli_dir = self.p(f"/srv/aiflow/control/cli/{argv[2]}")
            cli_dir.mkdir(parents=True, exist_ok=True)
            link = self.p("/srv/aiflow/control/cli/current")
            if link.is_symlink():
                link.unlink()
            link.symlink_to(cli_dir)
            return ok()
        if argv[:2] == ["docker", "login"]:
            return ok("Login Succeeded")
        if argv[:3] == ["docker", "network", "inspect"]:
            return ok("172.28.9.15/16\n")
        if argv[:3] == ["docker", "image", "inspect"]:
            return ok() if argv[3] in self.images else fail()
        if argv[:2] == ["docker", "inspect"]:
            c = self.containers.get(argv[-1])
            if not c:
                return fail()
            if argv[3] == "{{.Config.Image}}":
                return ok(c["image"] + "\n")
            component = argv[-1].removeprefix("aiflow4-")
            if argv[3] == "{{.State.Running}}":
                return ok("false\n" if component in self.failing() else "true\n")
            return ok("unhealthy\n" if component in self.failing() else "healthy\n")
        if argv[:2] == ["docker", "compose"]:
            if argv[3].endswith("infra/compose.yaml"):
                for svc in INFRA:
                    self.containers.setdefault(f"aiflow4-{svc}", {"id": next(self.ids), "image": svc})
                return ok()
            svc, tag = argv[-1], env["AIFLOW4_IMAGE_TAG"]
            c = self.containers.get(f"aiflow4-{svc}")
            if not c or c["image"] != f"{IMAGE}:{tag}":
                self.containers[f"aiflow4-{svc}"] = {"id": next(self.ids), "image": f"{IMAGE}:{tag}"}
            return ok()
        if argv[:2] == ["docker", "run"]:
            image_at = next(i for i, a in enumerate(argv) if a.startswith(IMAGE + ":"))
            command = argv[image_at + 1]
            if command == "upgrade-check":
                return subprocess.CompletedProcess(argv, self.check_rc, '{"ok":true}' if not self.check_rc else
                                                   '{"ok":false,"reasons":["node n1 would be outside"]}', "")
            if "aiflow4-postgres" not in self.containers:
                return fail(1, "could not connect")
            return ok(self.migrate_output)
        raise AssertionError(f"unexpected command {argv}")

    def deploy(self, args: list[str], env: dict[str, str]):
        argv = ["aiflow4-deploy", *args]
        stage_only = args[0] == "--stage-only"
        v = args[-1]
        if stage_only or not self.p(f"/srv/aiflow/build/{v}").is_dir():
            rel = self.p(f"/srv/aiflow/control/releases/{v}")
            rel.mkdir(parents=True, exist_ok=True)
            (rel / f"aiflow4-{v}.tar.gz").write_bytes(b"signed tarball")
            bundle = self.p(f"/srv/aiflow/build/{v}/deploy/v4")
            for rel_path in ("control/compose.yaml", "control/infra.yaml", "control/gatus.yaml", "o2/lab.env"):
                (bundle / rel_path).parent.mkdir(parents=True, exist_ok=True)
                (bundle / rel_path).write_bytes((ROOT / "deploy/v4" / rel_path).read_bytes())
            self.images.add(f"{IMAGE}:{v}")
            if stage_only:
                return subprocess.CompletedProcess(argv, 0, "", "")
        rc = self.deploy_rc.get(v, 0)
        if rc:
            return subprocess.CompletedProcess(argv, rc, "", "")
        for svc in ci.APP_SERVICES:
            self.containers[f"aiflow4-{svc}"] = {"id": next(self.ids), "image": f"{IMAGE}:{v}"}
        self.p("/srv/aiflow/control/releases/CURRENT").write_text(v + "\n")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def commands(self, name: str) -> list[list[str]]:
        return [argv for argv, _, _ in self.calls if Path(argv[0]).name == name or argv[:2] == ["docker", name]]


@pytest.fixture
def machine(tmp_path):
    root = tmp_path / "host"
    (root / "etc").mkdir(parents=True)
    (root / "etc/os-release").write_text('NAME="Ubuntu"\nID=ubuntu\nVERSION_ID="24.04"\n')
    (root / "usr/local/bin").mkdir(parents=True)
    (root / "usr/local/bin/cosign").write_bytes(b"cosign")
    return FakeHost(root)


def installer(host: FakeHost, out: list[str], **opts) -> ci.ControlInstaller:
    opts = {"version": "v4.1.0", "domain": "aiflow.example.com", "health_timeout": 30,
            "admin_email": "jooservices@gmail.com"} | opts
    return ci.ControlInstaller(ci.Options(**opts), host=host, layout=ci.Layout(host.root),
                               out=out.append, err=out.append, source=ROOT)


def secret_files(host: FakeHost) -> dict[str, bytes]:
    d = host.p("/etc/aiflow/secrets/control")
    return {p.name: p.read_bytes() for p in sorted(d.glob("*.env"))}


def all_secret_values(host: FakeHost) -> list[str]:
    return sec.secret_values(host.p("/etc/aiflow/secrets/control"))


def index(host: FakeHost, predicate) -> int:
    return next(i for i, (argv, _, _) in enumerate(host.calls) if predicate(argv))


# ---- AC1, AC2, AC3: a clean install ----

@pytest.mark.parametrize("ubuntu", ["22.04", "24.04"])
def test_clean_install_runs_every_step_in_order_and_brings_the_control_plane_up(machine, ubuntu):
    machine.p("/etc/os-release").write_text(f'ID=ubuntu\nVERSION_ID="{ubuntu}"\n')
    out: list[str] = []
    inst = installer(machine, out)
    assert inst.run() == 0, out
    assert inst.steps == list(ci.STEPS)
    # Docker comes from server-installer, the compose bundle and image from the signed release, then secrets,
    # the services, the migration, the deploy and the health check.
    order = [index(machine, lambda a: a[1:2] == ["plan"]), index(machine, lambda a: a[1:2] == ["apply"]),
             index(machine, lambda a: a[-2:] == ["--stage-only", "v4.1.0"]),
             index(machine, lambda a: a[:2] == ["docker", "compose"] and a[3].endswith("infra/compose.yaml")),
             index(machine, lambda a: a[:3] == ["docker", "network", "inspect"]),
             index(machine, lambda a: a[:2] == ["docker", "run"] and a[-1] == "migrate"),
             index(machine, lambda a: Path(a[0]).name == "aiflow4-deploy" and a[1:] == ["v4.1.0"])]
    assert order == sorted(order)
    assert {f"aiflow4-{c}" for c in INFRA | set(ci.APP_SERVICES)} <= set(machine.containers)
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"
    assert out[-1] == "AI Flow control plane v4.1.0 is running and healthy. Command Center: https://aiflow.example.com/cc"
    assert any(line.startswith("health: postgres ok, redis ok, traefik ok, openobserve ok, gatus ok") for line in out)
    # the control host does not run a node agent until it is joined
    assert sec.read_env(machine.p("/etc/aiflow/control.env")) == {
        "AIFLOW4_DOMAIN": "aiflow.example.com", "AIFLOW4_JOIN_CONTROL_URL": "http://aiflow.example.com",
        "AIFLOW4_DEPLOY_NODE_AGENT": "0", "AIFLOW4_CONSOLE_ADMIN_EMAIL": "jooservices@gmail.com"}
    assert machine.p("/srv/aiflow/control/cli/current").resolve().name == "v4.1.0"
    for name in ("postgres", "redis", "openobserve"):
        assert machine.p(f"/srv/aiflow/control/data/{name}").is_dir()


def test_internal_secrets_are_written_0600_under_etc_aiflow_secrets(machine):
    assert installer(machine, []).run() == 0
    d = machine.p("/etc/aiflow/secrets/control")
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    files = secret_files(machine)
    assert set(files) == {"aiflow4.env", "postgres.env", "openobserve.env"}
    for name in files:
        assert stat.S_IMODE((d / name).stat().st_mode) == 0o600
    app = sec.read_env(d / "aiflow4.env")
    pg = sec.read_env(d / "postgres.env")["POSTGRES_PASSWORD"]
    assert len(pg) >= 32 and app["AIFLOW4_DATABASE_URL"] == f"postgresql://aiflow:{pg}@aiflow4-postgres:5432/aiflow4"
    for key in ("AIFLOW4_JOIN_TOKEN", "AIFLOW4_OWNER_TOKEN", "AIFLOW4_REVIEWER_TOKEN", "AIFLOW4_CONSOLE_SESSION_SECRET",
                "O2_BASIC"):
        assert len(app[key]) >= 32
    assert app["AIFLOW4_PUBLIC_URL"] == "https://aiflow.example.com"
    assert app["AIFLOW4_GATUS_URL"] == "http://aiflow.example.com:8081/"
    assert app["AIFLOW4_TRUSTED_PROXIES"] == "172.28.0.0/16"


def test_trusted_proxy_cidrs_are_canonical_private_and_narrow():
    assert ci.sanitize_proxy_cidrs(
        "172.28.9.15/16\nfd00:abcd::5/64\n172.28.0.0/16\n0.0.0.0/0\n"
        "203.0.113.0/24\nnot-a-cidr") == ["172.28.0.0/16", "fd00:abcd::/64"]


@pytest.mark.parametrize("component", ["postgres", "redis", "traefik", "openobserve", "gatus", "control-api-b",
                                       "control-loops", "command-center"])
def test_a_failed_health_check_exits_non_zero_and_names_the_component(machine, component):
    machine.down = {component}
    out: list[str] = []
    rc = installer(machine, out).run()
    assert rc == ci.EXIT_UNHEALTHY
    assert re.search(rf"health check failed: .*{re.escape(component)}", out[-1]), out[-1]


def test_unsupported_os_or_bad_arguments_stop_before_any_change(machine):
    machine.p("/etc/os-release").write_text('ID=ubuntu\nVERSION_ID="20.04"\n')
    out: list[str] = []
    assert installer(machine, out).run() == ci.EXIT_USAGE
    assert "supports Ubuntu 22.04 and 24.04" in out[-1]
    assert machine.calls == []
    machine.p("/etc/os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
    for opts, message in (({"version": "4.1.0"}, "vX.Y.Z"), ({"version": "main"}, "vX.Y.Z"),
                          ({"domain": "bad domain"}, "--domain"), ({"stack": "ruby-3"}, "php-X.Y"),
                          ({"si": ["PHP_VERSION=8.3"]}, "SI_NAME=VALUE"), ({"si": ["SI_X=$(id)"]}, "SI_NAME=VALUE"),
                          ({"rollback": True}, "--rollback alone")):
        out = []
        assert installer(machine, out, **opts).run() == ci.EXIT_USAGE, opts
        assert message in out[-1]
    assert machine.calls == []


def test_os_preparation_failure_stops_with_its_own_exit_code(machine):
    machine.si_rc["apply"] = 1
    out: list[str] = []
    assert installer(machine, out).run() == ci.EXIT_OS
    assert "server-installer v1.1.0 `apply --modules" in out[-1]
    assert not machine.commands("aiflow4-deploy")


# ---- AC4: the same command twice ----

def test_second_run_keeps_secrets_data_and_containers(machine):
    assert installer(machine, []).run() == 0
    secrets_before = secret_files(machine)
    containers_before = {k: dict(v) for k, v in machine.containers.items()}
    data = machine.p("/srv/aiflow/control/data/postgres/PG_VERSION")
    data.write_text("16\n")
    calls_before = len(machine.calls)
    out: list[str] = []
    second = installer(machine, out)
    assert second.run() == 0
    rerun = [argv for argv, _, _ in machine.calls[calls_before:]]
    assert secret_files(machine) == secrets_before
    assert machine.containers == containers_before
    assert data.read_text() == "16\n"
    assert not [a for a in rerun if Path(a[0]).name == "aiflow4-deploy"]  # no download, no redeploy
    assert not [a for a in rerun if Path(a[0]).name == "aiflow4-control-install"]
    assert "release v4.1.0 is already staged and verified" in out
    assert any("already on v4.1.0" in line for line in out)
    assert "secrets in " in "\n".join(out) and "(0600): unchanged" in "\n".join(out)
    assert not [a for a in rerun if "--force-recreate" in a]
    assert second.steps == list(ci.STEPS)


def console_seeds(host: FakeHost) -> list[list[str]]:
    return [argv for argv, _, _ in host.calls if argv[:2] == ["docker", "run"] and "console-user" in argv]


def test_install_creates_the_first_super_administrator_and_turns_console_login_on(machine):
    out: list[str] = []
    assert installer(machine, out).run() == 0
    [seed] = console_seeds(machine)
    image_at = next(i for i, a in enumerate(seed) if a.startswith(IMAGE + ":"))
    assert seed[image_at:] == [f"{IMAGE}:v4.1.0", "console-user", "seed", "--email", "jooservices@gmail.com",
                               "--role", "super_admin", "--password-file", "/run/aiflow4-console/super-admin-password"]
    console_dir = machine.p("/etc/aiflow/secrets/console")
    assert f"{console_dir}:/run/aiflow4-console" in seed and stat.S_IMODE(console_dir.stat().st_mode) == 0o700
    # after the migration (the table exists) and before the deploy (the gate starts with a user to sign in)
    assert (index(machine, lambda a: a[:2] == ["docker", "run"] and a[-1] == "migrate")
            < index(machine, lambda a: "console-user" in a)
            < index(machine, lambda a: Path(a[0]).name == "aiflow4-deploy" and a[1:] == ["v4.1.0"]))
    assert sec.read_env(machine.p("/etc/aiflow/secrets/control/aiflow4.env"))["AIFLOW4_CONSOLE_LOGIN"] == "1"
    assert any("Super Administrator jooservices@gmail.com" in line and "super-admin-password" in line for line in out)
    assert any("https://aiflow.example.com/login" in line for line in out)


def test_rerun_remembers_the_admin_email_and_keeps_an_owner_choice_for_login(machine):
    assert installer(machine, []).run() == 0
    app_env = machine.p("/etc/aiflow/secrets/control/aiflow4.env")
    sec.update_env(app_env, force={"AIFLOW4_CONSOLE_LOGIN": "0"})
    assert installer(machine, [], admin_email="").run() == 0  # no --admin-email on a rerun
    assert [s[s.index("--email") + 1] for s in console_seeds(machine)] == ["jooservices@gmail.com"] * 2
    assert sec.read_env(app_env)["AIFLOW4_CONSOLE_LOGIN"] == "0"


def test_first_install_without_an_admin_email_stops_before_any_change(machine):
    out: list[str] = []
    assert installer(machine, out, admin_email="").run() == ci.EXIT_USAGE
    assert "--admin-email is required" in "\n".join(out) and machine.calls == []
    assert installer(machine, out, admin_email="not an email").run() == ci.EXIT_USAGE


def test_a_failed_admin_seed_stops_before_the_deploy(machine, monkeypatch):
    real = machine.run

    def run(argv, **kw):
        if "console-user" in argv:
            machine.calls.append((list(argv), {}, None))
            return subprocess.CompletedProcess(argv, 2, "", "cannot open the password file")
        return real(argv, **kw)

    monkeypatch.setattr(machine, "run", run)
    out: list[str] = []
    assert installer(machine, out).run() == ci.EXIT_UNHEALTHY
    assert not [a for a, _, _ in machine.calls if Path(a[0]).name == "aiflow4-deploy" and a[1:] == ["v4.1.0"]]
    assert "could not create the first Super Administrator" in "\n".join(out)


def test_rerun_with_a_new_domain_keeps_secrets_but_updates_the_urls(machine):
    assert installer(machine, []).run() == 0
    before = sec.read_env(machine.p("/etc/aiflow/secrets/control/aiflow4.env"))
    assert installer(machine, [], domain="cc.example.org").run() == 0
    after = sec.read_env(machine.p("/etc/aiflow/secrets/control/aiflow4.env"))
    assert after["AIFLOW4_PUBLIC_URL"] == "https://cc.example.org"
    assert {k: v for k, v in after.items() if "URL" not in k} == {k: v for k, v in before.items() if "URL" not in k}


# ---- AC5: upgrade ----

def test_newer_version_upgrades_with_check_migration_deploy_and_health(machine):
    assert installer(machine, [], version="v4.1.0").run() == 0
    machine.migrate_output = "applied: ['031_new']"
    calls_before = len(machine.calls)
    out: list[str] = []
    assert installer(machine, out, version="v4.2.0").run() == 0
    upgrade = machine.calls[calls_before:]
    argvs = [argv for argv, _, _ in upgrade]
    check = next(i for i, a in enumerate(argvs) if "upgrade-check" in a)
    migrate = next(i for i, a in enumerate(argvs) if a[:2] == ["docker", "run"] and a[-1] == "migrate")
    deploy = next(i for i, a in enumerate(argvs) if Path(a[0]).name == "aiflow4-deploy" and a[1:] == ["v4.2.0"])
    assert check < migrate < deploy
    assert argvs[check][-4:] == ["--target", "v4.2.0", "--current", "v4.1.0"]
    assert f"{IMAGE}:v4.2.0" in argvs[migrate]
    assert upgrade[deploy][1]["AIFLOW4_SKIP_UPGRADE_CHECK"] == "1"  # already checked before the migration
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.2.0"
    assert all(machine.containers[f"aiflow4-{s}"]["image"] == f"{IMAGE}:v4.2.0" for s in ci.APP_SERVICES)
    assert "applied: ['031_new']" in out
    assert out[-1].startswith("AI Flow control plane v4.2.0 is running and healthy")


def test_blocked_pre_upgrade_check_keeps_the_running_release(machine):
    assert installer(machine, [], version="v4.1.0").run() == 0
    machine.check_rc = 1
    out: list[str] = []
    assert installer(machine, out, version="v4.2.0").run() == ci.EXIT_BLOCKED
    assert "blocked v4.2.0; the control plane keeps v4.1.0" in out[-1]
    assert not [a for a, _, _ in machine.calls if a[:2] == ["docker", "run"] and a[-1] == "migrate"
                and f"{IMAGE}:v4.2.0" in a]
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"


# ---- AC6: rollback ----

def test_failed_health_after_upgrade_rolls_back_to_the_previous_release(machine):
    assert installer(machine, [], version="v4.1.0").run() == 0
    machine.broken["v4.2.0"] = {"control-api-a"}
    calls_before = len(machine.calls)
    out: list[str] = []
    assert installer(machine, out, version="v4.2.0").run() == ci.EXIT_UNHEALTHY
    deploys = [(argv, env) for argv, env, _ in machine.calls[calls_before:] if Path(argv[0]).name == "aiflow4-deploy"]
    assert [a[1:] for a, _ in deploys] == [["--stage-only", "v4.2.0"], ["v4.2.0"], ["v4.1.0"]]
    assert deploys[-1][1]["AIFLOW4_SKIP_UPGRADE_CHECK"] == "1"  # the older release lacks the newer migrations
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"
    assert all(machine.containers[f"aiflow4-{s}"]["image"] == f"{IMAGE}:v4.1.0" for s in ci.APP_SERVICES)
    assert "control-api-a: container aiflow4-control-api-a is unhealthy" in out[-1]
    assert "rolled back to v4.1.0, which is healthy" in out[-1]


def test_failed_deploy_rolls_back_too(machine):
    assert installer(machine, [], version="v4.1.0").run() == 0
    machine.deploy_rc["v4.2.0"] = 5
    out: list[str] = []
    assert installer(machine, out, version="v4.2.0").run() == ci.EXIT_UNHEALTHY
    assert "rolled back to v4.1.0" in out[-1]
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"


def test_simulated_failure_for_ci_only_affects_the_target(machine, monkeypatch):
    assert installer(machine, [], version="v4.1.0").run() == 0
    monkeypatch.setenv(ci.SIMULATE_ENV, "control-api-a")
    out: list[str] = []
    assert installer(machine, out, version="v4.2.0").run() == ci.EXIT_UNHEALTHY
    assert "control-api-a: simulated failure" in out[-1] and "rolled back to v4.1.0, which is healthy" in out[-1]


def test_rollback_that_is_unhealthy_too_exits_6(machine):
    assert installer(machine, [], version="v4.1.0").run() == 0
    machine.broken = {"v4.2.0": {"openobserve"}, "v4.1.0": {"openobserve"}}
    out: list[str] = []
    assert installer(machine, out, version="v4.2.0").run() == ci.EXIT_ROLLBACK_FAILED
    assert "rollback to v4.1.0 is unhealthy too: openobserve" in out[-1]


def test_owner_rollback_returns_to_the_previous_staged_release(machine):
    assert installer(machine, [], version="v4.0.3").run() == 0
    assert installer(machine, [], version="v4.1.0").run() == 0
    assert installer(machine, [], version="v4.2.0").run() == 0
    calls_before = len(machine.calls)
    out: list[str] = []
    rb = installer(machine, out, version="", domain="", rollback=True)
    assert rb.run() == 0
    assert rb.steps == ["preflight", "deploy", "health"]
    deploys = [(a, e) for a, e, _ in machine.calls[calls_before:] if Path(a[0]).name == "aiflow4-deploy"]
    assert [a[1:] for a, _ in deploys] == [["v4.1.0"]] and deploys[0][1]["AIFLOW4_SKIP_UPGRADE_CHECK"] == "1"
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.1.0"
    assert out[-1] == "rolled back to v4.1.0; the control plane is healthy. Command Center: https://aiflow.example.com/cc"
    # and once more to the release before it
    assert installer(machine, [], version="", domain="", rollback=True).run() == 0
    assert machine.p("/srv/aiflow/control/releases/CURRENT").read_text().strip() == "v4.0.3"


def test_owner_rollback_without_an_earlier_release_changes_nothing(machine):
    assert installer(machine, [], version="v4.1.0").run() == 0
    calls_before = len(machine.calls)
    out: list[str] = []
    assert installer(machine, out, version="", domain="", rollback=True).run() == ci.EXIT_USAGE
    assert "no earlier release is staged" in out[-1]
    assert machine.calls[calls_before:] == []


def test_installing_an_older_version_skips_the_check_and_the_migration(machine):
    assert installer(machine, [], version="v4.2.0").run() == 0
    calls_before = len(machine.calls)
    assert installer(machine, [], version="v4.1.0").run() == 0
    argvs = [(a, e) for a, e, _ in machine.calls[calls_before:]]
    assert not [a for a, _ in argvs if a[:2] == ["docker", "run"]]
    deploy = next(e for a, e in argvs if Path(a[0]).name == "aiflow4-deploy" and a[1:] == ["v4.1.0"])
    assert deploy["AIFLOW4_SKIP_UPGRADE_CHECK"] == "1"


# ---- AC7: secrets stay out of output, packages and argv ----

def test_secret_values_never_reach_the_output(machine):
    out: list[str] = []
    assert installer(machine, out).run() == 0
    url = sec.read_env(machine.p("/etc/aiflow/secrets/control/aiflow4.env"))["AIFLOW4_DATABASE_URL"]
    machine.migrate_output = f"applied: nothing (connected to {url})"
    machine.down = {"gatus"}
    assert installer(machine, out).run() == ci.EXIT_UNHEALTHY
    text = "\n".join(out)
    values = all_secret_values(machine)
    assert values and not [v for v in values if v in text]
    assert "postgresql://aiflow:***@aiflow4-postgres" in text
    for argv, _, stdin in machine.calls:  # no secret in any command line
        assert not [v for v in values if any(v in a for a in argv)], argv


def test_release_package_and_image_carry_no_secret():
    workflow = (ROOT / ".github/workflows/release-v4.yml").read_text()
    assert "aiflow pyproject.toml uv.lock prompts deploy" in workflow
    leaks = re.compile(r"(PASSWORD|TOKEN|SECRET|O2_BASIC)[ \t]*[:=][ \t]*['\"]?([A-Za-z0-9+/_-]{8,})")
    fixtures = re.compile(r"smoke|example|test|placeholder")  # CI-only keys and documentation samples
    for top in ("aiflow/v4", "prompts", "deploy/v4"):
        for path in (ROOT / top).rglob("*"):
            if path.is_file() and path.suffix in {".env", ".yaml", ".yml", ".sh", "", ".j2", ".md", ".json"}:
                found = [m[2] for m in leaks.finditer(path.read_text(errors="ignore")) if not fixtures.search(m[2])]
                assert not found, path
    dockerfile = (ROOT / "deploy/v4/control/Dockerfile").read_text()
    assert "/etc/aiflow" not in dockerfile and ".env" not in dockerfile
    infra = yaml.safe_load((ROOT / "deploy/v4/control/infra.yaml").read_text())
    assert infra["services"]["postgres"]["env_file"] == ["/etc/aiflow/secrets/control/postgres.env"]
    assert "/etc/aiflow/secrets/control/openobserve.env" in infra["services"]["openobserve"]["env_file"]


def test_secret_set_reads_hidden_input_and_stores_it_0600(machine):
    out: list[str] = []
    layout = ci.Layout(machine.root)
    prompts: list[str] = []

    def prompt(text):
        prompts.append(text)
        return "atl-token-123456"

    assert ci.secret_command("set", "JIRA_API_TOKEN", False, host=machine, layout=layout, out=out.append,
                             err=out.append, prompt=prompt) == 0
    assert prompts == ["JIRA_API_TOKEN (input hidden): ", "JIRA_API_TOKEN again: "]
    path = machine.p("/etc/aiflow/secrets/control/aiflow4.env")
    assert sec.read_env(path)["JIRA_API_TOKEN"] == "atl-token-123456"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "atl-token-123456" not in "\n".join(out)
    assert out == [f"saved JIRA_API_TOKEN in {path} (0600)"]
    assert machine.calls == []  # nothing installed yet, so nothing to restart


def test_secret_set_rejects_mismatch_unknown_names_and_spaces(machine, monkeypatch):
    layout = ci.Layout(machine.root)
    answers = iter(["one-value", "other-value"])
    out: list[str] = []
    assert ci.secret_command("set", "GH_TOKEN", False, host=machine, layout=layout, out=out.append, err=out.append,
                             prompt=lambda _: next(answers)) == ci.EXIT_USAGE
    assert "differ" in out[-1]
    assert ci.secret_command("set", "AIFLOW4_DATABASE_URL", False, host=machine, layout=layout, out=out.append,
                             err=out.append, prompt=lambda _: "x") == ci.EXIT_USAGE
    assert "unknown secret" in out[-1]
    monkeypatch.setattr("sys.stdin", io.StringIO("ghp one-secret\n"))
    assert ci.secret_command("set", "GH_TOKEN", True, host=machine, layout=layout, out=out.append,
                             err=out.append) == ci.EXIT_USAGE
    assert not machine.p("/etc/aiflow/secrets/control/github.env").exists()
    assert not [line for line in out if "one-value" in line or "one-secret" in line]


def test_secret_set_from_a_pipe_restarts_the_running_control_plane(machine, monkeypatch):
    assert installer(machine, []).run() == 0
    ids = {s: machine.containers[f"aiflow4-{s}"]["id"] for s in ci.APP_SERVICES}
    monkeypatch.setattr("sys.stdin", io.StringIO("me@example.com\n"))
    out: list[str] = []
    calls_before = len(machine.calls)
    assert ci.secret_command("set", "JIRA_EMAIL", True, host=machine, layout=ci.Layout(machine.root),
                             out=out.append, err=out.append) == 0
    ups = [a for a, _, _ in machine.calls[calls_before:] if a[:2] == ["docker", "compose"]]
    assert [a[-1] for a in ups] == list(ci.APP_SERVICES)
    assert "me@example.com" not in "\n".join(out)
    assert {s: machine.containers[f"aiflow4-{s}"]["id"] for s in ci.APP_SERVICES} == ids  # fake: same image
    monkeypatch.setattr("sys.stdin", io.StringIO("ghp_example_token\n"))
    assert ci.secret_command("set", "GH_TOKEN", True, host=machine, layout=ci.Layout(machine.root),
                             out=out.append, err=out.append) == 0
    assert installer(machine, []).run() == 0
    login = [(a, s) for a, _, s in machine.calls if a[:2] == ["docker", "login"]]
    assert login == [(["docker", "login", "ghcr.io", "--username", "aiflow4", "--password-stdin"], "ghp_example_token\n")]


def test_secret_cli_has_no_argument_for_the_value(monkeypatch):
    seen = {}
    monkeypatch.setattr(ci, "secret_command", lambda action, name, stdin: seen.update(a=action, n=name, s=stdin) or 0)
    assert cli.main(["secret", "set", "JIRA_API_TOKEN"]) == 0
    assert seen == {"a": "set", "n": "JIRA_API_TOKEN", "s": False}
    with pytest.raises(SystemExit):
        cli.main(["secret", "set", "JIRA_API_TOKEN", "the-token"])


def test_secret_list_names_only(machine):
    sec.set_owner_secret(machine.p("/etc/aiflow/secrets/control"), "GH_TOKEN", "ghp_value_1234")
    out: list[str] = []
    assert ci.secret_command("list", None, False, host=machine, layout=ci.Layout(machine.root), out=out.append) == 0
    assert out[0].startswith("JIRA_EMAIL: not set") and out[2].startswith("GH_TOKEN: set")
    assert "ghp_value_1234" not in "\n".join(out)


# ---- AC8, AC9: pinned server-installer and SI_* ----

def test_server_installer_is_the_pinned_cli_with_plan_then_apply(machine, monkeypatch):
    monkeypatch.setenv("SI_PHP_VERSION", "7.4")  # inherited SI_* values never reach server-installer
    monkeypatch.setenv("SI_UNRELATED", "x")
    assert installer(machine, [], stack="php-8.3", vpn="tailscale", otel_collector=True,
                     si=["SI_PHP_MODE=fpm"]).run() == 0
    assert machine.downloads == ["https://github.com/jooservices/server-installer/archive/refs/tags/v1.1.0.tar.gz"]
    si = [(a, e) for a, e, _ in machine.calls if Path(a[0]).name == "server-installer"]
    modules = "packages,timesync,swap,ulimits,docker,docker_group,php,otel_collector,tailscale"
    assert [a for a, _ in si] == [[str(machine.p("/opt/server-installer/v1.1.0/bin/server-installer")), cmd,
                                   "--modules", modules] for cmd in ("plan", "apply")]
    for _, env in si:
        assert {k: v for k, v in env.items() if k.startswith("SI_")} == {"SI_PHP_VERSION": "8.3", "SI_PHP_MODE": "fpm"}
    assert "--preflight" not in str(si)  # v1.1.0 has no such flag; plan shows the preflight result


def test_default_modules_and_no_php_without_a_stack(machine):
    assert installer(machine, []).run() == 0
    plan = next(a for a, _, _ in machine.calls if Path(a[0]).name == "server-installer")
    assert plan[-1] == "packages,timesync,swap,ulimits,docker,docker_group"
    assert machine.p("/opt/server-installer/v1.1.0/bin/server-installer").stat().st_mode & 0o111


def test_pinned_tag_is_shared_with_the_node_installers():
    from aiflow.v4.node.operations import SERVER_INSTALLER_VERSION
    assert SERVER_INSTALLER_VERSION == "v1.1.0"
    assert ci.SERVER_INSTALLER_URL.endswith("/refs/tags/v1.1.0.tar.gz")
    assert "SERVER_INSTALLER_TAG=v1.1.0" in (ROOT / "deploy/v4/bin/aiflow4-bootstrap").read_text()
    # ai-flow carries no copy of the OS modules
    for top in ("aiflow/v4", "deploy/v4"):
        assert not [p for p in (ROOT / top).rglob("module.sh")]


def test_install_cli_parses_every_option(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, opt):
            seen["opt"] = opt

        def run(self):
            return 0

    monkeypatch.setattr(ci, "ControlInstaller", Fake)
    assert cli.main(["install", "control", "--version", "v4.1.0", "--domain", "cc.example.com", "--stack", "php-8.3",
                     "--si", "SI_PHP_MODE=cli", "--vpn", "wireguard", "--otel-collector"]) == 0
    assert seen["opt"] == ci.Options(version="v4.1.0", domain="cc.example.com", stack="php-8.3",
                                     si=["SI_PHP_MODE=cli"], vpn="wireguard", otel_collector=True)
    assert cli.main(["install", "control", "--rollback"]) == 0
    assert seen["opt"].rollback and not seen["opt"].version


def test_cosign_download_is_checked_against_the_pinned_digest(machine, monkeypatch):
    machine.p("/usr/local/bin/cosign").unlink()
    real = machine.download
    monkeypatch.setattr(machine, "download", lambda url, dest: dest.write_bytes(b"not cosign")
                        if "/cosign/" in url else real(url, dest))
    out: list[str] = []
    assert installer(machine, out).run() == ci.EXIT_OS
    assert "cosign checksum verification failed" in out[-1]
    assert not machine.p("/usr/local/bin/cosign").exists()
    import hashlib
    monkeypatch.setitem(ci.COSIGN, "x86_64", ("amd64", hashlib.sha256(b"not cosign").hexdigest()))
    assert installer(machine, []).run() == 0
    assert machine.p("/usr/local/bin/cosign").stat().st_mode & 0o111


# ---- the files the installer uses ----

def test_infra_bundle_runs_the_services_with_pinned_images_and_persistent_data():
    infra = yaml.safe_load((ROOT / "deploy/v4/control/infra.yaml").read_text())
    app = yaml.safe_load((ROOT / "deploy/v4/control/compose.yaml").read_text())
    services = infra["services"]
    assert set(services) == {"postgres", "redis", "traefik", "openobserve", "gatus", "mailpit"}
    for svc in services.values():
        assert re.search(r":v?\d+\.\d+", svc["image"]) and "latest" not in svc["image"]
    assert services["postgres"]["image"].startswith("pgvector/pgvector:")  # memory needs pgvector
    for name in ("postgres", "redis", "openobserve"):
        assert any(v.startswith(f"/srv/aiflow/control/data/{name}:") for v in services[name]["volumes"])
    assert {n["name"] for n in infra["networks"].values()} == {n["name"] for n in app["networks"].values()}
    assert not any(n.get("external") for n in infra["networks"].values())
    for name in ("postgres", "redis"):
        assert "ports" not in services[name]  # internal only
    assert services["mailpit"]["container_name"] == "aiflow4-svc-mailpit"  # AIFLOW4_SMTP_HOST of the app
    assert services["openobserve"]["environment"]["ZO_BASE_URI"] == "/o2"
    assert "./o2.env" in services["openobserve"]["env_file"]
    gatus = yaml.safe_load((ROOT / "deploy/v4/control/gatus.yaml").read_text())
    assert {e["name"] for e in gatus["endpoints"]} >= {"control-api-a", "control-api-b", "postgres", "redis",
                                                        "openobserve", "traefik", "command-center"}


def test_deploy_scripts_support_a_new_control_host():
    deploy = (ROOT / "deploy/v4/bin/aiflow4-deploy").read_text()
    assert "/etc/aiflow/control.env" in deploy
    assert 'if [ "${AIFLOW4_DEPLOY_NODE_AGENT:-1}" != 0 ]; then' in deploy
    assert "https://github.com/jooservices/ai-flow/releases/download/$VERSION/$asset" in deploy
    assert deploy.index("sha256sum --check SHA256SUMS") > deploy.index("releases/download")  # still verified
    assert "aiflow4-control-install" in deploy
    autodeploy = (ROOT / "deploy/v4/bin/aiflow4-autodeploy").read_text()
    assert 'AIFLOW4_SKIP_UPGRADE_CHECK=1 aiflow4-deploy "$CUR" && ROLLED_BACK=yes' in autodeploy
    boot = (ROOT / "deploy/v4/bin/aiflow4-control-install").read_text()
    assert os.access(ROOT / "deploy/v4/bin/aiflow4-control-install", os.X_OK)
    verify, run = boot.index("cosign verify-blob"), boot.index('exec /usr/local/sbin/aiflow4 install control "$@"')
    assert boot.index("sha256sum --check --status SHA256SUMS") < verify < boot.index("uv sync") < run
    assert "COSIGN_VERSION=v3.1.4" in boot and "424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa" in boot
    assert "refs/tags/$VERSION" in boot


@pytest.mark.parametrize("script", ["deploy/v4/bin/aiflow4-control-install", "deploy/v4/bin/aiflow4-deploy",
                                    "deploy/v4/bin/aiflow4-autodeploy", "deploy/v4/ci/control-install-smoke.sh"])
def test_scripts_parse(script):
    subprocess.run(["bash", "-n", str(ROOT / script)], check=True)


def test_bootstrap_rejects_bad_arguments_before_touching_the_host(tmp_path):
    script = ROOT / "deploy/v4/bin/aiflow4-control-install"
    for args in ([], ["--version", "main", "--domain", "d"], ["--cli-only"], ["--cli-only", "v1.2.3", "extra"]):
        r = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, cwd=tmp_path)
        assert r.returncode == 2 and "usage: aiflow4-control-install" in r.stderr, args


def test_docs_describe_install_secrets_upgrade_rollback_pins_and_profiles():
    doc = (ROOT / "docs/v4/control-install.md").read_text()
    for needle in ("aiflow4 install control --version", "--domain", "aiflow4 secret set", "--rollback",
                   "v1.1.0", "plan", "apply", "--preflight", "SI_PHP_VERSION=8.3", "TESTWP", "aiflow-control",
                   "aiflow-node", "/etc/aiflow/secrets", "0600", "22.04", "24.04", "Owner-verified",
                   "control-install-smoke.sh", "10 minutes"):
        assert needle in doc, needle
    readme = (ROOT / "deploy/v4/README.md").read_text()
    topics = readme.split("## Topics", 1)[1].split("\n## ", 1)[0]
    assert "(../../docs/v4/control-install.md)" in topics
