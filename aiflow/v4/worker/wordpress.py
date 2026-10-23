"""Per-run WordPress test sites for projects that declare the `wordpress` stack.

Each site is a Compose project (MariaDB, PHP 8.3 Apache WordPress, and a one-shot seeding service) that the
JAIF-241 container runtime validates and runs; the worker never calls Podman or a Docker daemon. The worktree's
plugin or theme is mounted read-only, seeded through the WordPress PHP runtime, and the runtime removes every
run-owned container, network and volume when the run ends or the worker dies.
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import shutil
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

from ..core.toolchains import PLAYWRIGHT_BROWSERS_DIR

log = logging.getLogger("aiflow4.wordpress")

DATABASE_IMAGE = "mariadb:11.4"
COMPOSE_DIR = ".aiflow-wordpress"
START_TIMEOUT_S = 85
_VERSION = re.compile(r"\d+\.\d+(?:\.\d+)?\Z")

# Run with `php -r` in the WordPress image; the configuration arrives in WP_SEED_CONFIG.
SEED_SCRIPT = r'''$data = json_decode(getenv('WP_SEED_CONFIG'), true, 512, JSON_THROW_ON_ERROR);
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


class WordPressSiteError(RuntimeError):
    """The isolated WordPress environment could not be started."""


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


def _wp_component(worktree: Path) -> tuple[str, Path, str] | None:
    """Find a root or one-level child plugin/theme and return its type, directory and entry point."""
    roots = [worktree]
    roots.extend(path for path in sorted(worktree.iterdir())
                 if path.is_dir() and path.name not in {".git", "vendor", "node_modules", "tests", COMPOSE_DIR})
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
    """One throw-away site, started through the JAIF-241 container runtime.

    `runtime` is the run's `ContainerRuntime`. The worker writes a small Compose file under the worktree and the
    runtime broker validates and runs it (`run_supervised`), so its quotas, container limits and crash cleanup
    own the containers, network and volume. The Compose file is deleted as soon as the site is seeded.
    """

    def __init__(self, worktree: str | Path, run_id: str, core_version: str, runtime, *,
                 timeout_s: int = START_TIMEOUT_S):
        self.worktree = Path(worktree).resolve()
        self.core_version = str(core_version or "").strip()
        if not _VERSION.fullmatch(self.core_version):
            raise WordPressSiteError("WordPress projects must set WP_CORE_VERSION to a numeric core version")
        self.runtime = runtime
        self.run_id = run_id
        self.compose_dir = self.worktree / COMPOSE_DIR
        self.host_port = ""
        self.timeout_s = timeout_s
        self.started_at = 0.0
        self.admin_user = "aiflow-admin"
        self.admin_password = secrets.token_urlsafe(24)
        self.db_password = secrets.token_urlsafe(24)
        self.db_root_password = secrets.token_urlsafe(24)
        self.wp_component = _wp_component(self.worktree)

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

    def _compose(self, *args: str) -> str:
        """Run one `docker compose` command through the runtime broker; credentials never reach error text."""
        self._remaining()
        try:
            code, stdout, stderr = self.runtime.run_supervised(
                ["compose", "-f", str(self.compose_dir / "compose.yaml"), *args], self.worktree)
        except (OSError, ValueError, RuntimeError) as exc:
            raise WordPressSiteError(
                f"The container runtime could not complete WordPress setup ({type(exc).__name__})") from exc
        if code:
            detail = (stderr or stdout or "").strip()[-400:]
            for secret in (self.admin_password, self.db_password, self.db_root_password):
                detail = detail.replace(secret, "[REDACTED]")
            raise WordPressSiteError(f"Container runtime WordPress setup failed (exit {code}): {detail}")
        return (stdout or "").strip()

    def _compose_document(self) -> dict:
        env = {"WORDPRESS_DB_HOST": "db:3306", "WORDPRESS_DB_NAME": "wordpress", "WORDPRESS_DB_USER": "wordpress",
               "WORDPRESS_DB_PASSWORD": self.db_password}
        volumes = ["wpdata:/var/www/html"]
        if self.wp_component:
            kind, root, _activation = self.wp_component
            target = "/var/www/html/wp-content/plugins" if kind == "plugin" else "/var/www/html/wp-content/themes"
            volumes.append(f"{root}:{target}/{root.name}:ro")
        image = f"wordpress:{self.core_version}-php8.3-apache"
        return {"services": {
            "db": {"image": DATABASE_IMAGE,
                   "environment": {"MARIADB_DATABASE": "wordpress", "MARIADB_USER": "wordpress",
                                   "MARIADB_PASSWORD": self.db_password,
                                   "MARIADB_ROOT_PASSWORD": self.db_root_password}},
            "web": {"image": image, "environment": env, "volumes": volumes,
                    "ports": [f"127.0.0.1:{self.host_port}:80"], "depends_on": ["db"]},
            "setup": {"image": image, "environment": env, "volumes": volumes, "entrypoint": ["php", "-r"],
                      "command": [SEED_SCRIPT]},
        }, "volumes": {"wpdata": {}}}

    def start(self) -> "WordPressSite":
        self.started_at = time.monotonic()
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                self.host_port = str(probe.getsockname()[1])
            self.compose_dir.mkdir(exist_ok=True)
            (self.compose_dir / "compose.yaml").write_text(
                yaml.safe_dump(self._compose_document(), sort_keys=False), encoding="utf-8")
            self._compose("up", "-d", "db", "web")
            self._wait_for_site()
            self._install_site()
            return self
        finally:
            # Containers, network and volume belong to the runtime, which removes them when the run ends.
            self.cleanup()

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
        # The setup service runs the PHP runtime of the WordPress image on the site's own volume and network.
        self._compose("run", "--rm", "--no-deps", "-T", "-e", "WP_SEED_CONFIG=" + json.dumps(config), "setup")

    def cleanup(self) -> None:
        shutil.rmtree(self.compose_dir, ignore_errors=True)


def start_site(runtime, worktree: str | Path, project: dict, run_id: str, spec) -> str:
    """Start the run's site inside the container runtime; add its variables and notes to the run spec.

    Returns the notes text. Projects that do not declare `wordpress` do no work and get "".
    """
    if not declares_wordpress(project.get("stacks") or project.get("stack")):
        return ""
    core_version = str(spec.extra_env.get("WP_CORE_VERSION") or spec.extra_env.get("WORDPRESS_CORE_VERSION") or "")
    site = WordPressSite(worktree, run_id, core_version, runtime).start()
    spec.extra_env.update(site.environment)
    notes = run_notes(spec.extra_env)
    spec.prompt += notes
    return notes
