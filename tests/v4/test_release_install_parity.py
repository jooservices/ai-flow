"""JAIF-553: the release routes install keeps its registrations after being split into route groups.

`install` runs against a recording fake app; every registration (method, path, handler name, signature, decorator
options) is compared with tests/v4/fixtures/release-install-parity.jsonl, written from the unchanged code.
Behaviour of the helpers is pinned by calling them through the returned namespace."""
import inspect
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from aiflow.v4.control.features.release import routes

GOLDEN = Path(__file__).with_name("fixtures") / "release-install-parity.jsonl"
VERSION = "v1.2.3"


class RecordingApp:
    """Stands in for FastAPI: each route decorator records its registration and returns the handler unchanged."""

    def __init__(self):
        self.registered = []

    def __getattr__(self, method):
        def decorator_factory(path, **options):
            def decorator(handler):
                self.registered.append({"method": method.upper(), "path": path, "handler": handler.__name__,
                                        "signature": re.sub(r"0x[0-9a-f]+", "0x", str(inspect.signature(handler))), "options": options})
                return handler
            return decorator
        return decorator_factory


def node_auth():
    return "node"


def release_reader():
    return "reader"


def _install(tmp_path):
    app = RecordingApp()
    deps = SimpleNamespace(node_auth=node_auth, release_reader=release_reader)
    ctx = SimpleNamespace()
    namespace = routes.install(app, ctx, deps)
    return app, namespace


def test_registrations_match_the_recorded_golden(tmp_path):
    app, _ = _install(tmp_path)
    actual = [json.loads(json.dumps(item, default=str)) for item in app.registered]
    expected = [json.loads(line) for line in GOLDEN.read_text(encoding="utf-8").splitlines()]
    assert actual == expected
    assert len(expected) == 6


def test_the_returned_namespace_exposes_the_same_names(tmp_path):
    _, namespace = _install(tmp_path)
    assert sorted(vars(namespace)) == sorted([
        "current_release", "fleet_release", "tagged_release", "release_assets", "release_manifest",
        "release_checksums", "release_signature", "release_file", "release_failure", "worktrees_sweep"])


@pytest.fixture
def releases(tmp_path, monkeypatch):
    from aiflow.v4.control import api
    monkeypatch.setattr(api, "RELEASES", tmp_path)
    return tmp_path


def _publish(directory: Path, version: str, sums_line: str | None = None, complete: bool = True):
    release = directory / version
    release.mkdir()
    name = f"aiflow4-{version}.tar.gz"
    (release / name).write_bytes(b"tar")
    if complete:
        (release / f"{name}.sigstore.json").write_text("{}")
    (release / "SHA256SUMS").write_text(sums_line if sums_line is not None else f"{'a' * 64}  {name}\n")


def test_tagged_release_refuses_a_bad_version_and_resolves_a_good_one(releases, tmp_path):
    _, namespace = _install(tmp_path)
    for bad in ("1.2.3", "v1.2", "v01.2.3", "../x"):
        with pytest.raises(HTTPException) as failed:
            namespace.tagged_release(bad)
        assert (failed.value.status_code, failed.value.detail) == (400, "bad release version")
    assert namespace.tagged_release(VERSION) == releases / VERSION


def test_release_assets_checks_files_and_the_single_checksum_line(releases, tmp_path):
    _, namespace = _install(tmp_path)
    with pytest.raises(HTTPException) as missing:
        namespace.release_assets(VERSION)
    assert (missing.value.status_code, missing.value.detail) == (404, "no such signed release")
    _publish(releases, VERSION)
    tarball, bundle, sums, checksum = namespace.release_assets(VERSION)
    assert (tarball.name, bundle.name, sums.name, checksum) == (
        f"aiflow4-{VERSION}.tar.gz", f"aiflow4-{VERSION}.tar.gz.sigstore.json", "SHA256SUMS", "a" * 64)
    _publish(releases, "v2.0.0", sums_line="not a checksum line\n")
    with pytest.raises(HTTPException) as incomplete:
        namespace.release_assets("v2.0.0")
    assert (incomplete.value.status_code, incomplete.value.detail) == (404, "release checksum manifest is incomplete")
    _publish(releases, "v3.0.0", complete=False)
    with pytest.raises(HTTPException) as unsigned:
        namespace.release_assets("v3.0.0")
    assert unsigned.value.status_code == 404


def test_manifest_and_file_routes(releases, tmp_path):
    _, namespace = _install(tmp_path)
    _publish(releases, VERSION)
    manifest = namespace.release_manifest(VERSION)
    assert manifest == {"version": VERSION, "sha256": "a" * 64, "tarball": f"/v1/releases/{VERSION}.tar.gz",
                        "signature": f"/v1/releases/{VERSION}.tar.gz.sigstore.json",
                        "checksums": f"/v1/releases/{VERSION}/SHA256SUMS", "filename": f"aiflow4-{VERSION}.tar.gz"}
    assert namespace.release_file(VERSION).path == releases / VERSION / f"aiflow4-{VERSION}.tar.gz"
    (releases / "aiflow4-abcdef1.tar.gz").write_bytes(b"old")
    assert namespace.release_file("abcdef1").path == releases / "aiflow4-abcdef1.tar.gz"
    for bad, status in (("not-a-release", 400), ("abcdef2", 404)):
        with pytest.raises(HTTPException) as failed:
            namespace.release_file(bad)
        assert failed.value.status_code == status
    assert namespace.release_checksums(VERSION).media_type == "text/plain"
    assert namespace.release_signature(VERSION).media_type == "application/json"
