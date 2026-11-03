"""Where a tagged release lives on disk and how its signed files are checked (used by the release routes)."""
import re
from pathlib import Path
from typing import Callable

from fastapi import HTTPException

RELEASE_VERSION = re.compile(r"v(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)")


def tagged_release(releases: Callable[[], Path], version: str) -> Path:
    if not RELEASE_VERSION.fullmatch(version):
        raise HTTPException(400, "bad release version")
    return releases() / version


def release_assets(releases: Callable[[], Path], version: str) -> tuple[Path, Path, Path, str]:
    directory = tagged_release(releases, version)
    tar_name = f"aiflow4-{version}.tar.gz"
    tarball = directory / tar_name
    bundle = directory / f"{tar_name}.sigstore.json"
    sums = directory / "SHA256SUMS"
    if not all(path.is_file() for path in (tarball, bundle, sums)):
        raise HTTPException(404, "no such signed release")
    checksum = re.compile(rf"^([0-9a-f]{{64}})  {re.escape(tar_name)}$")
    matches = [m.group(1) for line in sums.read_text().splitlines() if (m := checksum.fullmatch(line))]
    if len(matches) != 1:
        raise HTTPException(404, "release checksum manifest is incomplete")
    return tarball, bundle, sums, matches[0]
