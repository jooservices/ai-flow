"""The signed release downloads: manifest, checksums, signature bundle and tarball."""
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Callable

from fastapi import Depends, HTTPException
from fastapi.responses import FileResponse


def install_release_asset_routes(app, release_assets: Callable, releases: Callable[[], Path], node_auth: Callable,
                                 release_reader: Callable) -> SimpleNamespace:
    @app.get("/v1/releases/{version}/manifest")
    def release_manifest(version: str, _node: str = Depends(node_auth)) -> dict:
        _, _, _, checksum = release_assets(version)
        base = f"/v1/releases/{version}"
        tar_name = f"aiflow4-{version}.tar.gz"
        return {"version": version, "sha256": checksum, "tarball": f"{base}.tar.gz",
                "signature": f"{base}.tar.gz.sigstore.json", "checksums": f"{base}/SHA256SUMS",
                "filename": tar_name}

    @app.get("/v1/releases/{version}/SHA256SUMS")
    def release_checksums(version: str, _node: str = Depends(node_auth)) -> FileResponse:
        _, _, sums, _ = release_assets(version)
        return FileResponse(sums, media_type="text/plain", filename="SHA256SUMS")

    @app.get("/v1/releases/{version}.tar.gz.sigstore.json")
    def release_signature(version: str, _node: str = Depends(release_reader)) -> FileResponse:
        _, bundle, _, _ = release_assets(version)
        return FileResponse(bundle, media_type="application/json", filename=f"aiflow4-{version}.tar.gz.sigstore.json")

    @app.get("/v1/releases/{sha}.tar.gz")
    def release_file(sha: str, node: str = Depends(release_reader)) -> FileResponse:
        if re.fullmatch(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", sha):
            f, _, _, _ = release_assets(sha)
        elif re.fullmatch(r"[0-9a-f]{7,40}", sha):  # old, already-installed releases remain downloadable
            f = releases() / f"aiflow4-{sha}.tar.gz"
        else:
            raise HTTPException(400, "bad release id")
        if not f.is_file():
            raise HTTPException(404, "no such release")
        return FileResponse(f, media_type="application/gzip")
    return SimpleNamespace(release_manifest=release_manifest, release_checksums=release_checksums,
                           release_signature=release_signature, release_file=release_file)
