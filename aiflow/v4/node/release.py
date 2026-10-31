"""Verify and schedule node self-updates from release and develop channels."""
from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path

from ..worker.client import ControlClient

log = logging.getLogger("aiflow4.node")

_update_tried: dict[str, float] = {}

_DEVELOP_BUILD = re.compile(r"[0-9a-f]{7,40}\Z")

_RELEASE_VERSION = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\Z")

_RELEASE_WORKFLOW_IDENTITY = "https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/{}"

_GITHUB_OIDC_ISSUER = "https://token.actions.githubusercontent.com"

RELEASE_INCOMING_DIR = Path("/srv/aiflow/releases/incoming")

def installed_release() -> str:
    try:
        return Path("/srv/aiflow/current/RELEASE").read_text().strip()
    except OSError:
        return ""

def verify_release_signature(tarball: Path, bundle: Path, version: str) -> tuple[bool, str]:
    """Verify a release bundle from the repository's tag-triggered GitHub Actions workflow."""
    if not _RELEASE_VERSION.fullmatch(version):
        return False, "release version is not a vX.Y.Z tag"
    command = ["cosign", "verify-blob", str(tarball), "--bundle", str(bundle),
               "--certificate-identity", _RELEASE_WORKFLOW_IDENTITY.format(version),
               "--certificate-oidc-issuer", _GITHUB_OIDC_ISSUER]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"cosign verification could not run: {type(exc).__name__}"
    if result.returncode:
        return False, (result.stderr or result.stdout or "cosign rejected the release signature").strip()[:300]
    return True, ""

def _report_release_rejection(api: ControlClient, node: str, version: str, detail: str) -> None:
    try:
        api.call("POST", f"/v1/nodes/{node}/release-failures",
                 {"release_version": version, "detail": detail[:1000]}, retries=1)
    except Exception as exc:  # noqa: BLE001 - installation still fails closed if control is unreachable
        log.error("could not report rejected release %s: %s", version, str(exc)[:200])

def self_update_develop(api: ControlClient, sha: str) -> None:
    """Develop channel (lab only, docs/v4/develop-channel.md): install the control plane's unsigned develop build,
    named by its commit id, with the installer this node already has (never one taken from the new archive)."""
    if sha == installed_release() or time.time() - _update_tried.get(sha, 0) < 600:
        return
    _update_tried[sha] = time.time()
    RELEASE_INCOMING_DIR.mkdir(parents=True, exist_ok=True)
    tar = RELEASE_INCOMING_DIR / f"aiflow4-{sha}.tar.gz"
    response = api.c.get(f"/v1/releases/{sha}.tar.gz", timeout=120)
    if response.status_code != 200:
        log.warning("develop build %s not downloadable: %s", sha, response.status_code)
        return
    tmp = tar.with_suffix(tar.suffix + ".tmp")
    tmp.write_bytes(response.content)
    tmp.replace(tar)
    result = subprocess.run(["systemd-run", "--unit", f"aiflow4-self-update-{sha[:10]}-{int(time.time())}",
                             "--no-block", "--collect", "/usr/local/sbin/aiflow4-develop-install", str(tar), sha],
                            capture_output=True, text=True)
    log.info("develop self-update to %s started: %s %s", sha, result.returncode,
             (result.stderr or result.stdout).strip()[:200])

def self_update(api: ControlClient, version: str, node: str = "unknown") -> None:
    """Download, checksum and verify the control plane's tagged release before scheduling its installer."""
    if version and _DEVELOP_BUILD.fullmatch(version) and os.environ.get("AIFLOW4_CHANNEL", "release") == "develop":
        self_update_develop(api, version)
        return
    if not version or not _RELEASE_VERSION.fullmatch(version):
        if version:
            log.warning("ignoring unversioned release identifier %s", version[:80])
        return
    if version == installed_release() or time.time() - _update_tried.get(version, 0) < 600:
        return
    _update_tried[version] = time.time()
    inc = RELEASE_INCOMING_DIR
    inc.mkdir(parents=True, exist_ok=True)
    tar = inc / f"aiflow4-{version}.tar.gz"
    bundle = inc / f"aiflow4-{version}.tar.gz.sigstore.json"
    try:
        manifest_response = api.c.get(f"/v1/releases/{version}/manifest", timeout=30)
    except Exception as exc:  # noqa: BLE001 - an advertised release without its manifest is a verification failure
        detail = f"release manifest unavailable: {type(exc).__name__}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    if manifest_response.status_code != 200:
        detail = f"release manifest unavailable (HTTP {manifest_response.status_code})"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    try:
        manifest = manifest_response.json()
        expected_sha256 = str(manifest["sha256"])
        tar_url, bundle_url = manifest["tarball"], manifest["signature"]
        if manifest.get("version") != version or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError("release manifest has an invalid version or checksum")
        if tar_url != f"/v1/releases/{version}.tar.gz" or bundle_url != f"/v1/releases/{version}.tar.gz.sigstore.json":
            raise ValueError("release manifest points outside its versioned asset paths")
    except (KeyError, TypeError, ValueError) as exc:
        detail = f"invalid release manifest: {exc}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return

    try:
        response = api.c.get(tar_url, timeout=120)
    except Exception as exc:  # noqa: BLE001 - all required signed assets must exist before installation
        detail = f"release tarball unavailable: {type(exc).__name__}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    if response.status_code != 200:
        detail = f"release tarball unavailable (HTTP {response.status_code})"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    try:
        signature_response = api.c.get(bundle_url, timeout=120)
    except Exception as exc:  # noqa: BLE001 - an advertised release without its signature cannot be installed
        detail = f"release signature bundle unavailable: {type(exc).__name__}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    if signature_response.status_code != 200:
        detail = f"release signature bundle unavailable (HTTP {signature_response.status_code})"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    tar_tmp = tar.with_suffix(tar.suffix + ".tmp")
    bundle_tmp = bundle.with_suffix(bundle.suffix + ".tmp")
    tar_tmp.write_bytes(response.content)
    bundle_tmp.write_bytes(signature_response.content)
    tar_tmp.replace(tar)
    bundle_tmp.replace(bundle)

    if hashlib.sha256(tar.read_bytes()).hexdigest() != expected_sha256:
        detail = "SHA256SUMS checksum does not match the downloaded release tarball"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return
    valid, detail = verify_release_signature(tar, bundle, version)
    if not valid:
        detail = f"cosign signature verification failed: {detail}"
        log.error("rejecting release %s: %s", version, detail)
        _report_release_rejection(api, node, version, detail)
        return

    unit = f"aiflow4-self-update-{version.replace('.', '-')}-{int(time.time())}"
    command = ["systemd-run", "--unit", unit, "--no-block", "--collect",
               "/usr/local/sbin/aiflow4-node-install", str(tar), version, str(bundle), expected_sha256]
    result = subprocess.run(command, capture_output=True, text=True)
    log.info("self-update to %s started: %s %s", version, result.returncode,
             (result.stderr or result.stdout).strip()[:200])
