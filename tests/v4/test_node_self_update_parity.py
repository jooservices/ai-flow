"""Literal characterization of node self-update before it was split into named steps."""
from __future__ import annotations

import hashlib
import json
import logging
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.node import release

_NOW = 1_000_000.0
_TARBALL = b"signed archive bytes"
_BUNDLE = b"signed signature bundle"
_WRONG_TARBALL = b"different archive bytes"
_CHECKSUM = hashlib.sha256(_TARBALL).hexdigest()

_CASES = [
    {"name": "empty-version"},
    {"name": "invalid-version-short", "version": "v4.2"},
    {"name": "invalid-version-word", "version": "nightly"},
    {"name": "develop-build-ignored-on-release-channel", "version": "0123456789"},
    {"name": "develop-install", "version": "0123456789", "channel": "develop"},
    {"name": "develop-already-installed", "version": "0123456789", "channel": "develop",
     "installed": "0123456789"},
    {"name": "develop-recently-tried", "version": "0123456789", "channel": "develop", "tried_at": 999_500},
    {"name": "develop-tried-exactly-ten-minutes-ago", "version": "0123456789", "channel": "develop",
     "tried_at": 999_400},
    {"name": "develop-build-unavailable", "version": "0123456789", "channel": "develop", "tarball": "http_404"},
    {"name": "release-already-installed", "version": "v4.2.1", "installed": "v4.2.1"},
    {"name": "release-recently-tried", "version": "v4.2.1", "tried_at": 999_401},
    {"name": "release-tried-exactly-ten-minutes-ago", "version": "v4.2.1", "tried_at": 999_400},
    {"name": "manifest-request-raises", "version": "v4.2.1", "manifest": "raise"},
    {"name": "manifest-http-404", "version": "v4.2.1", "manifest": "http_404"},
    {"name": "manifest-missing-checksum", "version": "v4.2.1", "manifest": "missing_sha"},
    {"name": "manifest-version-mismatch", "version": "v4.2.1", "manifest": "version_mismatch"},
    {"name": "manifest-checksum-malformed", "version": "v4.2.1", "manifest": "bad_checksum"},
    {"name": "manifest-tarball-outside-version", "version": "v4.2.1", "manifest": "bad_tar_url"},
    {"name": "manifest-signature-outside-version", "version": "v4.2.1", "manifest": "bad_signature_url"},
    {"name": "manifest-not-an-object", "version": "v4.2.1", "manifest": "not_dict"},
    {"name": "tarball-request-raises", "version": "v4.2.1", "tarball": "raise"},
    {"name": "tarball-http-500", "version": "v4.2.1", "tarball": "http_500"},
    {"name": "signature-request-raises", "version": "v4.2.1", "signature": "raise"},
    {"name": "signature-http-404", "version": "v4.2.1", "signature": "http_404"},
    {"name": "checksum-does-not-match", "version": "v4.2.1", "manifest": "wrong_checksum"},
    {"name": "cosign-rejects-signature", "version": "v4.2.1", "cosign": "reject"},
    {"name": "rejection-report-unreachable", "version": "v4.2.1", "manifest": "raise", "report": "raise"},
    {"name": "release-install", "version": "v4.2.1"},
    {"name": "release-install-secondary-version", "version": "v10.0.0"},
]

_PARITY_FILE = Path(__file__).parent / "fixtures" / "node_self_update_parity.jsonl"
_EXPECTED = {row["name"]: row["expected"] for row in map(json.loads, _PARITY_FILE.read_text().splitlines())}


class _Response:
    def __init__(self, content: bytes = b"", payload: dict | list | None = None, status_code: int = 200):
        self.content = content
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


def _manifest_payload(case: dict, version: str) -> dict | list:
    kind = case.get("manifest", "ok")
    if kind == "not_dict":
        return []
    payload = {"version": version, "sha256": _CHECKSUM,
               "tarball": f"/v1/releases/{version}.tar.gz",
               "signature": f"/v1/releases/{version}.tar.gz.sigstore.json"}
    if kind == "missing_sha":
        payload.pop("sha256")
    elif kind == "version_mismatch":
        payload["version"] = "v9.9.9"
    elif kind == "bad_checksum":
        payload["sha256"] = "not-a-sha256"
    elif kind == "wrong_checksum":
        payload["sha256"] = hashlib.sha256(_WRONG_TARBALL).hexdigest()
    elif kind == "bad_tar_url":
        payload["tarball"] = "/v1/releases/v9.9.9.tar.gz"
    elif kind == "bad_signature_url":
        payload["signature"] = "/v1/releases/v9.9.9.tar.gz.sigstore.json"
    return payload


