#!/usr/bin/env bash
# Put the documentation site into a release tarball.
#   add-docs-site.sh VERSION TARBALL.tar.gz
# Builds the static site from docs/guide and the API code of the checked-out tree and adds it to the tarball as
# `docs-site/`, where control serves it at /help/. PYTHON selects the interpreter (CI: `uv run --frozen python`;
# the API reference needs the project dependencies). The tarball is rewritten in place; SHA256SUMS and the
# signature are made after this step.
set -euo pipefail
VERSION="${1:?usage: add-docs-site.sh VERSION TARBALL.tar.gz}"
TARBALL="$(realpath "${2:?usage: add-docs-site.sh VERSION TARBALL.tar.gz}")"
PYTHON="${PYTHON:-python3}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

cd "$ROOT"
$PYTHON -m aiflow.v4.core.docsite build --out "$WORK/docs-site" --version "$VERSION"
test -f "$WORK/docs-site/index.html"
gunzip -c "$TARBALL" > "$WORK/release.tar"
tar -rf "$WORK/release.tar" -C "$WORK" docs-site
gzip -n -c "$WORK/release.tar" > "$TARBALL"
echo "added docs-site/ to $TARBALL"
