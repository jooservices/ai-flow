"""WordPress site lifecycle through the container runtime, browser notes and the no-WordPress fast path."""
from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from aiflow.v4.adapters.base import RunSpec
from aiflow.v4.worker import containers, wordpress


class FakeRuntime:
    """Records what the worker asks the JAIF-241 broker to run; the real broker enforces the policy."""

    def __init__(self, worktree):
        self.worktree = worktree
        self.commands: list[list[str]] = []
        self.compose_files: list[dict] = []

    def run_supervised(self, argv, cwd):
        self.commands.append(list(argv))
        compose_file = self.worktree / wordpress.COMPOSE_DIR / "compose.yaml"
        assert compose_file.is_file() and str(cwd) == str(self.worktree)
        self.compose_files.append(yaml.safe_load(compose_file.read_text()))
        return 0, "", ""


def _plugin(tmp_path):
    (tmp_path / "sample-plugin.php").write_text("<?php\n/*\nPlugin Name: Sample Plugin\n*/\n")


def test_wordpress_site_starts_through_the_runtime_and_never_calls_podman(monkeypatch, tmp_path):
    _plugin(tmp_path)

    def no_host_process(*args, **kwargs):
        raise AssertionError(f"the worker must not start host processes: {args}")

    monkeypatch.setattr(subprocess, "run", no_host_process)
    monkeypatch.setattr(subprocess, "Popen", no_host_process)
    monkeypatch.setattr(wordpress.WordPressSite, "_wait_for_site", lambda _self: None)
    runtime = FakeRuntime(tmp_path)

    spec = RunSpec(prompt="work", cwd=str(tmp_path), account_home=str(tmp_path), model="m", effort="e",
                   timeout_s=60, control_url="", run_token="", log_path="", extra_env={"WP_CORE_VERSION": "6.8.3"})
    notes = wordpress.start_site(runtime, tmp_path, {"stack": "wordpress"}, "run-123", spec)

    assert spec.extra_env["WP_BASE_URL"].startswith("http://127.0.0.1:")
    assert spec.extra_env["WP_ADMIN_USER"] == "aiflow-admin" and spec.extra_env["WP_ADMIN_PASSWORD"]
    assert notes and spec.prompt.endswith(notes) and "npx playwright test" in notes
    # Every command is a `docker compose` command for the runtime broker (up the services, then seed).
    assert [command[3] for command in runtime.commands] == ["up", "run"]
    assert runtime.commands[0][4:] == ["-d", "db", "web"]
    assert runtime.commands[1][4:6] == ["--rm", "--no-deps"]
    assert all(command[:2] == ["compose", "-f"] for command in runtime.commands)
    document = runtime.compose_files[0]
    assert set(document["services"]) == {"db", "web", "setup"}
    assert document["services"]["web"]["image"] == "wordpress:6.8.3-php8.3-apache"
    assert document["services"]["db"]["image"] == "mariadb:11.4"
    port = spec.extra_env["WP_BASE_URL"].rsplit(":", 1)[1]
    assert document["services"]["web"]["ports"] == [f"127.0.0.1:{port}:80"]
    assert f"{tmp_path}:/var/www/html/wp-content/plugins/{tmp_path.name}:ro" in document["services"]["web"]["volumes"]
    seed = json.loads(next(part for part in runtime.commands[1] if part.startswith("WP_SEED_CONFIG=")).split("=", 1)[1])
    assert seed["kind"] == "plugin" and seed["activation"] == f"{tmp_path.name}/sample-plugin.php"
    assert seed["password"] == spec.extra_env["WP_ADMIN_PASSWORD"]
    assert "activate_plugin" in document["services"]["setup"]["command"][0]
    assert "wp_insert_post" in document["services"]["setup"]["command"][0]
    # The Compose file (it holds the throw-away database passwords) is gone once the site is up.
    assert not (tmp_path / wordpress.COMPOSE_DIR).exists()
    assert wordpress.START_TIMEOUT_S < 90


def test_compose_file_passes_the_runtime_policy(monkeypatch, tmp_path):
    """The generated Compose file is accepted by the real JAIF-241 validator (no privileged options, bind in tree)."""
    _plugin(tmp_path)
    site = wordpress.WordPressSite(tmp_path, "run-1", "6.8.3", FakeRuntime(tmp_path))
    site.host_port = "40000"
    document = site._compose_document()
    compose_file = tmp_path / "compose.yaml"
    compose_file.write_text(yaml.safe_dump(document))
    assert containers._validate_compose_document(document, compose_file, tmp_path, tmp_path) == 3