class _HTTP:
    def __init__(self, case: dict):
        self.case = case

    def get(self, url: str, timeout: int):
        version = self.case["version"]
        if url == f"/v1/releases/{version}/manifest":
            kind = self.case.get("manifest", "ok")
            if kind == "raise":
                raise ConnectionError("control offline")
            if kind == "http_404":
                return _Response(status_code=404)
            return _Response(payload=_manifest_payload(self.case, version))
        if url == f"/v1/releases/{version}.tar.gz":
            return self._asset(self.case.get("tarball", "ok"), _TARBALL)
        if url == f"/v1/releases/{version}.tar.gz.sigstore.json":
            return self._asset(self.case.get("signature", "ok"), _BUNDLE)
        raise AssertionError(f"unexpected release URL: {url}")

    def _asset(self, kind: str, content: bytes) -> _Response:
        if kind == "raise":
            raise TimeoutError("asset request timed out")
        if kind == "http_404":
            return _Response(status_code=404)
        if kind == "http_500":
            return _Response(status_code=500)
        return _Response(content=content)


class _API:
    def __init__(self, case: dict):
        self.case = case
        self.c = _HTTP(case)
        self.calls = []

    def call(self, method: str, path: str, body: dict | None = None, **kwargs):
        self.calls.append((method, path, body, kwargs))
        if self.case.get("report") == "raise":
            raise ConnectionError("control unreachable")


class _LogCapture(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record: logging.LogRecord):
        self.messages.append((record.levelname, record.getMessage()))


def _fake_subprocess(case: dict, commands: list):
    def run(command, **kwargs):
        commands.append(command)
        if command[0] == "cosign" and case.get("cosign") == "reject":
            return SimpleNamespace(returncode=1, stdout="", stderr="untrusted signature")
        return SimpleNamespace(returncode=0, stdout="started", stderr="")

    return run


@contextmanager
def _captured_logs():
    logs = _LogCapture()
    old_level, old_propagate = release.log.level, release.log.propagate
    release.log.addHandler(logs)
    release.log.setLevel(logging.DEBUG)
    release.log.propagate = False
    try:
        yield logs
    finally:
        release.log.removeHandler(logs)
        release.log.setLevel(old_level)
        release.log.propagate = old_propagate


def _normalize(value, root: str):
    if isinstance(value, str):
        return value.replace(root, "<incoming>")
    if isinstance(value, dict):
        return {key: _normalize(item, root) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item, root) for item in value]
    return value


def _observe(case: dict, tmp_path, monkeypatch) -> dict:
    version = case.get("version", "")
    api = _API(case)
    commands: list = []
    logs = _LogCapture()
    saved_tried = dict(release._update_tried)
    old_level, old_propagate = release.log.level, release.log.propagate
    monkeypatch.setattr(release, "RELEASE_INCOMING_DIR", tmp_path)
    monkeypatch.setattr(release, "installed_release", lambda: case.get("installed", ""))
    monkeypatch.setattr(release.time, "time", lambda: _NOW)
    monkeypatch.setattr(release.subprocess, "run", _fake_subprocess(case, commands))
    if case.get("channel"):
        monkeypatch.setenv("AIFLOW4_CHANNEL", case["channel"])
    else:
        monkeypatch.delenv("AIFLOW4_CHANNEL", raising=False)
    release._update_tried.clear()
    if "tried_at" in case:
        release._update_tried[version] = case["tried_at"]
    release.log.addHandler(logs)
    release.log.setLevel(logging.DEBUG)
    release.log.propagate = False
    try:
        release.self_update(api, version, "node-7")
    finally:
        release.log.removeHandler(logs)
        release.log.setLevel(old_level)
        release.log.propagate = old_propagate
        release._update_tried.clear()
        release._update_tried.update(saved_tried)
    files = {path.name: path.read_text() for path in sorted(tmp_path.iterdir()) if path.is_file()}
    return _normalize({"reports": api.calls, "commands": commands, "files": files,
                       "logs": logs.messages}, str(tmp_path))


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_self_update_matches_the_pre_refactor_table(case, tmp_path, monkeypatch):
    assert _observe(case, tmp_path, monkeypatch) == _EXPECTED[case["name"]]


