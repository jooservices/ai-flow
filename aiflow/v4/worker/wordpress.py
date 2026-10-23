"""Per-run WordPress test sites for projects that declare the `wordpress` stack.

Each site is a Podman network with a MariaDB container and a PHP 8.3 Apache
WordPress container. The worktree's plugin or theme is mounted read-only, seeded through
the WordPress PHP runtime, and all run-owned resources are removed when the AI process exits.
"""
from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from collections.abc import Iterator
from ..core.toolchains import PLAYWRIGHT_BROWSERS_DIR

log = logging.getLogger("aiflow4.wordpress")

DATABASE_IMAGE = "mariadb:11.4"
START_TIMEOUT_S = 85
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?\Z")
_SAFE_COMPONENT = re.compile(r"[^a-z0-9]+")


class WordPressSiteError(RuntimeError):
    """The isolated WordPress environment could not be started or cleaned up."""


def declares_wordpress(stacks: object) -> bool:
    """Whether a project has the supported WordPress stack, in either stack YAML form."""
    if isinstance(stacks, str):
        values = [stacks]
    else:
        values = list(stacks or []) if isinstance(stacks, (list, tuple, set)) else []
    return "wordpress" in {str(value).strip() for value in values}


def run_notes(env: dict[str, str]) -> str:
    """Instructions appended to WordPress dev and verify runs; no credentials are copied into notes."""
    if not env.get("WP_BASE_URL"):
        return ""
    return ("\nWordPress run environment:\n"
            f"- Seeded, isolated site: {env['WP_BASE_URL']}\n"
            "- Admin credentials are available as WP_ADMIN_USER and WP_ADMIN_PASSWORD.\n"
            "- Run browser checks with `npx playwright test`; it uses the project's Playwright config.\n"
            "- Installed browsers: Chromium, Firefox, and WebKit.\n"
            f"- Shared Playwright browser folder: {env.get('PLAYWRIGHT_BROWSERS_PATH', PLAYWRIGHT_BROWSERS_DIR)}.\n")


def _slug(value: str) -> str:
    return _SAFE_COMPONENT.sub("-", value.lower()).strip("-") or "site"


def _wp_component(worktree: Path) -> tuple[str, Path, str] | None:
    """Find a root or one-level child plugin/theme and return its type, directory and entry point."""
    roots = [worktree]
    roots.extend(path for path in sorted(worktree.iterdir())
                 if path.is_dir() and path.name not in {".git", "vendor", "node_modules", "tests"})
    plugins: list[tuple[Path, str]] = []
    themes: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for php in sorted(root.glob("*.php")):
            try:
                header = php.read_text(encoding="utf-8", errors="ignore")[:12000]
            except OSError:
                continue
            if re.search(r"(?im)^\s*\*?\s*Plugin Name\s*:", header):
                plugins.append((root, php.name))
        style = root / "style.css"
        if style.is_file():
            try:
                header = style.read_text(encoding="utf-8", errors="ignore")[:12000]
            except OSError:
                continue
            if re.search(r"(?im)^\s*\*?\s*Theme Name\s*:", header):
                themes.append(root)
    if plugins:
        root, entry = plugins[0]
        return "plugin", root, f"{root.name}/{entry}"
    if themes:
        return "theme", themes[0], themes[0].name
    # WordPress core projects are served from the image's pinned core and use its bundled default theme.
    return None