def test_failed_setup_is_reported_without_credentials_and_removes_the_compose_file(monkeypatch, tmp_path):
    runtime = FakeRuntime(tmp_path)
    site = wordpress.WordPressSite(tmp_path, "run-1", "6.8.3", runtime)

    def failing(argv, cwd):
        return 1, "", f"boom {site.admin_password} {site.db_password} {site.db_root_password}"

    runtime.run_supervised = failing
    with pytest.raises(wordpress.WordPressSiteError) as error:
        site.start()
    for secret in (site.admin_password, site.db_password, site.db_root_password):
        assert secret not in str(error.value)
    assert str(error.value).count("[REDACTED]") == 3
    assert not (tmp_path / wordpress.COMPOSE_DIR).exists()


def test_runtime_refusal_fails_the_site_start(tmp_path):
    class Refusing:
        def run_supervised(self, argv, cwd):
            raise RuntimeError("compose file is outside the policy")

    with pytest.raises(wordpress.WordPressSiteError, match="container runtime"):
        wordpress.WordPressSite(tmp_path, "run-1", "6.8.3", Refusing()).start()


def test_theme_detection_and_bad_core_pin_are_explicit(tmp_path):
    theme = tmp_path / "sample-theme"
    theme.mkdir()
    (theme / "style.css").write_text("/*\nTheme Name: Sample Theme\n*/\n")
    assert wordpress._wp_component(tmp_path) == ("theme", theme, "sample-theme")
    with pytest.raises(wordpress.WordPressSiteError, match="WP_CORE_VERSION"):
        wordpress.WordPressSite(tmp_path, "run-123", "latest", None)


def test_wordpress_run_notes_name_environment_command_browsers_and_browser_folder():
    notes = wordpress.run_notes({"WP_BASE_URL": "http://127.0.0.1:12345", "WP_ADMIN_USER": "admin",
                                 "WP_ADMIN_PASSWORD": "secret"})
    assert "http://127.0.0.1:12345" in notes
    assert "WP_ADMIN_USER" in notes and "WP_ADMIN_PASSWORD" in notes
    assert "npx playwright test" in notes and "project's Playwright config" in notes
    assert "Chromium, Firefox, and WebKit" in notes
    assert "/usr/local/lib/aiflow4/playwright-browser" in notes
    assert "secret" not in notes


def test_project_environment_runtime_values_override_static_values(monkeypatch):
    from aiflow.v4.worker import notes

    monkeypatch.setattr("aiflow.v4.core.settings.read_env_file", lambda _path: {
        "WP_BASE_URL": "http://shared-site", "WP_CORE_VERSION": "6.8.3"})
    merged = notes.project_env("JOOWP", items=[
        {"name": "WP_BASE_URL", "kind": "base_url", "value": "http://shared-site", "runs": ["dev"]},
        {"name": "WP_CORE_VERSION", "kind": "secret", "runs": ["dev"]}],
        runtime_env={"WP_BASE_URL": "http://127.0.0.1:12345",
                     "WP_ADMIN_USER": "aiflow-admin", "WP_ADMIN_PASSWORD": "one-run"})
    assert merged["WP_BASE_URL"] == "http://127.0.0.1:12345"
    assert merged["WP_CORE_VERSION"] == "6.8.3"
    assert merged["WP_ADMIN_PASSWORD"] == "one-run"


def test_non_wordpress_project_starts_no_site_and_no_runtime(monkeypatch, tmp_path):
    def unexpected(*_args, **_kwargs):
        raise AssertionError("non-WordPress runs must not start a site")

    monkeypatch.setattr(wordpress.WordPressSite, "start", unexpected)
    spec = SimpleNamespace(extra_env={"PLAYWRIGHT_BROWSERS_PATH": "/folder"}, prompt="p")
    assert wordpress.start_site(object(), tmp_path, {"stack": "php-8.3"}, "run-1", spec) == ""
    assert spec.extra_env == {"PLAYWRIGHT_BROWSERS_PATH": "/folder"} and spec.prompt == "p"


def test_wordpress_stack_recognizes_string_and_list_project_settings():
    assert wordpress.declares_wordpress("wordpress")
    assert wordpress.declares_wordpress(["php-8.3", "wordpress"])
    assert not wordpress.declares_wordpress(["php-8.3", "playwright"])


def test_broker_runs_supervisor_commands_without_a_descendant_check(monkeypatch, tmp_path):
    """`run_supervised` binds the worker once, then sends a `supervise` request the broker accepts only from uid 0."""
    runtime = containers.ContainerRuntime.__new__(containers.ContainerRuntime)
    runtime.socket_path, runtime.token, runtime.supervisor_bound = tmp_path / "sock", "token", False
    sent = []

    def send(path, payload):
        sent.append(payload)
        return {"ok": True, "code": 0, "stdout": "out", "stderr": ""}

    monkeypatch.setattr(containers, "_send", send)
    assert runtime.run_supervised(["compose", "up"], tmp_path) == (0, "out", "")
    runtime.run_supervised(["compose", "up"], tmp_path)
    assert [item["action"] for item in sent] == ["bind", "supervise", "supervise"]
    assert sent[1]["token"] == "token"
