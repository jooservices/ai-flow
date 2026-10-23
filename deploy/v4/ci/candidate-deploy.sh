#!/usr/bin/env bash
# Feed a signed, unpublished candidate to the documented `aiflow4 install control` flow.
# The release assets are mounted at /candidate. All other deploy commands are delegated to the installed release.
set -Eeuo pipefail

VERSION=${AIFLOW4_ACCEPTANCE_CANDIDATE:?candidate version is required}
REVISION=${AIFLOW4_ACCEPTANCE_REVISION:?candidate revision is required}
ARTIFACTS=${AIFLOW4_ACCEPTANCE_ARTIFACTS:-/candidate}
CONTROL=/srv/aiflow/control
TAR="$ARTIFACTS/aiflow4-$VERSION.tar.gz"
BUNDLE="$TAR.sigstore.json"
SUMS="$ARTIFACTS/SHA256SUMS"
IMAGE="ghcr.io/jooservices/ai-flow/control-api:$VERSION"

if [[ $# -eq 2 && $1 == --stage-only && $2 == "$VERSION" ]]; then
  (cd "$ARTIFACTS" && sha256sum --check --status SHA256SUMS) \
    || { echo "candidate $VERSION checksum verification failed" >&2; exit 1; }
  cosign verify-blob "$TAR" --bundle "$BUNDLE" \
    --certificate-identity "https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/$VERSION" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com

  release="$CONTROL/releases/$VERSION"
  build="$CONTROL/build/$VERSION"
  install -d -m 0755 "$release" "$build"
  install -m 0644 "$TAR" "$BUNDLE" "$SUMS" "$release/"
  tar -xzf "$TAR" -C "$build"
  docker build --build-arg "AIFLOW4_VERSION=$VERSION" --build-arg "AIFLOW4_REVISION=$REVISION" \
    -f "$build/deploy/v4/control/Dockerfile" -t "$IMAGE" "$build"
  exit 0
fi

if [[ $# -eq 1 && $1 == "$VERSION" ]]; then
  # The tagged archive is not public yet, so do the documented release deploy's local compose switch from its
  # verified, staged files and locally built image. This is the only deploy adaptation in candidate mode.
  compose="$CONTROL/build/$VERSION/deploy/v4/control/compose.yaml"
  [[ -f $compose ]] || { echo "candidate compose bundle is missing" >&2; exit 1; }
  exec 9>/run/aiflow4-deploy.lock
  flock -w 1800 9 || { echo "another deploy is running" >&2; exit 8; }
  for service in control-api-a control-api-b control-loops; do
    AIFLOW4_IMAGE_REPOSITORY=ghcr.io/jooservices/ai-flow/control-api \
      AIFLOW4_IMAGE_TAG="$VERSION" AIFLOW4_VERSION="$VERSION" \
      docker compose -f "$compose" up -d --no-deps "$service"
  done
  install -m 0755 "$CONTROL/build/$VERSION/deploy/v4/bin/aiflow4-node-install" \
    "$CONTROL/build/$VERSION/deploy/v4/bin/aiflow4-deploy" \
    "$CONTROL/build/$VERSION/deploy/v4/bin/aiflow4-autodeploy" \
    "$CONTROL/build/$VERSION/deploy/v4/bin/aiflow4-develop-install" \
    "$CONTROL/build/$VERSION/deploy/v4/bin/aiflow4-deploy-develop" \
    "$CONTROL/build/$VERSION/deploy/v4/bin/aiflow4-autodeploy-develop" /usr/local/sbin/
  install -m 0644 "$CONTROL/build/$VERSION"/deploy/v4/control/systemd/aiflow4-autodeploy.* \
    /etc/systemd/system/
  systemctl daemon-reload
  # The candidate tag is intentionally unpublished; enabling the one-minute timer would select the previous public
  # release and replace the candidate while the two nodes are joining.
  printf '%s\n' "$VERSION" > "$CONTROL/releases/CURRENT.tmp"
  mv -f "$CONTROL/releases/CURRENT.tmp" "$CONTROL/releases/CURRENT"
  exit 0
fi

exec "$CONTROL/cli/current/deploy/v4/bin/aiflow4-deploy" "$@"