def test_reject_release_logs_the_reason_then_reports_it():
    api = _API({"version": "v4.2.1"})
    with _captured_logs() as logs:
        release._reject_release(api, "node-7", "v4.2.1", "untrusted release")
    assert logs.messages == [("ERROR", "rejecting release v4.2.1: untrusted release")]
    assert api.calls == [("POST", "/v1/nodes/node-7/release-failures",
                          {"release_version": "v4.2.1", "detail": "untrusted release"}, {"retries": 1})]


def test_fetch_release_manifest_returns_the_validated_manifest_without_services():
    case = {"version": "v4.2.1"}
    api = _API(case)
    assert release._fetch_release_manifest(api, "node-7", "v4.2.1") == _manifest_payload(case, "v4.2.1")
    assert not api.calls


def test_fetch_release_manifest_rejects_an_unavailable_manifest():
    api = _API({"version": "v4.2.1", "manifest": "http_404"})
    with _captured_logs() as logs:
        assert release._fetch_release_manifest(api, "node-7", "v4.2.1") is None
    assert logs.messages == [("ERROR", "rejecting release v4.2.1: release manifest unavailable (HTTP 404)")]
    assert api.calls[0][2] == {"release_version": "v4.2.1",
                               "detail": "release manifest unavailable (HTTP 404)"}


def test_download_release_assets_verifies_and_returns_the_checksum(tmp_path, monkeypatch):
    case = {"version": "v4.2.1"}
    api = _API(case)
    commands = []
    monkeypatch.setattr(release.subprocess, "run", _fake_subprocess(case, commands))
    tar = tmp_path / "aiflow4-v4.2.1.tar.gz"
    bundle = tmp_path / "aiflow4-v4.2.1.tar.gz.sigstore.json"

    assert release._download_release_assets(api, "node-7", "v4.2.1", _manifest_payload(case, "v4.2.1"),
                                            tar, bundle, _CHECKSUM) == _CHECKSUM
    assert tar.read_bytes() == _TARBALL and bundle.read_bytes() == _BUNDLE
    assert commands[0][:2] == ["cosign", "verify-blob"] and not api.calls


def test_download_release_assets_rejects_a_checksum_mismatch(tmp_path, monkeypatch):
    case = {"version": "v4.2.1"}
    api = _API(case)
    monkeypatch.setattr(release.subprocess, "run", _fake_subprocess(case, []))
    tar = tmp_path / "aiflow4-v4.2.1.tar.gz"
    bundle = tmp_path / "aiflow4-v4.2.1.tar.gz.sigstore.json"
    with _captured_logs() as logs:
        assert release._download_release_assets(api, "node-7", "v4.2.1", _manifest_payload(case, "v4.2.1"),
                                                tar, bundle, "0" * 64) is None
    assert tar.is_file() and bundle.is_file()
    assert logs.messages == [("ERROR", "rejecting release v4.2.1: "
                                         "SHA256SUMS checksum does not match the downloaded release tarball")]


def test_schedule_release_install_runs_the_installer_and_logs_it(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setattr(release.subprocess, "run", _fake_subprocess({}, commands))
    monkeypatch.setattr(release.time, "time", lambda: _NOW)
    tar = tmp_path / "aiflow4-v4.2.1.tar.gz"
    bundle = tmp_path / "aiflow4-v4.2.1.tar.gz.sigstore.json"
    with _captured_logs() as logs:
        release._schedule_release_install("v4.2.1", tar, bundle, _CHECKSUM)
    assert commands == [["systemd-run", "--unit", "aiflow4-self-update-v4-2-1-1000000", "--no-block",
                         "--collect", "/usr/local/sbin/aiflow4-node-install", str(tar), "v4.2.1", str(bundle),
                         _CHECKSUM]]
    assert logs.messages == [("INFO", "self-update to v4.2.1 started: 0 started")]
