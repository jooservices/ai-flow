#!/usr/bin/env bash
# Security scans for pull requests and releases (docs/v4/security.md): dependencies (uv.lock), the repository
# (secrets, misconfiguration) and, when an image is given, the container image. A finding at or above
# SECURITY_SEVERITY_THRESHOLD (default HIGH) fails the run. The results are written as JSON to the output directory so
# the release security report can list them.
#
# usage: SECURITY_SEVERITY_THRESHOLD=HIGH security-scan.sh [output-dir] [image]
set -uo pipefail

OUT="${1:-security-report}"
IMAGE="${2:-}"
THRESHOLD="${SECURITY_SEVERITY_THRESHOLD:-HIGH}"
TRIVY_IMAGE="${TRIVY_IMAGE:-aquasec/trivy:0.58.1}"

SEVERITIES="$(python3 -m aiflow.v4.core.security_report severities "$THRESHOLD")" || exit 2
mkdir -p "$OUT"
OUT="$(cd "$OUT" && pwd)"
CACHE="${TRIVY_CACHE_DIR:-$HOME/.cache/trivy}"
mkdir -p "$CACHE"

trivy() {
  docker run --rm -v "$PWD:/src:ro" -v "$OUT:/out" -v "$CACHE:/root/.cache/trivy" "$@"
}

status=0
echo "== blocking severities: $SEVERITIES =="

echo "== dependencies (uv.lock) =="
trivy -w /src "$TRIVY_IMAGE" fs --scanners vuln --severity "$SEVERITIES" --exit-code 1 \
  --format json --output /out/dependencies.json uv.lock || status=1

echo "== repository (secrets and misconfiguration) =="
trivy -w /src "$TRIVY_IMAGE" fs --scanners secret,misconfig --severity "$SEVERITIES" --exit-code 1 \
  --skip-dirs .venv --skip-dirs .git --format json --output /out/repository.json . || status=1

if [ -n "$IMAGE" ]; then
  echo "== container image $IMAGE =="
  docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v "$OUT:/out" -v "$CACHE:/root/.cache/trivy" \
    "$TRIVY_IMAGE" image --scanners vuln --severity "$SEVERITIES" --exit-code 1 \
    --format json --output /out/image.json "$IMAGE" || status=1
fi

if [ "$status" -ne 0 ]; then
  echo "Security scan found issues at or above $THRESHOLD (or a scan could not run)." >&2
fi
exit "$status"
