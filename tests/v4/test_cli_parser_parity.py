"""Characterize the v4 developer and CI CLI parser."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from aiflow.v4.cli import build_parser


PARSE_CASES = [
    (["version"], {"cmd": "version"}),
    (["migrate"], {"cmd": "migrate"}),
    (["import-inventory"], {"cmd": "import-inventory"}),
    (["control-api"], {"cmd": "control-api", "host": "0.0.0.0", "port": 8080}),
    (["control-api", "--host", "127.0.0.1", "--port", "9090"],
     {"cmd": "control-api", "host": "127.0.0.1", "port": 9090}),
    (["control-loops"], {"cmd": "control-loops"}),
    (["node", "join", "--join-token-file", "/tmp/join.token"],
     {"cmd": "node", "action": "join", "name": None, "join_token_file": "/tmp/join.token",
      "join_token_stdin": False, "control": "", "ip": ""}),
    (["node", "join", "--join-token-stdin", "--name", "node-a", "--control", "https://control.example",
      "--ip", "192.0.2.3"],
     {"cmd": "node", "action": "join", "name": "node-a", "join_token_file": None,
      "join_token_stdin": True, "control": "https://control.example", "ip": "192.0.2.3"}),
    (["node-agent"], {"cmd": "node-agent", "slots": 2, "no_workers": False}),
    (["node-agent", "--slots", "4", "--no-workers"],
     {"cmd": "node-agent", "slots": 4, "no_workers": True}),
    (["canary", "evaluate", "--node", "node-a", "--target", "v4.3.0", "--previous", "v4.2.9",
      "--started-at", "2026-01-02T03:04:05+00:00"],
     {"cmd": "canary", "canary_action": "evaluate", "node": "node-a", "target": "v4.3.0",
      "previous": "v4.2.9", "started_at": "2026-01-02T03:04:05+00:00", "minimum_age_s": 1200,
      "minimum_runs": 5, "maximum_age_s": 3600, "failure_ratio": "2", "minimum_infra_failures": 3}),
    (["canary", "evaluate", "--node", "node-a", "--target", "v4.3.0", "--previous", "v4.2.9",
      "--started-at", "2026-01-02T03:04:05+00:00", "--minimum-age-s", "90", "--minimum-runs", "3",
      "--maximum-age-s", "900", "--failure-ratio", "0.5", "--minimum-infra-failures", "2"],
     {"cmd": "canary", "canary_action": "evaluate", "node": "node-a", "target": "v4.3.0",
      "previous": "v4.2.9", "started_at": "2026-01-02T03:04:05+00:00", "minimum_age_s": 90,
      "minimum_runs": 3, "maximum_age_s": 900, "failure_ratio": "0.5", "minimum_infra_failures": 2}),
    (["canary", "record-rollback", "--node", "node-a", "--target", "v4.3.0", "--previous", "v4.2.9",
      "--commit", "abc123", "--measurements", "{}"],
     {"cmd": "canary", "canary_action": "record-rollback", "node": "node-a", "target": "v4.3.0",
      "previous": "v4.2.9", "commit": "abc123", "measurements": "{}"}),
    (["canary", "record-promotion", "--node", "node-a", "--target", "v4.3.0", "--measurements", "{}"],
     {"cmd": "canary", "canary_action": "record-promotion", "node": "node-a", "target": "v4.3.0",
      "measurements": "{}", "minimum_runs": 5}),
    (["canary", "record-promotion", "--node", "node-a", "--target", "v4.3.0", "--measurements", "{}",
      "--minimum-runs", "8"],
     {"cmd": "canary", "canary_action": "record-promotion", "node": "node-a", "target": "v4.3.0",
      "measurements": "{}", "minimum_runs": 8}),
    (["canary", "retry-tickets"], {"cmd": "canary", "canary_action": "retry-tickets"}),
    (["notify", "--severity", "warning", "--title", "Alert"],
     {"cmd": "notify", "severity": "warning", "title": "Alert", "detail": "", "link": ""}),
    (["notify", "--severity", "critical", "--title", "Alert", "--detail", "Details", "--link",
      "https://example.test/incident"],
     {"cmd": "notify", "severity": "critical", "title": "Alert", "detail": "Details",
      "link": "https://example.test/incident"}),
    (["ba-chat", "setup", "--name", "BA", "--chat-id", "-1001", "--project", "ABC"],
     {"cmd": "ba-chat", "ba_action": "setup", "name": "BA", "chat_id": "-1001", "project": "ABC",
      "token_stdin": False}),
    (["ba-chat", "setup", "--name", "BA", "--chat-id", "-1001", "--project", "ABC", "--token-stdin"],
     {"cmd": "ba-chat", "ba_action": "setup", "name": "BA", "chat_id": "-1001", "project": "ABC",
      "token_stdin": True}),
    (["upgrade-check", "--target", "v4.3.0"],
     {"cmd": "upgrade-check", "target": "v4.3.0", "current": "", "migrations": "", "baseline": ""}),
    (["upgrade-check", "--target", "v4.3.0", "--current", "v4.2.9", "--migrations", "/tmp/migrations",
      "--baseline", "/tmp/baseline"],
     {"cmd": "upgrade-check", "target": "v4.3.0", "current": "v4.2.9", "migrations": "/tmp/migrations",
      "baseline": "/tmp/baseline"}),
    (["worker", "--account", "account-1"], {"cmd": "worker", "account": "account-1"}),
    (["install", "control"],
     {"cmd": "install", "target": "control", "release": "", "domain": "", "rollback": False, "stack": "",
      "si": [], "vpn": "", "otel_collector": False, "health_timeout": 300, "admin_email": ""}),
    (["install", "control", "--version", "v4.3.0", "--domain", "control.example", "--rollback", "--stack",
      "php-8.3", "--si", "PHP_VERSION=8.3", "--si", "WITH_NGINX=true", "--vpn", "tailscale",
      "--otel-collector", "--health-timeout", "45", "--admin-email", "admin@example.test"],
     {"cmd": "install", "target": "control", "release": "v4.3.0", "domain": "control.example", "rollback": True,
      "stack": "php-8.3", "si": ["PHP_VERSION=8.3", "WITH_NGINX=true"], "vpn": "tailscale",
      "otel_collector": True, "health_timeout": 45, "admin_email": "admin@example.test"}),
    (["scale-proof", "--release", "v4.3.0"],
     {"cmd": "scale-proof", "release": "v4.3.0", "out": "", "submit": "",
      "database_url": "postgresql://db.example/aiflow_test", "redis_url": "redis://redis.example/0"}),
    (["scale-proof", "--release", "v4.3.0", "--out", "/tmp/report.json", "--submit",
      "https://control.example", "--database-url", "postgresql://override/db", "--redis-url", "redis://override/1"],
     {"cmd": "scale-proof", "release": "v4.3.0", "out": "/tmp/report.json", "submit": "https://control.example",
      "database_url": "postgresql://override/db", "redis_url": "redis://override/1"}),
    (["secret", "set", "JIRA_API_TOKEN"],
     {"cmd": "secret", "action": "set", "name": "JIRA_API_TOKEN", "stdin": False}),
    (["secret", "set", "JIRA_API_TOKEN", "--stdin"],
     {"cmd": "secret", "action": "set", "name": "JIRA_API_TOKEN", "stdin": True}),
    (["secret", "list"], {"cmd": "secret", "action": "list", "name": None, "stdin": False}),
    (["secret", "list", "JIRA_API_TOKEN"],
     {"cmd": "secret", "action": "list", "name": "JIRA_API_TOKEN", "stdin": False}),
    (["backup", "restore", "--from", "/tmp/backup.aiflow", "--key-file", "/tmp/key"],
     {"cmd": "backup", "backup_action": "restore", "source": "/tmp/backup.aiflow", "key_file": "/tmp/key",
      "s3_endpoint": "", "s3_region": "us-east-1"}),
    (["backup", "restore", "--from", "s3://bucket/backup.aiflow", "--key-file", "/tmp/key", "--s3-endpoint",
      "https://s3.example", "--s3-region", "eu-west-1"],
     {"cmd": "backup", "backup_action": "restore", "source": "s3://bucket/backup.aiflow", "key_file": "/tmp/key",
      "s3_endpoint": "https://s3.example", "s3_region": "eu-west-1"}),
    (["console-user", "seed"],
     {"cmd": "console-user", "action": "seed", "email": None, "role": "super_admin", "name": "",
      "password_file": None}),
    (["console-user", "seed", "--email", "admin@example.test", "--role", "super_admin", "--name", "Admin",
      "--password-file", "/tmp/password"],
     {"cmd": "console-user", "action": "seed", "email": "admin@example.test", "role": "super_admin",
      "name": "Admin", "password_file": "/tmp/password"}),
    (["console-user", "reset-password", "--email", "admin@example.test", "--password-file", "/tmp/password"],
     {"cmd": "console-user", "action": "reset-password", "email": "admin@example.test", "role": "super_admin",
      "name": "", "password_file": "/tmp/password"}),
    (["console-user", "list"],
     {"cmd": "console-user", "action": "list", "email": None, "role": "super_admin", "name": "",
      "password_file": None}),
]


def _plain(value: Any) -> Any:
    if value is argparse.SUPPRESS:
        return "==SUPPRESS=="
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return str(value)


def _parser_spec(parser: argparse.ArgumentParser) -> dict[str, Any]:
    actions = []
    subparsers = {}
    for action in parser._actions:
        choices = list(action.choices) if isinstance(action, argparse._SubParsersAction) else action.choices
        actions.append({
            "option_strings": list(action.option_strings),
            "dest": action.dest,
            "default": _plain(action.default),
            "choices": _plain(choices),
            "required": action.required,
            "nargs": _plain(action.nargs),
            "type_name": getattr(action.type, "__name__", None),
            "help": _plain(action.help),
            "metavar": _plain(action.metavar),
        })
        if isinstance(action, argparse._SubParsersAction):
            subparsers = {name: _parser_spec(child) for name, child in action.choices.items()}
    # format_help() text is not compared: argparse wraps usage differently across Python versions (CI runs 3.14, local 3.12);
    # every option and its help string is already compared through `actions`.
    return {"actions": actions, "subcommands": subparsers}


def _set_parser_environment(monkeypatch) -> None:
    monkeypatch.setenv("AIFLOW4_TEST_DATABASE_URL", "postgresql://db.example/aiflow_test")
    monkeypatch.setenv("AIFLOW4_TEST_REDIS_URL", "redis://redis.example/0")
    monkeypatch.setenv("COLUMNS", "80")  # argparse wraps help text to the terminal width


def test_parser_spec_matches_golden(monkeypatch) -> None:
    _set_parser_environment(monkeypatch)
    actual = _parser_spec(build_parser())
    fixture = Path(__file__).parent / "fixtures" / "cli_parser_spec.json"
    assert actual == json.loads(fixture.read_text(encoding="utf-8"))


def test_parse_results_match_literal_cases(monkeypatch) -> None:
    _set_parser_environment(monkeypatch)
    parser = build_parser()
    assert len(PARSE_CASES) >= 25
    for argv, expected in PARSE_CASES:
        assert vars(parser.parse_args(argv)) == expected, argv
