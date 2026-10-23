#!/usr/bin/env bash
# Install a real offline bundle on Ubuntu 24.04 with systemd and no public network route.
# The supplied control container is attached only to the smoke's private Docker network for enrollment.
set -Eeuo pipefail

usage() {
  echo "usage: offline-install-smoke.sh <bundle-dir> <control-container> <https-control-url> <join-token-file> [private-ca-cert]" >&2
  exit 2
}

[[ $# -eq 4 || $# -eq 5 ]] || usage
BUNDLE=$(realpath "$1")
CONTROL_CONTAINER=$2
CONTROL_URL=$3
JOIN_TOKEN_FILE=$(realpath "$4")
CA_CERTIFICATE=${5:+$(realpath "$5")}
[[ -d $BUNDLE && -f $BUNDLE/manifest.json && -f $BUNDLE/installer/aiflow4-offline ]] || {
  echo "offline smoke requires an extracted bundle directory" >&2; exit 2;
}
[[ -f $JOIN_TOKEN_FILE ]] || { echo "join-token file does not exist" >&2; exit 2; }
[[ $CONTROL_URL == https://* ]] || { echo "control URL must use HTTPS" >&2; exit 2; }
if [[ -n $CA_CERTIFICATE && ! -f $CA_CERTIFICATE ]]; then
  echo "private CA certificate does not exist" >&2; exit 2
fi
CONTROL_HOST=${CONTROL_URL#https://}
CONTROL_HOST=${CONTROL_HOST%%/*}
CONTROL_HOST=${CONTROL_HOST%%:*}
[[ -n $CONTROL_HOST ]] || { echo "control URL must include a hostname" >&2; exit 2; }

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
NAME="aiflow4-offline-smoke-$$"
NETWORK="$NAME-net"
IMAGE="$NAME-systemd"
CONTROL_CONNECTED=0
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  if [[ $CONTROL_CONNECTED -eq 1 ]]; then
    docker network disconnect "$NETWORK" "$CONTROL_CONTAINER" >/dev/null 2>&1 || true
  fi
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT

# The target image is prepared before isolation and contains only systemd plus TLS trust. The bundle provides the
# node packages, Docker engine and runtimes; the install itself runs with an internal-only network.
docker build --pull -f "$ROOT/deploy/v4/ci/offline-target.Dockerfile" -t "$IMAGE" "$ROOT"
docker network create --driver bridge --internal "$NETWORK" >/dev/null
docker network connect --alias "$CONTROL_HOST" "$NETWORK" "$CONTROL_CONTAINER"
CONTROL_CONNECTED=1
target_mounts=(--volume "$BUNDLE:/media/aiflow-offline:ro"
  --volume "$JOIN_TOKEN_FILE:/tmp/aiflow4-offline-join-token:ro")
if [[ -n $CA_CERTIFICATE ]]; then
  target_mounts+=(--volume "$CA_CERTIFICATE:/tmp/aiflow4-offline-control-ca.crt:ro")
fi
docker run --detach --name "$NAME" --privileged --cgroupns=host \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
  --volume /sys/fs/cgroup:/sys/fs/cgroup:rw \
  "${target_mounts[@]}" \
  --network "$NETWORK" "$IMAGE" >/dev/null

[[ $(docker network inspect --format '{{.Internal}}' "$NETWORK") == true ]] || {
  echo "offline smoke Docker network is not internal" >&2; exit 1;
}
for _ in $(seq 1 60); do
  if docker exec "$NAME" systemctl is-active --quiet basic.target; then break; fi
  sleep 1
done
docker exec "$NAME" systemctl is-active --quiet basic.target || {
  echo "Ubuntu systemd did not start in the offline target" >&2; exit 1;
}
if [[ -n $CA_CERTIFICATE ]]; then
  docker exec "$NAME" install -m 0644 /tmp/aiflow4-offline-control-ca.crt \
    /usr/local/share/ca-certificates/aiflow4-offline-control.crt
  docker exec "$NAME" update-ca-certificates >/dev/null
fi

echo "Installing the bundle on Ubuntu 24.04 with public networking disabled"
timeout 900 docker exec "$NAME" /media/aiflow-offline/installer/aiflow4-offline install \
  /media/aiflow-offline --control-url "$CONTROL_URL" \
  --join-token-file /tmp/aiflow4-offline-join-token

docker exec "$NAME" systemctl is-active --quiet aiflow4-node-agent.service || {
  echo "offline node agent unit is not active" >&2; exit 1;
}
worker_units=$(docker exec "$NAME" systemctl list-units --state=active --plain --no-legend \
  'aiflow4-worker@*.service')
if ! grep -Eq '^aiflow4-worker@[^[:space:]]+\.service[[:space:]]+loaded[[:space:]]+active[[:space:]]' <<<"$worker_units"; then
  echo "no assigned offline worker unit is active" >&2; exit 1
fi

# Read the node token inside the target and query the live control API; never print or pass the token in argv.
docker exec -i --env AIFLOW4_SMOKE_CONTROL_URL="$CONTROL_URL" "$NAME" \
  /srv/aiflow/current/.venv/bin/python - <<'PY'
import json
import os
import shlex
import urllib.parse
import urllib.request
from pathlib import Path

settings = {}
for line in Path("/etc/aiflow/node.env").read_text(encoding="utf-8").splitlines():
    key, _, value = line.partition("=")
    if key and value:
        settings[key] = shlex.split(value)[0]
name = settings["AIFLOW4_NODE_NAME"]
token = Path(settings["AIFLOW4_NODE_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
url = os.environ["AIFLOW4_SMOKE_CONTROL_URL"].rstrip("/")
path = "/v1/nodes/" + urllib.parse.quote(name, safe="") + "/health"
request = urllib.request.Request(url + path, headers={"Authorization": "Bearer " + token})
with urllib.request.urlopen(request, timeout=30) as response:
    health = json.load(response)
age = health.get("heartbeat_age_seconds")
if (health.get("status") != "online" or not isinstance(age, (int, float)) or age > 60
        or health.get("preflight_ok") is not True):
    raise SystemExit("offline node heartbeat or tool preflight is not healthy")
print(f"live control health is online, heartbeat age {age:.1f}s, preflight green")
PY

echo "offline install smoke passed: agent heartbeat and assigned worker unit are healthy"