class WordPressSite:
    """One throw-away site, isolated by a unique Podman network, volume, and container names."""

    def __init__(self, worktree: str | Path, run_id: str, core_version: str, *, timeout_s: int = START_TIMEOUT_S):
        self.worktree = Path(worktree).resolve()
        self.core_version = str(core_version or "").strip()
        if not _VERSION.fullmatch(self.core_version):
            raise WordPressSiteError("WordPress projects must set WP_CORE_VERSION to a numeric core version")
        self.prefix = f"aiflow-wp-{_slug(run_id)[:24]}-{secrets.token_hex(3)}"
        self.network = self.prefix + "-net"
        self.volume = self.prefix + "-data"
        self.database = self.prefix + "-db"
        self.web = self.prefix + "-web"
        self.host_port = ""
        self.timeout_s = timeout_s
        self.started_at = 0.0
        self.admin_user = "aiflow-admin"
        self.admin_password = secrets.token_urlsafe(24)
        self.db_password = secrets.token_urlsafe(24)
        self.db_root_password = secrets.token_urlsafe(24)
        self.wp_component = _wp_component(self.worktree)
        self._network_created = False
        self._volume_created = False
        self._containers: list[str] = []

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.host_port}"

    @property
    def environment(self) -> dict[str, str]:
        if not self.host_port:
            return {}
        return {"WP_BASE_URL": self.base_url, "WP_ADMIN_USER": self.admin_user,
                "WP_ADMIN_PASSWORD": self.admin_password}

    def _remaining(self) -> int:
        remaining = self.timeout_s - int(time.monotonic() - self.started_at)
        if remaining <= 0:
            raise WordPressSiteError(f"WordPress site startup exceeded {self.timeout_s} seconds")
        return remaining

    def _run(self, args: list[str], *, timeout: int | None = None, input_text: str | None = None) -> str:
        try:
            command_timeout = self._remaining() if timeout is None else timeout
            result = subprocess.run(["podman", *args], capture_output=True, text=True,
                                    timeout=command_timeout, input=input_text)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise WordPressSiteError(f"Podman could not complete WordPress setup ({type(exc).__name__})") from exc
        if result.returncode:
            # Tool output may contain site details; bound it and never include command arguments or credentials.
            detail = (result.stderr or result.stdout or "").strip()[-400:]
            for secret in (self.admin_password, self.db_password, self.db_root_password):
                detail = detail.replace(secret, "[REDACTED]")
            raise WordPressSiteError(f"Podman WordPress setup failed (exit {result.returncode}): {detail}")
        return (result.stdout or "").strip()

    def start(self) -> "WordPressSite":
        self.started_at = time.monotonic()
        try:
            self._run(["network", "create", self.network])
            self._network_created = True
            self._run(["volume", "create", self.volume])
            self._volume_created = True
            self._containers.append(self.database)
            self._run(["run", "--detach", "--name", self.database, "--network", self.network,
                       "--network-alias", "db", "--env", "MARIADB_DATABASE=wordpress",
                       "--env", "MARIADB_USER=wordpress", "--env", f"MARIADB_PASSWORD={self.db_password}",
                       "--env", f"MARIADB_ROOT_PASSWORD={self.db_root_password}", DATABASE_IMAGE])
            web_args = ["run", "--detach", "--name", self.web, "--network", self.network,
                       "--publish", "127.0.0.1::80", "--volume", f"{self.volume}:/var/www/html:Z"]
            if self.wp_component:
                kind, root, _activation = self.wp_component
                target = "/var/www/html/wp-content/plugins" if kind == "plugin" else "/var/www/html/wp-content/themes"
                web_args += ["--volume", f"{root}:{target}/{root.name}:ro,z"]
            web_args += [
                       "--env", "WORDPRESS_DB_HOST=db:3306", "--env", "WORDPRESS_DB_NAME=wordpress",
                       "--env", "WORDPRESS_DB_USER=wordpress", "--env", f"WORDPRESS_DB_PASSWORD={self.db_password}",
                       f"wordpress:{self.core_version}-php8.3-apache"]
            self._containers.append(self.web)
            self._run(web_args)
            mapping = self._run(["port", self.web, "80/tcp"])
            port = re.search(r":(\d+)\s*$", mapping)
            if not port:
                raise WordPressSiteError("Podman did not publish the isolated WordPress site port")
            self.host_port = port.group(1)
            self._wait_for_site()
            self._install_site()
            return self
        except Exception:
            self.cleanup(suppress=True)
            raise

    def _wait_for_site(self) -> None:
        deadline = time.monotonic() + self._remaining()
        url = self.base_url + "/wp-admin/install.php"
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=min(2, max(1, self._remaining()))) as response:
                    if response.status < 500:
                        return
            except (urllib.error.URLError, TimeoutError, OSError):
                pass
            time.sleep(min(1, max(0.05, deadline - time.monotonic())))
        raise WordPressSiteError(f"WordPress site did not become ready within {self.timeout_s} seconds")

    def _install_site(self) -> None:
        kind, _root, activation = self.wp_component or ("", Path(""), "")
        config = {"title": "AI Flow test site", "user": self.admin_user, "password": self.admin_password,
                  "email": "aiflow@example.invalid", "kind": kind, "activation": activation,
                  "host": f"127.0.0.1:{self.host_port}", "port": int(self.host_port),
                  "post_title": "AI Flow seeded post", "post_content": "Seed content for isolated browser tests",
                  "page_title": "AI Flow seeded page", "page_content": "Seed page content"}
        # Use the PHP runtime in the WordPress image for setup. This avoids an extra host CLI or third image pull.
        script = r'''$data = json_decode(stream_get_contents(STDIN), true, 512, JSON_THROW_ON_ERROR);
define('WP_INSTALLING', true);
$_SERVER['HTTP_HOST'] = $data['host'];
$_SERVER['SERVER_NAME'] = '127.0.0.1';
$_SERVER['SERVER_PORT'] = (string) $data['port'];
$_SERVER['REQUEST_URI'] = '/';
$_SERVER['REQUEST_SCHEME'] = 'http';
require '/var/www/html/wp-load.php';
require_once ABSPATH . 'wp-admin/includes/upgrade.php';
if (!is_blog_installed()) {
  wp_install($data['title'], $data['user'], $data['email'], false, '', $data['password']);
}
wp_set_current_user((int) get_user_by('login', $data['user'])->ID);
if ($data['kind'] === 'plugin') {
  require_once ABSPATH . 'wp-admin/includes/plugin.php';
  $activated = activate_plugin($data['activation']);
  if (is_wp_error($activated)) { fwrite(STDERR, $activated->get_error_message()); exit(2); }
} elseif ($data['kind'] === 'theme') {
  switch_theme($data['activation']);
}
foreach ([['post_title', 'post_content', 'post'], ['page_title', 'page_content', 'page']] as $seed) {
  $post = wp_insert_post(['post_title' => $data[$seed[0]], 'post_content' => $data[$seed[1]],
                          'post_type' => $seed[2], 'post_status' => 'publish'], true);
  if (is_wp_error($post)) { fwrite(STDERR, $post->get_error_message()); exit(3); }
}
'''
        self._run(["exec", "--interactive", self.web, "php", "-r", script], input_text=json.dumps(config))

    def cleanup(self, *, suppress: bool = False) -> None:
        errors: list[str] = []
        for name in reversed(self._containers):
            try:
                self._run(["rm", "--force", name], timeout=15)
            except WordPressSiteError as exc:
                errors.append(str(exc))
        if self._volume_created:
            try:
                self._run(["volume", "rm", "--force", self.volume], timeout=15)
            except WordPressSiteError as exc:
                errors.append(str(exc))
        if self._network_created:
            try:
                self._run(["network", "rm", self.network], timeout=15)
            except WordPressSiteError as exc:
                errors.append(str(exc))
        self._containers.clear()
        self._network_created = self._volume_created = False
        if errors and not suppress:
            raise WordPressSiteError("WordPress site cleanup failed: " + "; ".join(errors)[:600])

    def __enter__(self) -> "WordPressSite":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> bool:
        try:
            self.cleanup(suppress=exc is not None)
        except WordPressSiteError:
            if exc is None:
                raise
            log.exception("WordPress site cleanup also failed while handling a run error")
        return False


@contextmanager
def run_site(worktree: str | Path, project: dict, run_id: str,
             base_env: dict[str, str]) -> Iterator[dict[str, str]]:
    """Yield project env plus per-run WordPress credentials, with zero setup for non-WordPress projects."""
    if not declares_wordpress(project.get("stacks") or project.get("stack")):
        yield dict(base_env)
        return
    core_version = str(base_env.get("WP_CORE_VERSION") or base_env.get("WORDPRESS_CORE_VERSION") or "")
    with WordPressSite(worktree, run_id, core_version) as site:
        yield {**base_env, **site.environment}

