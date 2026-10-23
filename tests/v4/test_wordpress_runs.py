"""WordPress site lifecycle, browser notes and the no-WordPress fast path."""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from aiflow.v4.worker import wordpress


def test_wordpress_run_uses_isolated_podman_site_for_the_full_test_loop(monkeypatch, tmp_path):
    plugin = tmp_path / "sample-plugin.php"
    plugin.write_text("<?php\n/*\nPlugin Name: Sample Plugin\n*/\n")
    calls = []

    def podman(command, **kwargs):
        calls.append((list(command), kwargs))
        stdout = "127.0.0.1:49101\n" if command[1] == "port" else ""
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(wordpress.subprocess, "run", podman)
    monkeypatch.setattr(wordpress.WordPressSite, "_wait_for_site", lambda _self: None)

    site = wordpress.WordPressSite(tmp_path, "run-123", "6.8.3")
    with site:
        env = site.environment
        assert env["WP_BASE_URL"] == "http://127.0.0.1:49101"
        assert env["WP_ADMIN_USER"] == "aiflow-admin"
        assert env["WP_ADMIN_PASSWORD"]
        assert site.timeout_s == 85 < 90
        # JOOWP-243 can run its complete repeated KernelTest loop while this same DB and URL stay alive.
        for _ in range(20):
            site._run(["exec", site.web, "php", "-r", "run KernelTest iteration"])
        assert not any(command[1] == "rm" for command, _ in calls)

    commands = [command for command, _ in calls]
    assert commands[0][1:3] == ["network", "create"]
    assert any(command[-1] == "mariadb:11.4" for command in commands)
    web = next(command for command in commands if "wordpress:6.8.3-php8.3-apache" in command)
    assert "--network" in web and "--publish" in web and "127.0.0.1::80" in web
    assert any(str(tmp_path) in part and f"/wp-content/plugins/{tmp_path.name}:ro,z" in part for part in web)
    setup = next((command, kwargs) for command, kwargs in calls if command[1:3] == ["exec", "--interactive"])
    config = json.loads(setup[1]["input"])
    assert config["password"] == env["WP_ADMIN_PASSWORD"]
    assert config["kind"] == "plugin" and config["activation"] == f"{tmp_path.name}/sample-plugin.php"
    assert "activate_plugin" in setup[0][-1] and "wp_insert_post" in setup[0][-1]
    assert "WP_CORE_VERSION" not in env  # the core pin is configuration, not a runtime credential
    assert all(command[0] == "podman" for command in commands)
    assert all("docker" not in command for command in commands)
    assert len([command for command in commands if command[1:2] == ["exec"]]) == 21
    assert {"volume", "network"}.issubset({command[1] for command in commands if command[1] in {"volume", "network"}})
    assert sum(command[1:3] == ["rm", "--force"] for command in commands) == 2
    assert any(command[1:3] == ["volume", "rm"] for command in commands)
    assert any(command[1:3] == ["network", "rm"] for command in commands)


def test_theme_detection_and_bad_core_pin_are_explicit(tmp_path):
    theme = tmp_path / "sample-theme"
    theme.mkdir()
    (theme / "style.css").write_text("/*\nTheme Name: Sample Theme\n*/\n")
    assert wordpress._wp_component(tmp_path) == ("theme", theme, "sample-theme")
    with pytest.raises(wordpress.WordPressSiteError, match="WP_CORE_VERSION"):
        wordpress.WordPressSite(tmp_path, "run-123", "latest")


def test_podman_error_does_not_report_site_credentials(monkeypatch, tmp_path):
    site = wordpress.WordPressSite(tmp_path, "run-123", "6.8.3")
    site.started_at = time.monotonic()
    monkeypatch.setattr(wordpress.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(
        returncode=1, stdout="", stderr=f"failure {site.admin_password} {site.db_password} {site.db_root_password}"))
    with pytest.raises(wordpress.WordPressSiteError) as error:
        site._run(["info"])
    for secret in (site.admin_password, site.db_password, site.db_root_password):
        assert secret not in str(error.value)
    assert str(error.value).count("[REDACTED]") == 3


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
    merged = notes.project_env("JOOWP", {"WP_BASE_URL": "http://127.0.0.1:12345",
                                          "WP_ADMIN_USER": "aiflow-admin", "WP_ADMIN_PASSWORD": "one-run"})
    assert merged["WP_BASE_URL"] == "http://127.0.0.1:12345"
    assert merged["WP_CORE_VERSION"] == "6.8.3"
    assert merged["WP_ADMIN_PASSWORD"] == "one-run"


def test_non_wordpress_project_does_not_start_site_or_change_environment(monkeypatch, tmp_path):
    def unexpected_start(*_args, **_kwargs):
        raise AssertionError("non-WordPress runs must not start a site")

    monkeypatch.setattr(wordpress.WordPressSite, "start", unexpected_start)
    base_env = {"PLAYWRIGHT_BROWSERS_PATH": "/folder"}
    with wordpress.run_site(tmp_path, {"stack": "php-8.3"}, "run-1", base_env) as env:
        assert env == base_env
    assert wordpress.run_notes(env) == ""


def test_wordpress_stack_recognizes_string_and_list_project_settings():
    assert wordpress.declares_wordpress("wordpress")
    assert wordpress.declares_wordpress(["php-8.3", "wordpress"])
    assert not wordpress.declares_wordpress(["php-8.3", "playwright"])
