"""`aiflow4 install control`: install, re-run, upgrade or roll back the v4 control plane on Ubuntu 22.04/24.04.

    aiflow4 install control --version vX.Y.Z --domain D [--stack php-8.3] [--si SI_NAME=VALUE ...]
                            [--vpn tailscale|wireguard] [--otel-collector]
    aiflow4 install control --rollback
    aiflow4 secret set NAME [--stdin] | aiflow4 secret list

The install runs these steps in order (STEPS); docs/v4/control-install.md is the owner's guide.

- os: the OS layer (packages, timesync, swap, ulimits, Docker with its compose plugin, docker_group, and on request
  php, otel_collector, tailscale or wireguard) is jooservices/server-installer at the pinned tag, called as an
  external CLI: `plan`, then `apply` for the selected modules. Project toolchains reach it only as SI_* variables.
  AI Flow contains no copy of these steps. v1.1.0 has no `--preflight` flag: its preflight metadata is checked when
  each module loads, and `plan` shows the result before `apply` changes anything.
- tools: what AI Flow itself needs on the host (git, curl, the pinned cosign, its deploy scripts, the ai-flow
  checkout that `aiflow4-deploy` reads tags from).
- release: `aiflow4-deploy --stage-only` downloads release vX.Y.Z, checks SHA256SUMS and the cosign signatures of
  the tarball and the GHCR image, and unpacks the compose bundle in /srv/aiflow/build/vX.Y.Z.
- secrets: internal secrets are generated once into /etc/aiflow/secrets/control (0600; see secrets.py).
- infra: the release's deploy/v4/control/infra.yaml (PostgreSQL, Redis, Traefik, OpenObserve, Gatus, Mailpit) with
  `docker compose up -d`, which leaves unchanged containers running. Data lives under /srv/aiflow/control/data.
- migrate: the pre-upgrade check (upgrades only), then `aiflow4 migrate` from the release image (forward only).
- admin: `aiflow4 console-user seed` from the release image creates the first Super Administrator (--admin-email,
  remembered in control.env) once; its generated password lands in /etc/aiflow/secrets/console (0600) and is never
  printed. The secrets step turns console login on (AIFLOW4_CONSOLE_LOGIN=1), so after the install the owner only
  uses the UI (docs/v4/console-login.md).
- deploy: `aiflow4-deploy vX.Y.Z` (control-api a and b one at a time, control-loops, CURRENT). When CURRENT is already
  vX.Y.Z and the containers run that image, only `docker compose up -d` runs, so a second run recreates nothing.
- health: every component; the failed ones are named. After an upgrade a failed check redeploys the previous
  release with `aiflow4-deploy` and checks it again (exit 5 when that worked, 6 when it did not).
- cli: point the host's `aiflow4` at the installed release (aiflow4-control-install --cli-only).
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator
from contextlib import contextmanager

from ..node.operations import SERVER_INSTALLER_VERSION
from . import secrets as sec

RELEASE = re.compile(r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
DOMAIN = re.compile(r"^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
SI_NAME = re.compile(r"^SI_[A-Z0-9_]+$")
SI_VALUE = re.compile(r"^[A-Za-z0-9._:/@,+-]*$")
PHP_STACK = re.compile(r"^php-([0-9]+\.[0-9]+)$")
IMAGE = "ghcr.io/jooservices/ai-flow/control-api"
REPO_URL = "https://github.com/jooservices/ai-flow.git"
SERVER_INSTALLER_URL = f"https://github.com/jooservices/server-installer/archive/refs/tags/{SERVER_INSTALLER_VERSION}.tar.gz"
SUPPORTED_UBUNTU = ("22.04", "24.04")
STEPS = ("preflight", "os", "tools", "release", "secrets", "infra", "migrate", "admin", "deploy", "health", "cli")
ADMIN_EMAIL = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s]{2,63}$")
ADMIN_PASSWORD_FILE = "super-admin-password"  # in Layout.console_secrets; the CLI writes it 0600 and never prints it
OS_MODULES = ("packages", "timesync", "swap", "ulimits", "docker", "docker_group")
VPN_MODULES = ("tailscale", "wireguard")
HOST_PACKAGES = ("ca-certificates", "curl", "git", "tar")
COSIGN_VERSION = "v3.1.4"  # same pin and digests as deploy/v4/bin/aiflow4-bootstrap
COSIGN = {"x86_64": ("amd64", "424e2c97d0b45fd7a6b608a87f090f35633f4c23ece5f092d48bba11aaf4bbfa"),
          "aarch64": ("arm64", "ade83b7aac62e131cacfe96d958948b2150cc9efec46a570a2649b8e27dedad2")}
APP_SERVICES = ("control-api-a", "control-api-b", "control-loops")
HEALTH_COMPONENTS = ("postgres", "redis", "traefik", "openobserve", "gatus",
                     "control-api-a", "control-api-b", "control-loops", "command-center")
SIMULATE_ENV = "AIFLOW4_INSTALL_SIMULATE_UNHEALTHY"  # CI only: report these components unhealthy on the target
EXIT_USAGE, EXIT_OS, EXIT_RELEASE, EXIT_UNHEALTHY, EXIT_ROLLBACK_FAILED, EXIT_BUSY, EXIT_BLOCKED = 2, 3, 4, 5, 6, 8, 9
SOURCE = Path(__file__).resolve().parents[3]  # the release tree this CLI runs from


def version_key(v: str) -> tuple[int, int, int]:
    m = RELEASE.match(v)
    return (int(m[1]), int(m[2]), int(m[3])) if m else (-1, -1, -1)


class InstallError(Exception):
    def __init__(self, message: str, code: int, step: str = "") -> None:
        super().__init__(message)
        self.code, self.step = code, step


@dataclass(frozen=True)
class Layout:
    """Every host path, under `root` (/ on a machine, a temporary directory in the tests)."""
    root: Path = Path("/")

    def p(self, path: str) -> Path:
        return self.root / path.lstrip("/")

    @property
    def secrets(self) -> Path: return self.p("/etc/aiflow/secrets/control")
    @property
    def console_secrets(self) -> Path: return self.p("/etc/aiflow/secrets/console")
    @property
    def control_env(self) -> Path: return self.p("/etc/aiflow/control.env")
    @property
    def control(self) -> Path: return self.p("/srv/aiflow/control")
    @property
    def releases(self) -> Path: return self.control / "releases"
    @property
    def build(self) -> Path: return self.p("/srv/aiflow/build")
    @property
    def infra(self) -> Path: return self.control / "infra"
    @property
    def data(self) -> Path: return self.control / "data"
    @property
    def repo(self) -> Path: return self.control / "repos/ai-flow"
    @property
    def cli(self) -> Path: return self.control / "cli"
    @property
    def sbin(self) -> Path: return self.p("/usr/local/sbin")
    @property
    def cosign(self) -> Path: return self.p("/usr/local/bin/cosign")
    @property
    def server_installer(self) -> Path:
        return self.p(f"/opt/server-installer/{SERVER_INSTALLER_VERSION}/bin/server-installer")
    @property
    def lock(self) -> Path: return self.p("/run/aiflow4-install.lock")
    @property
    def os_release(self) -> Path: return self.p("/etc/os-release")

    def bundle(self, version: str) -> Path:
        return self.build / version / "deploy/v4/control"


class Host:
    """Commands, HTTP probes and time on this machine; the tests replace it."""

    def run(self, argv: list[str], *, env: dict[str, str] | None = None, stdin: str | None = None,
            stream: bool = False, timeout: int = 3600) -> subprocess.CompletedProcess[str]:
        try:
            if stream:  # long steps show their own progress; they print no secrets
                return subprocess.run(argv, env=env, input=stdin, text=True, timeout=timeout, check=False)
            return subprocess.run(argv, env=env, input=stdin, text=True, capture_output=True, timeout=timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return subprocess.CompletedProcess(argv, 127, "", f"{argv[0]}: {type(exc).__name__}")

    def http_ok(self, url: str) -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as r:  # noqa: S310 - fixed local URLs
                return 200 <= r.status < 400
        except (urllib.error.URLError, OSError, ValueError):
            return False

    def download(self, url: str, dest: Path) -> None:
        with urllib.request.urlopen(url, timeout=300) as r, dest.open("wb") as f:  # noqa: S310 - pinned URLs
            shutil.copyfileobj(r, f)

    def euid(self) -> int:
        return os.geteuid()

    def machine(self) -> str:
        return os.uname().machine

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class Options:
    version: str = ""
    domain: str = ""
    rollback: bool = False
    stack: str = ""
    si: list[str] = field(default_factory=list)
    vpn: str = ""
    otel_collector: bool = False
    health_timeout: int = 300
    admin_email: str = ""


class ControlInstaller:
    def __init__(self, opt: Options, host: Host | None = None, layout: Layout | None = None,
                 out: Callable[[str], None] | None = None, err: Callable[[str], None] | None = None,
                 source: Path = SOURCE) -> None:
        self.opt, self.host, self.layout, self.source = opt, host or Host(), layout or Layout(), source
        self.out = out or (lambda m: print(m, flush=True))
        self.err = err or (lambda m: print(m, file=sys.stderr, flush=True))
        self.steps: list[str] = []
        self._step = ""

    # ---- entry points ----

    def run(self) -> int:
        try:
            self.step("preflight")
            self.preflight()
            with self.locked():
                return self.roll_back_on_request() if self.opt.rollback else self.install()
        except InstallError as e:
            self.err(f"aiflow4 install control failed ({e.step or self._step}): {e}")
            return e.code

    def install(self) -> int:
        v, cur = self.opt.version, self.current()
        self.step("os")
        self.prepare_os()
        self.step("tools")
        self.prepare_tools()
        self.step("release")
        self.stage(v)
        self.step("secrets")
        self.write_secrets_and_config()
        self.step("infra")
        self.start_infra(v)
        self.step("migrate")
        checked = self.migrate(v, cur)
        self.step("admin")
        self.seed_admin(v, cur)
        self.step("deploy")
        rc = self.deploy(v, cur, skip_check=checked or self.is_downgrade(v, cur))
        self.step("health")
        failures = self.health(v, simulate=True) if rc == 0 else (
            self.health(v, simulate=True, wait=False) or [("deploy", f"aiflow4-deploy {v} exited {rc}")])
        if failures:
            if RELEASE.match(cur) and cur != v:
                return self.roll_back_after_failure(cur, v, failures)
            raise InstallError(f"health check failed: {describe(failures)}", EXIT_UNHEALTHY, "health")
        self.step("cli")
        self.refresh_cli(v)
        self.out(f"AI Flow control plane {v} is running and healthy. Command Center: http://{self.opt.domain}/cc")
        return 0

    def roll_back_on_request(self) -> int:
        cur = self.current()
        prev = self.previous_release(cur)
        if not prev:
            raise InstallError(f"no earlier release is staged in {self.layout.releases} to roll back to "
                               f"(CURRENT is {cur or 'unset'})", EXIT_USAGE, "preflight")
        self.out(f"rolling back the control plane from {cur} to {prev}")
        self.step("deploy")
        rc = self.run_deploy(prev, skip_check=True)
        self.step("health")
        failures = self.health(prev) if rc == 0 else [("deploy", f"aiflow4-deploy {prev} exited {rc}")]
        if failures:
            raise InstallError(f"rollback to {prev} is unhealthy: {describe(failures)}", EXIT_ROLLBACK_FAILED, "health")
        self.out(f"rolled back to {prev}; the control plane is healthy. Command Center: http://{self.domain()}/cc")
        return 0

    def roll_back_after_failure(self, prev: str, failed: str, failures: list[tuple[str, str]]) -> int:
        self.err(f"{failed} failed its health check: {describe(failures)}; rolling back to {prev}")
        rc = self.run_deploy(prev, skip_check=True)
        again = self.health(prev) if rc == 0 else [("deploy", f"aiflow4-deploy {prev} exited {rc}")]
        if again:
            raise InstallError(f"{failed} failed its health check ({describe(failures)}) and the rollback to {prev} "
                               f"is unhealthy too: {describe(again)}", EXIT_ROLLBACK_FAILED, "health")
        raise InstallError(f"{failed} failed its health check ({describe(failures)}); rolled back to {prev}, "
                           "which is healthy", EXIT_UNHEALTHY, "health")

    # ---- steps ----

    def step(self, name: str) -> None:
        self._step = name
        self.steps.append(name)
        self.out(f"==> [{STEPS.index(name) + 1}/{len(STEPS)}] {name}")

    def preflight(self) -> None:
        o = self.opt
        if o.rollback:
            if o.version:
                raise InstallError("use --rollback alone, or --version with the earlier release", EXIT_USAGE)
        else:
            if not RELEASE.match(o.version or ""):
                raise InstallError("--version must be a published release vX.Y.Z", EXIT_USAGE)
            if not DOMAIN.match(o.domain or ""):
                raise InstallError("--domain must be a host name or IPv4 address, for example aiflow.example.com",
                                   EXIT_USAGE)
            if o.admin_email and not ADMIN_EMAIL.match(o.admin_email.strip()):
                raise InstallError("--admin-email must be an email address, for example owner@example.com", EXIT_USAGE)
            if not self.admin_email():
                raise InstallError("--admin-email is required on the first install: it becomes the first Super "
                                   "Administrator of Command Center", EXIT_USAGE)
        self.si_settings()  # validates --stack and --si
        if o.vpn and o.vpn not in VPN_MODULES:
            raise InstallError("--vpn must be tailscale or wireguard", EXIT_USAGE)
        if self.host.euid() != 0:
            raise InstallError("run as root: sudo aiflow4 install control ...", EXIT_USAGE)
        osr = sec.read_env(self.layout.os_release)
        os_id, os_version = osr.get("ID", "").strip('"'), osr.get("VERSION_ID", "").strip('"')
        if os_id != "ubuntu" or os_version not in SUPPORTED_UBUNTU:
            raise InstallError(f"this installer supports Ubuntu {' and '.join(SUPPORTED_UBUNTU)}; this host is "
                               f"{os_id or 'unknown'} {os_version}".rstrip(), EXIT_USAGE)

    def si_settings(self) -> dict[str, str]:
        settings: dict[str, str] = {}
        if self.opt.stack:
            m = PHP_STACK.match(self.opt.stack)
            if not m:
                raise InstallError("--stack takes php-X.Y; other stacks need no server-installer module", EXIT_USAGE)
            settings |= {"SI_PHP_VERSION": m[1], "SI_PHP_MODE": "cli"}
        for item in self.opt.si:
            name, _, value = item.partition("=")
            if not SI_NAME.match(name) or not _ or not SI_VALUE.match(value):
                raise InstallError(f"--si takes SI_NAME=VALUE (letters, digits and ._:/@,+-): {name or item}",
                                   EXIT_USAGE)
            settings[name] = value
        return settings

    def os_modules(self) -> list[str]:
        modules = list(OS_MODULES)
        if "SI_PHP_VERSION" in self.si_settings():
            modules.append("php")
        if self.opt.otel_collector:
            modules.append("otel_collector")
        if self.opt.vpn:
            modules.append(self.opt.vpn)
        return modules

    def prepare_os(self) -> None:
        self.ensure_server_installer()
        csv = ",".join(self.os_modules())
        # Only the settings chosen on the command line reach server-installer; inherited SI_* values are dropped.
        env = {k: v for k, v in os.environ.items() if not k.startswith("SI_")} | self.si_settings()
        binary = str(self.layout.server_installer)
        for command in ("plan", "apply"):
            r = self.host.run([binary, command, "--modules", csv], env=env, stream=True)
            if r.returncode:
                raise InstallError(f"server-installer {SERVER_INSTALLER_VERSION} `{command} --modules {csv}` exited "
                                   f"{r.returncode}; its output above names the module", EXIT_OS)

    def ensure_server_installer(self) -> None:
        target = self.layout.server_installer.parent.parent
        if self.layout.server_installer.is_file():
            return
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".server-installer-", dir=target.parent) as tmp:
            archive, extract = Path(tmp) / "si.tar.gz", Path(tmp) / "x"
            extract.mkdir()
            try:
                self.host.download(SERVER_INSTALLER_URL, archive)
                with tarfile.open(archive, "r:*") as tar:
                    tar.extractall(extract, filter="data")
            except (OSError, tarfile.TarError, urllib.error.URLError) as exc:
                raise InstallError(f"could not download server-installer {SERVER_INSTALLER_VERSION} "
                                   f"({type(exc).__name__})", EXIT_OS) from exc
            found = [p for p in extract.rglob("bin/server-installer") if p.is_file()]
            if len(found) != 1:
                raise InstallError(f"server-installer {SERVER_INSTALLER_VERSION} archive has no CLI", EXIT_OS)
            if target.exists():
                shutil.rmtree(target)
            os.replace(found[0].parent.parent, target)
        os.chmod(self.layout.server_installer, 0o755)

    def prepare_tools(self) -> None:
        h, lay = self.host, self.layout
        missing = [p for p in HOST_PACKAGES
                   if h.run(["dpkg-query", "-W", "-f=${Status}", p]).stdout.strip() != "install ok installed"]
        if missing:
            env = os.environ | {"DEBIAN_FRONTEND": "noninteractive"}
            if h.run(["apt-get", "update", "-qq"], env=env).returncode or \
                    h.run(["apt-get", "install", "-y", "-qq", *missing], env=env).returncode:
                raise InstallError(f"could not install {' '.join(missing)} with apt-get", EXIT_OS)
        if not lay.cosign.is_file():
            self.install_cosign()
        for script in ("aiflow4-deploy", "aiflow4-control-install"):
            dest, src = lay.sbin / script, self.source / "deploy/v4/bin" / script
            if dest.is_file():
                continue  # later releases install their own copies (aiflow4-deploy)
            if not src.is_file():
                raise InstallError(f"{script} is not installed and this aiflow4 has no release tree; start with "
                                   "deploy/v4/bin/aiflow4-control-install (docs/v4/control-install.md)", EXIT_USAGE)
            lay.sbin.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dest)
            os.chmod(dest, 0o755)
        if not (lay.repo / ".git").exists():
            lay.repo.parent.mkdir(parents=True, exist_ok=True)
            r = h.run(["git", "clone", "--quiet", REPO_URL, str(lay.repo)], env=self.deploy_env())
            if r.returncode:
                raise InstallError(f"could not clone {REPO_URL} (for a private repository run "
                                   "`aiflow4 secret set GH_TOKEN` first)", EXIT_RELEASE)
        token = sec.read_env(lay.secrets / sec.GITHUB_ENV).get("GH_TOKEN")
        if token:  # private GHCR package: docker keeps the login; the token goes through stdin only
            r = h.run(["docker", "login", "ghcr.io", "--username", "aiflow4", "--password-stdin"], stdin=token + "\n")
            if r.returncode:
                raise InstallError("docker login to ghcr.io with GH_TOKEN failed", EXIT_RELEASE)

    def install_cosign(self) -> None:
        arch = COSIGN.get(self.host.machine())
        if not arch:
            raise InstallError(f"no pinned cosign for {self.host.machine()}", EXIT_OS)
        self.layout.cosign.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.layout.cosign.with_name(".cosign.download")
        try:
            self.host.download(f"https://github.com/sigstore/cosign/releases/download/{COSIGN_VERSION}/"
                               f"cosign-linux-{arch[0]}", tmp)
        except (OSError, urllib.error.URLError) as exc:
            raise InstallError(f"could not download cosign {COSIGN_VERSION} ({type(exc).__name__})", EXIT_OS) from exc
        if hashlib.sha256(tmp.read_bytes()).hexdigest() != arch[1]:
            tmp.unlink()
            raise InstallError("cosign checksum verification failed", EXIT_OS)
        os.chmod(tmp, 0o755)
        os.replace(tmp, self.layout.cosign)

    def stage(self, v: str) -> None:
        lay = self.layout
        if (lay.releases / v / f"aiflow4-{v}.tar.gz").is_file() and (lay.bundle(v) / "compose.yaml").is_file() \
                and self.host.run(["docker", "image", "inspect", f"{IMAGE}:{v}"]).returncode == 0:
            self.out(f"release {v} is already staged and verified")
            return
        r = self.host.run([str(lay.sbin / "aiflow4-deploy"), "--stage-only", v], env=self.deploy_env(), stream=True)
        if r.returncode:
            raise InstallError(f"release {v} could not be downloaded or verified (aiflow4-deploy --stage-only exited "
                               f"{r.returncode})", EXIT_RELEASE)

    def write_secrets_and_config(self) -> None:
        lay, d = self.layout, self.opt.domain
        sec.update_env(lay.control_env, header="AI Flow control plane settings (no secrets)", mode=0o644,
                       force={"AIFLOW4_DOMAIN": d, "AIFLOW4_JOIN_CONTROL_URL": f"http://{d}",
                              "AIFLOW4_CONSOLE_ADMIN_EMAIL": self.admin_email()},
                       keep={"AIFLOW4_DEPLOY_NODE_AGENT": "0"})
        changed = sec.ensure_internal(lay.secrets, f"http://{d}", f"http://{d}:8081/")
        for path, mode in ((lay.p("/etc/aiflow/secrets"), 0o700), (lay.p("/etc/aiflow/secrets/github"), 0o700),
                           (lay.p("/etc/aiflow/secrets/jira"), 0o700), (lay.console_secrets, 0o700)):
            sec.ensure_dir(path, mode)
        for path in (lay.control / "repos/aiflow-control", lay.control / "cache/aiflow4-memory", lay.releases):
            path.mkdir(parents=True, exist_ok=True)
        self.out(f"secrets in {lay.secrets} (0600): " + (f"wrote {', '.join(changed)}" if changed else "unchanged"))

    def start_infra(self, v: str) -> None:
        lay, src = self.layout, self.layout.bundle(v)
        lay.infra.mkdir(parents=True, exist_ok=True)
        for source, name in ((src / "infra.yaml", "compose.yaml"), (src / "gatus.yaml", "gatus.yaml"),
                             (self.layout.build / v / "deploy/v4/o2/lab.env", "o2.env")):
            if not source.is_file():
                raise InstallError(f"release {v} has no {source.name}; it predates `aiflow4 install control`",
                                   EXIT_RELEASE)
            dest = lay.infra / name
            if not dest.is_file() or dest.read_bytes() != source.read_bytes():
                shutil.copyfile(source, dest)
        for name in ("postgres", "redis", "openobserve"):
            (lay.data / name).mkdir(parents=True, exist_ok=True)
        r = self.host.run(["docker", "compose", "-f", str(lay.infra / "compose.yaml"), "up", "-d"])
        self.show(r)
        if r.returncode:
            raise InstallError(f"docker compose could not start the control services (exit {r.returncode})",
                               EXIT_UNHEALTHY)
        failures = self.wait(lambda: [f for f in (self.container_health("postgres"), self.container_health("redis"))
                                      if f])
        if failures:
            raise InstallError(f"health check failed: {describe(failures)}", EXIT_UNHEALTHY)

    def migrate(self, v: str, cur: str) -> bool:
        """Run the pre-upgrade check (upgrades) and `aiflow4 migrate`; returns whether the check ran."""
        if self.is_downgrade(v, cur):
            self.out(f"{v} is older than {cur}: migrations are forward only, nothing to apply")
            return False
        base = ["docker", "run", "--rm", "--network", "aiflow4-control", "--env-file", str(self.layout.secrets / sec.APP_ENV)]
        checked = False
        if RELEASE.match(cur) and cur != v:
            args = base + ["-e", f"AIFLOW4_VERSION={v}"]
            check = ["upgrade-check", "--target", v, "--current", cur]
            baseline = self.layout.build / cur / "aiflow/v4/control/migrations"
            if baseline.is_dir():
                args += ["-v", f"{baseline}:/srv/aiflow/upgrade-baseline:ro"]
                check += ["--baseline", "/srv/aiflow/upgrade-baseline"]
            r = self.host.run(args + [f"{IMAGE}:{v}", *check])
            self.show(r)
            if r.returncode:
                raise InstallError(f"the pre-upgrade check blocked {v}; the control plane keeps {cur}", EXIT_BLOCKED)
            checked = True
        r = self.host.run(base + [f"{IMAGE}:{v}", "migrate"])
        self.show(r)
        if r.returncode:
            raise InstallError(f"migrations of {v} failed (exit {r.returncode}); the control plane keeps "
                               f"{cur or 'no release'}", EXIT_UNHEALTHY)
        return checked

    def seed_admin(self, v: str, cur: str) -> None:
        """Create the first Super Administrator once; a rerun finds the user and changes nothing."""
        if self.is_downgrade(v, cur):
            self.out(f"{v} is older than {cur}: the Super Administrator already exists")
            return
        lay, email = self.layout, self.admin_email()
        sec.ensure_dir(lay.console_secrets, 0o700)
        inside = "/run/aiflow4-console"
        r = self.host.run(["docker", "run", "--rm", "--network", "aiflow4-control",
                           "--env-file", str(lay.secrets / sec.APP_ENV), "-v", f"{lay.console_secrets}:{inside}",
                           f"{IMAGE}:{v}", "console-user", "seed", "--email", email, "--role", "super_admin",
                           "--password-file", f"{inside}/{ADMIN_PASSWORD_FILE}"])
        self.show(r)
        if r.returncode:
            raise InstallError(f"could not create the first Super Administrator {email} (exit {r.returncode})",
                               EXIT_UNHEALTHY)
        self.out(f"Super Administrator {email}: sign in at http://{self.opt.domain}/login; the first password is in "
                 f"{lay.console_secrets / ADMIN_PASSWORD_FILE} (0600, written once)")

    def deploy(self, v: str, cur: str, skip_check: bool) -> int:
        if cur == v and self.app_on(v):
            # Same release again: compose recreates a container only when its configuration changed.
            ok = self.compose_app(v)
            self.out(f"control plane already on {v}; containers recreated only if their configuration changed")
            return 0 if ok else 5
        return self.run_deploy(v, skip_check=skip_check)

    def run_deploy(self, v: str, skip_check: bool) -> int:
        env = self.deploy_env() | ({"AIFLOW4_SKIP_UPGRADE_CHECK": "1"} if skip_check else {})
        rc = self.host.run([str(self.layout.sbin / "aiflow4-deploy"), v], env=env, stream=True).returncode
        if rc == EXIT_BLOCKED:
            raise InstallError(f"the pre-upgrade check blocked {v}; the control plane keeps "
                               f"{self.current() or 'its release'}", EXIT_BLOCKED, "deploy")
        return rc

    def compose_app(self, v: str) -> bool:
        env = self.deploy_env() | {"AIFLOW4_IMAGE_REPOSITORY": IMAGE, "AIFLOW4_IMAGE_TAG": v, "AIFLOW4_VERSION": v}
        compose = str(self.layout.bundle(v) / "compose.yaml")
        for svc in APP_SERVICES:  # one at a time, like aiflow4-deploy, so one API always answers
            r = self.host.run(["docker", "compose", "-f", compose, "up", "-d", "--no-deps", svc], env=env)
            self.show(r)
            if r.returncode:
                return False
            if svc != "control-loops" and self.wait(lambda s=svc: [f for f in [self.container_health(s)] if f]):
                return False
        return True

    def health(self, v: str, simulate: bool = False, wait: bool = True) -> list[tuple[str, str]]:
        simulated = [(c, "simulated failure") for c in os.environ.get(SIMULATE_ENV, "").split(",")
                     if simulate and c in HEALTH_COMPONENTS]
        failures = self.wait(lambda: self.check(v)) if wait else self.check(v)
        result = failures + [s for s in simulated if s[0] not in dict(failures)]
        if not result:
            self.out("health: " + ", ".join(f"{c} ok" for c in HEALTH_COMPONENTS))
        return result

    def check(self, v: str) -> list[tuple[str, str]]:
        failures = [f for f in (self.container_health("postgres"), self.container_health("redis")) if f]
        for name, url in (("traefik", "http://127.0.0.1/ping"), ("openobserve", "http://127.0.0.1/o2/healthz"),
                          ("gatus", "http://127.0.0.1:8081/health")):
            if not self.host.http_ok(url):
                failures.append((name, f"{url} does not answer"))
        for svc in APP_SERVICES:
            image = self.inspect(f"aiflow4-{svc}", "{{.Config.Image}}")
            problem = self.container_health(svc) if svc != "control-loops" else (
                "" if self.inspect("aiflow4-control-loops", "{{.State.Running}}") == "true"
                else ("control-loops", "container aiflow4-control-loops is not running"))
            if problem:
                failures.append(problem)
            elif image != f"{IMAGE}:{v}":
                failures.append((svc, f"runs {image or 'no image'} instead of {IMAGE}:{v}"))
        if not (self.host.http_ok("http://127.0.0.1/healthz") and self.host.http_ok("http://127.0.0.1/cc")):
            failures.append(("command-center", "http://127.0.0.1/cc or /healthz does not answer through Traefik"))
        return failures

    def refresh_cli(self, v: str) -> None:
        current = self.layout.cli / "current"
        if current.exists() and current.resolve().name == v:
            return
        r = self.host.run([str(self.layout.sbin / "aiflow4-control-install"), "--cli-only", v], stream=True)
        if r.returncode:
            self.err(f"warning: the aiflow4 command still runs the previous release; rerun "
                     f"`aiflow4-control-install --cli-only {v}` (exit {r.returncode})")

    # ---- helpers ----

    @contextmanager
    def locked(self) -> Iterator[None]:
        self.layout.lock.parent.mkdir(parents=True, exist_ok=True)
        with self.layout.lock.open("w") as f:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise InstallError("another aiflow4 install is running", EXIT_BUSY) from None
            yield

    def current(self) -> str:
        f = self.layout.releases / "CURRENT"
        return f.read_text().strip() if f.is_file() else ""

    def admin_email(self) -> str:
        """--admin-email, else the one a previous install remembered in control.env."""
        return (self.opt.admin_email.strip().lower()
                or sec.read_env(self.layout.control_env).get("AIFLOW4_CONSOLE_ADMIN_EMAIL", ""))

    def domain(self) -> str:
        return sec.read_env(self.layout.control_env).get("AIFLOW4_DOMAIN", "") or "<domain>"

    def previous_release(self, cur: str) -> str:
        staged = [d.name for d in self.layout.releases.glob("v*") if RELEASE.match(d.name)
                  and (d / f"aiflow4-{d.name}.tar.gz").is_file() and version_key(d.name) < version_key(cur)]
        return max(staged, key=version_key, default="")

    @staticmethod
    def is_downgrade(v: str, cur: str) -> bool:
        return bool(RELEASE.match(cur)) and version_key(v) < version_key(cur)

    def deploy_env(self) -> dict[str, str]:
        env = dict(os.environ) | sec.read_env(self.layout.control_env)
        token = sec.read_env(self.layout.secrets / sec.GITHUB_ENV).get("GH_TOKEN")
        return env | ({"GH_TOKEN": token} if token else {})

    def inspect(self, container: str, fmt: str) -> str:
        r = self.host.run(["docker", "inspect", "-f", fmt, container])
        return r.stdout.strip() if r.returncode == 0 else ""

    def container_health(self, name: str) -> tuple[str, str] | None:
        status = self.inspect(f"aiflow4-{name}", "{{.State.Health.Status}}")
        return None if status == "healthy" else (name, f"container aiflow4-{name} is {status or 'missing'}")

    def app_on(self, v: str) -> bool:
        return all(self.inspect(f"aiflow4-{svc}", "{{.Config.Image}}") == f"{IMAGE}:{v}" for svc in APP_SERVICES)

    def wait(self, probe: Callable[[], list[tuple[str, str]]]) -> list[tuple[str, str]]:
        deadline = self.host.monotonic() + self.opt.health_timeout
        while True:
            failures = probe()
            if not failures or self.host.monotonic() >= deadline:
                return failures
            self.host.sleep(5)

    def show(self, r: subprocess.CompletedProcess[str]) -> None:
        """Print captured command output with every secret value masked."""
        text = "\n".join(t.strip() for t in (r.stdout or "", r.stderr or "") if t and t.strip())
        if text:
            self.out(sec.redact(text, sec.secret_values(self.layout.secrets)))


def describe(failures: list[tuple[str, str]]) -> str:
    return "; ".join(f"{name}: {why}" for name, why in failures)


def secret_command(action: str, name: str | None, from_stdin: bool, host: Host | None = None,
                   layout: Layout | None = None, out: Callable[[str], None] = print,
                   err: Callable[[str], None] | None = None, prompt: Callable[[str], str] | None = None) -> int:
    """`aiflow4 secret set NAME` / `aiflow4 secret list`; values are never printed."""
    host, layout = host or Host(), layout or Layout()
    err = err or (lambda m: print(m, file=sys.stderr))
    if host.euid() != 0:
        err("run as root: sudo aiflow4 secret ...")
        return EXIT_USAGE
    if action == "list":
        for key, present in sec.owner_secret_status(layout.secrets).items():
            out(f"{key}: {'set' if present else 'not set'} - {sec.OWNER_SECRETS[key][1]}")
        return 0
    if not name:
        err(f"name the secret: aiflow4 secret set NAME ({', '.join(sec.OWNER_SECRETS)})")
        return EXIT_USAGE
    try:
        if name not in sec.OWNER_SECRETS:
            raise ValueError(f"unknown secret {name}; choose one of {', '.join(sec.OWNER_SECRETS)}")
        value = sec.read_hidden(name, from_stdin=from_stdin, **({"prompt": prompt} if prompt else {}))
        path = sec.set_owner_secret(layout.secrets, name, value)
    except (ValueError, EOFError, KeyboardInterrupt) as e:
        err(str(e) or "cancelled; nothing was saved")
        return EXIT_USAGE
    del value
    out(f"saved {name} in {path} (0600)")
    if sec.OWNER_SECRETS[name][0] == sec.APP_ENV:
        installer = ControlInstaller(Options(), host=host, layout=layout, out=out, err=err)
        cur = installer.current()
        if RELEASE.match(cur) and installer.app_on(cur):
            out(f"restarting control-api and control-loops of {cur} with the new value")
            if not installer.compose_app(cur):
                err("the control containers did not come back healthy; run `aiflow4 install control` again")
                return EXIT_UNHEALTHY
    return 0
