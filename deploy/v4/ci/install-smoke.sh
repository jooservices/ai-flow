#!/usr/bin/env bash
# Install a signed release in clean Ubuntu/systemd containers before the release is published.
set -Eeuo pipefail

usage() {
  echo "usage: install-smoke.sh <version> <tarball> <sigstore-bundle> [previous-version previous-tarball previous-bundle]" >&2
  exit 2
}

[[ $# -eq 3 || $# -eq 6 ]] || usage

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
COSIGN=$(command -v cosign || true)
[[ -n $COSIGN && -x $COSIGN ]] || { echo "cosign must be installed before the install smoke test" >&2; exit 2; }
COSIGN=$(realpath "$COSIGN")

WORK=$(mktemp -d "${RUNNER_TEMP:-/tmp}/aiflow4-install-smoke.XXXXXX")
trap 'rm -rf "$WORK"' EXIT
ARTIFACTS=$WORK/artifacts
source "$ROOT/deploy/v4/ci/install-smoke-artifacts.sh"
prepare_install_smoke_artifacts "$ROOT" "$ARTIFACTS" "$@" || usage

NAME="aiflow4-smoke-$$"
NETWORK="$NAME-net"
CONTROL="$NAME-control"
NODE_A="$NAME-node-a"
NODE_B="$NAME-node-b"
IMAGE="$NAME-systemd"
cleanup() {
  docker rm -f "$NODE_A" "$NODE_B" "$CONTROL" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

docker build --pull -f "$ROOT/deploy/v4/ci/systemd.Dockerfile" -t "$IMAGE" "$ROOT"
docker network create "$NETWORK" >/dev/null
start_systemd() {
  local name=$1 alias=$2 publish=${3:-}
  local publish_args=()
  [[ -z $publish ]] || publish_args+=(--publish "$publish")
  docker run --detach --name "$name" --privileged --cgroupns=host \
    --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
    --volume /sys/fs/cgroup:/sys/fs/cgroup:rw \
    --volume "$ARTIFACTS:/artifacts:ro" \
    --volume "$COSIGN:/usr/local/bin/cosign:ro" \
    --network "$NETWORK" --network-alias "$alias" \
    --add-host host.docker.internal:host-gateway "${publish_args[@]}" "$IMAGE" >/dev/null
}
start_systemd "$CONTROL" control 18080:8080
start_systemd "$NODE_A" smoke-node-a
start_systemd "$NODE_B" smoke-node-b

wait_systemd() {
  local container=$1
  for _ in $(seq 1 30); do
    if docker exec "$container" systemctl is-active --quiet basic.target; then return 0; fi
    sleep 1
  done
  echo "Ubuntu systemd did not start in $container" >&2
  return 1
}
wait_systemd "$CONTROL"
wait_systemd "$NODE_A"
wait_systemd "$NODE_B"

docker exec --env VERSION="$VERSION" --env PREVIOUS_VERSION="$PREVIOUS_VERSION" --env BASELINE="$BASELINE" \
  "$CONTROL" bash -se <<'CONTROL_SETUP'
set -euo pipefail
release="/srv/aiflow/control/releases/$VERSION"
repo=/srv/aiflow/control/repos/aiflow-control
mkdir -p "$release" "$repo/nodes" "$repo/projects" /run/aiflow4-smoke
publish() {  # serve a signed release to the nodes exactly like aiflow4-deploy lays it out
  local version=$1 directory="/srv/aiflow/control/releases/$1"
  mkdir -p "$directory"
  install -m 0644 "/artifacts/aiflow4-$version.tar.gz" "$directory/"
  install -m 0644 "/artifacts/aiflow4-$version.tar.gz.sigstore.json" "$directory/"
  (cd "$directory" && sha256sum "aiflow4-$version.tar.gz" > SHA256SUMS)
}
publish "$VERSION"
# A halted rollout sends its members back to the previous signed release, so control must serve it too.
[[ $BASELINE -eq 1 ]] || publish "$PREVIOUS_VERSION"
printf '%s\n' "$VERSION" > /srv/aiflow/control/releases/CURRENT
tar -xzf "/artifacts/aiflow4-$VERSION.tar.gz" -C "$release"
(cd "$release" && uv sync --frozen --no-dev -q)

cat > "$repo/accounts.yaml" <<'YAML'
accounts:
  - {id: smoke-a, provider: codex, node: smoke-node-a, tier: middle, roles: [dev]}
  - {id: smoke-b, provider: codex, node: smoke-node-b, tier: middle, roles: [dev]}
YAML
cat > "$repo/profiles.yaml" <<'YAML'
providers:
  codex: {models: {middle: gpt-6-luna}}
YAML
cat > "$repo/nodes/smoke-node-a.yaml" <<'YAML'
name: smoke-node-a
role: worker
slots: 1
projects: [SMOKE]
YAML
cat > "$repo/nodes/smoke-node-b.yaml" <<'YAML'
name: smoke-node-b
role: worker
slots: 1
projects: [SMOKE]
YAML
cat > "$repo/projects/SMOKE.yaml" <<'YAML'
key: SMOKE
repo: jooservices/install-smoke
base: main
YAML

cat > /run/aiflow4-smoke/control.env <<'ENV'
AIFLOW4_ENV=ci-install-smoke
AIFLOW4_DATABASE_URL=postgresql://aiflow:test@host.docker.internal:5432/aiflow4
AIFLOW4_REDIS_URL=redis://host.docker.internal:6379/5
AIFLOW4_REDIS_PREFIX=ci-install-smoke:
AIFLOW4_CONTROL_REPO=/srv/aiflow/control/repos/aiflow-control
AIFLOW4_JOIN_TOKEN=ci-install-smoke-key
AIFLOW4_JOIN_CONTROL_URL=http://control:8080
AIFLOW4_ROLLOUT_SOAK_S=10
AIFLOW4_ROLLOUT_BATCH_SIZE=1
AIFLOW4_ROLLOUT_CANARY_NODE=smoke-node-a
AIFLOW4_CONSOLE_LOGIN=1
AIFLOW4_CONSOLE_SESSION_SECRET=ci-install-smoke-session-secret-32-bytes
AIFLOW4_OWNER_TOKEN=ci-install-smoke-owner-token
ENV
# control-loops drives the fleet rollout (canary, waves, halt, rollback), so it runs next to the API.
unit() {
  local name=$1 description=$2 command=$3
  cat > "/etc/systemd/system/aiflow4-$name.service" <<UNIT
[Unit]
Description=AI Flow v4 install smoke $description
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=/run/aiflow4-smoke/control.env
WorkingDirectory=$release
ExecStart=$release/.venv/bin/aiflow4 $command
Restart=on-failure
RestartSec=2

[Install]
WantedBy=multi-user.target
UNIT
}
unit control-api "control API" "control-api --host 0.0.0.0 --port 8080"
unit control-loops "control loops" "control-loops"
systemctl daemon-reload
systemctl enable --now aiflow4-control-api.service
CONTROL_SETUP

for _ in $(seq 1 120); do
  if curl --silent --show-error --fail http://127.0.0.1:18080/healthz >/dev/null 2>&1; then break; fi
  sleep 2
done
curl --silent --show-error --fail http://127.0.0.1:18080/healthz >/dev/null \
  || { echo "control API from the release artifact did not become healthy" >&2; exit 1; }

# Console sign-in is on for the whole run (the clean-install acceptance of docs/v4/clean-install-acceptance.md):
# anonymous callers are refused, the Super Administrator signs in, and every read below uses the owner token the
# deploy and canary scripts use. The machines join and heartbeat with their node tokens only.
CONTROL_URL=http://127.0.0.1:18080
state_read() { curl --silent --show-error --fail -H "Authorization: Bearer ci-install-smoke-owner-token" "$CONTROL_URL/v1/state"; }
docker exec "$CONTROL" bash -se <<'SEED_ADMIN'
set -euo pipefail
set -a; . /run/aiflow4-smoke/control.env; set +a
release="/srv/aiflow/control/releases/$(cat /srv/aiflow/control/releases/CURRENT)"
umask 077
"$release/.venv/bin/aiflow4" console-user seed --email owner@example.test --password-file /run/aiflow4-smoke/admin-password >/dev/null
SEED_ADMIN
anon=$(curl --silent --output /dev/null --write-out '%{http_code}' "$CONTROL_URL/v1/state")
[[ $anon == 401 ]] || { echo "anonymous /v1/state answered $anon instead of 401 with sign-in on" >&2; exit 1; }
anon=$(curl --silent --output /dev/null --write-out '%{http_code}' "$CONTROL_URL/cc")
[[ $anon == 302 ]] || { echo "anonymous /cc answered $anon instead of a redirect to /login" >&2; exit 1; }
docker exec "$CONTROL" bash -se <<'SIGN_IN'
set -euo pipefail
body=$(python3 -c 'import json; print(json.dumps({"email": "owner@example.test", "password": open("/run/aiflow4-smoke/admin-password").read().strip()}))')
code=$(curl -s -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' --data-binary @- http://127.0.0.1:8080/v1/auth/login <<<"$body")
[[ $code == 200 ]] || { echo "the Super Administrator could not sign in (HTTP $code)" >&2; exit 1; }
SIGN_IN
state_read >/dev/null || { echo "the owner-token state read failed with sign-in on" >&2; exit 1; }
docker exec "$CONTROL" bash -se <<'SEED_TICKET'
set -euo pipefail
/srv/aiflow/control/releases/"$(cat /srv/aiflow/control/releases/CURRENT)"/.venv/bin/python - <<'PY'
import psycopg
db = psycopg.connect("postgresql://aiflow:test@host.docker.internal:5432/aiflow4")
with db, db.cursor() as cursor:
    cursor.execute(
        "INSERT INTO tickets (key, project, status, labels, summary, description)"
        " VALUES ('SMOKE-1','SMOKE','To Do',ARRAY['aiflow-ready'],'Install smoke claim','CI smoke ticket')"
        " ON CONFLICT (key) DO UPDATE SET status='To Do', labels=ARRAY['aiflow-ready']"
    )
db.close()
PY
SEED_TICKET

bootstrap_nodes() {
  local container=$1 node=$2 account=$3 token
  docker exec --env VERSION="$VERSION" "$container" bash -se <<'NODE_BOOTSTRAP'
set -euo pipefail
mkdir -p /etc/aiflow /run/aiflow4-smoke /srv/aiflow/state /tmp/aiflow4-bootstrap
cat > /etc/aiflow/node.env <<'ENV'
AIFLOW4_CONTROL_URL=http://control:8080
AIFLOW4_NODE_NAME=NODE_NAME_REPLACED
AIFLOW4_NODE_TOKEN_FILE=/srv/aiflow/state/node.token
AIFLOW4_HEARTBEAT_S=5
AIFLOW4_SLOTS=1
AIFLOW4_POLICY_RELAY=0
AIFLOW4_SELF_UPDATE=0
ENV
tar -xzf "/artifacts/aiflow4-$VERSION.tar.gz" -C /tmp/aiflow4-bootstrap
(cd /tmp/aiflow4-bootstrap && uv sync --frozen --no-dev -q)
mkdir -p /etc/systemd/system/aiflow4-node-agent.service.d
cat > /etc/systemd/system/aiflow4-node-agent.service.d/ci-no-workers.conf <<'UNIT'
[Service]
ExecStart=
ExecStart=/srv/aiflow/current/.venv/bin/aiflow4 node-agent --slots ${AIFLOW4_SLOTS} --no-workers
UNIT
NODE_BOOTSTRAP

  # Replace the fixed node name in the freshly written environment without putting a join token in argv.
  docker exec --env NODE_NAME="$node" "$container" bash -euc \
    'sed -i "s/NODE_NAME_REPLACED/$NODE_NAME/" /etc/aiflow/node.env'
  token=$(curl --silent --show-error --fail http://127.0.0.1:18080/v1/nodes/join-tokens \
    -H 'Content-Type: application/json' \
    --data "{\"provision_key\":\"ci-install-smoke-key\",\"name\":\"$node\",\"role\":\"worker\",\"project\":\"SMOKE\"}" \
    | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
  printf '%s' "$token" | docker exec -i "$container" bash -euc \
    'umask 077; cat > /run/aiflow4-smoke/join-token'
  docker exec --env NODE_NAME="$node" --env AIFLOW4_NODE_TOKEN_FILE=/srv/aiflow/state/node.token \
    "$container" \
    /tmp/aiflow4-bootstrap/.venv/bin/aiflow4 node join \
    --join-token-file /run/aiflow4-smoke/join-token --name "$node" --control http://control:8080 >/dev/null
  docker exec "$container" rm -f /run/aiflow4-smoke/join-token

  if [[ $BASELINE -eq 0 ]]; then
    local checksum
    checksum=$(sha256sum "$ARTIFACTS/aiflow4-$PREVIOUS_VERSION.tar.gz" | awk '{print $1}')
    docker exec --env PREVIOUS_VERSION="$PREVIOUS_VERSION" --env CHECKSUM="$checksum" "$container" bash -se <<'INSTALL_PREVIOUS'
set -euo pipefail
AIFLOW4_NODE_ENV=/etc/aiflow/node.env \
  /tmp/aiflow4-bootstrap/deploy/v4/bin/aiflow4-node-install \
  "/artifacts/aiflow4-$PREVIOUS_VERSION.tar.gz" "$PREVIOUS_VERSION" \
  "/artifacts/aiflow4-$PREVIOUS_VERSION.tar.gz.sigstore.json" "$CHECKSUM"
INSTALL_PREVIOUS
  else
    local baseline="$PREVIOUS_VERSION"
    docker exec --env BASELINE="$baseline" "$container" bash -se <<'INSTALL_BASELINE'
set -euo pipefail
release="/srv/aiflow/releases/$BASELINE"
mkdir -p "$release"
tar -xzf "/artifacts/aiflow4-baseline-$BASELINE.tar.gz" -C "$release"
(cd "$release" && uv sync --frozen --no-dev -q)
printf '%s\n' "$BASELINE" > "$release/RELEASE"
install -m 0755 "$release/deploy/v4/bin/aiflow4-node-install" /usr/local/sbin/aiflow4-node-install
install -m 0644 "$release"/deploy/v4/node/systemd/*.service /etc/systemd/system/
ln -sfn "$release" /srv/aiflow/current.new && mv -T /srv/aiflow/current.new /srv/aiflow/current
systemctl daemon-reload
systemctl enable --now aiflow4-node-agent
INSTALL_BASELINE
  fi
}

bootstrap_nodes "$NODE_A" smoke-node-a smoke-a
bootstrap_nodes "$NODE_B" smoke-node-b smoke-b
wait_for_release() {
  local node=$1 expected=$2
  for _ in $(seq 1 120); do
    if state_read \
      | python3 -c 'import json,sys; state=json.load(sys.stdin); node=next((n for n in state.get("nodes",[]) if n.get("name")==sys.argv[1]),{}); sys.exit(0 if node.get("status")=="online" and node.get("release")==sys.argv[2] else 1)' \
        "$node" "$expected"; then
      return 0
    fi
    sleep 2
  done
  echo "timed out waiting for $node to report online on $expected" >&2
  return 1
}

wait_for_system_check() {
  local node=$1
  for _ in $(seq 1 120); do
    if curl --silent --show-error --fail http://127.0.0.1:18080/v1/system-check \
      | python3 -c 'import json,sys; data=json.load(sys.stdin); node=next((n for n in data.get("nodes",[]) if n.get("name")==sys.argv[1]),{}); result=node.get("readiness",{}); sys.exit(0 if result.get("checked_at") and result.get("machine") and "SMOKE" in result.get("projects",{}) else 1)' \
        "$node"; then
      break
    fi
    sleep 2
  done
  curl --silent --show-error --fail http://127.0.0.1:18080/v1/support-bundle \
    | python3 -c 'import json,sys; data=json.load(sys.stdin); nodes=data.get("system_check",{}).get("nodes",[]); node=next((n for n in nodes if n.get("name")==sys.argv[1]),{}); result=node.get("readiness",{}); sys.exit(0 if result.get("checked_at") and "SMOKE" in result.get("projects",{}) else 1)' \
      "$node" || { echo "clean-install support bundle omitted the system check for $node" >&2; return 1; }
}

# Evaluate a Python expression against control's /v1/state: r is the latest fleet rollout, m its members by node,
# incidents the open (kind, subject) pairs, v the candidate and p the previous release.
STATE_CHECK='
import json, sys
state = json.load(sys.stdin)
r = state.get("rollout") or {}
m = {n["node"]: n for n in r.get("nodes") or []}
incidents = {(i["kind"], i["subject"]) for i in state.get("incidents") or []}
v, p = sys.argv[2], sys.argv[3]
sys.exit(0 if eval(f"({sys.argv[1]})") else 1)
'
wait_for_state() {
  local what=$1 check=$2
  for _ in $(seq 1 150); do
    if state_read \
      | python3 -c "$STATE_CHECK" "$check" "$VERSION" "$PREVIOUS_VERSION"; then
      return 0
    fi
    sleep 2
  done
  echo "timed out waiting for control to report $what; latest rollout:" >&2
  state_read | python3 -c 'import json,sys; print(json.load(sys.stdin).get("rollout"))' >&2 || true
  return 1
}
set_current() {
  docker exec --env RELEASE="$1" "$CONTROL" bash -euc 'printf "%s\n" "$RELEASE" > /srv/aiflow/control/releases/CURRENT'
}

wait_for_release smoke-node-a "$PREVIOUS_VERSION"
wait_for_release smoke-node-b "$PREVIOUS_VERSION"
wait_for_system_check smoke-node-a
wait_for_system_check smoke-node-b
# Start fleet rollout processing only after both joined nodes have reported the previous release.
# This gives the rollout a real baseline to canary from and lets node B's failed candidate halt it.
docker exec "$CONTROL" systemctl enable --now aiflow4-control-loops.service

docker exec "$NODE_A" bash -se <<'CLAIM_SMOKE'
set -euo pipefail
/tmp/aiflow4-bootstrap/.venv/bin/python - <<'PY'
from pathlib import Path
import httpx
token = Path("/srv/aiflow/state/node.token").read_text().strip()
response = httpx.post(
    "http://control:8080/v1/work/claim",
    headers={"Authorization": f"Bearer {token}"},
    json={"ticket": "SMOKE-1", "account": "smoke-a", "step": "dev"},
    timeout=20,
)
data = response.json()
if response.status_code != 200 or not data.get("claimed"):
    raise SystemExit(f"node claim failed: HTTP {response.status_code} {response.text[:200]}")
print("node claim smoke passed")
PY
CLAIM_SMOKE

# Stage a signed-but-unhealthy candidate only on node B. Its installed release helper must detect the
# unhealthy agent and switch back to the previous release; node A verifies the successful upgrade path.
docker exec --env VERSION="$VERSION" "$NODE_B" bash -se <<'STAGE_BROKEN_CANDIDATE'
set -euo pipefail
release="/srv/aiflow/releases/$VERSION"
mkdir -p "$release/.venv/bin" "$release/deploy/v4/bin" "$release/deploy/v4/node/systemd" /run/aiflow4-smoke
cp /tmp/aiflow4-bootstrap/deploy/v4/bin/aiflow4-node-install "$release/deploy/v4/bin/"
cp /tmp/aiflow4-bootstrap/deploy/v4/node/systemd/*.service "$release/deploy/v4/node/systemd/"
cat > "$release/.venv/bin/aiflow4" <<'BROKEN'
#!/bin/sh
touch /run/aiflow4-smoke/candidate-started
exit 1
BROKEN
chmod 0755 "$release/.venv/bin/aiflow4"
printf '%s\n' "$VERSION" > "$release/RELEASE"
STAGE_BROKEN_CANDIDATE

self_update_on() {  # also restarts the agent, which clears its in-memory "tried this release" throttle
  docker exec "$1" bash -euc \
    'sed -i "s/^AIFLOW4_SELF_UPDATE=.*/AIFLOW4_SELF_UPDATE=1/" /etc/aiflow/node.env; systemctl restart aiflow4-node-agent'
}

# Upgrade: node A is the rollout's canary (AIFLOW4_ROLLOUT_CANARY_NODE) and installs the candidate itself.
self_update_on "$NODE_A"
wait_for_release smoke-node-a "$VERSION"
if [[ $BASELINE -eq 0 ]]; then
  # The canary passes its soak and the next wave (node B alone, batch size 1) is assigned the candidate.
  wait_for_state "node A healthy and node B assigned $VERSION in wave 1" \
    'r.get("target") == v and r.get("status") == "rolling" and m.get("smoke-node-a", {}).get("status") == "healthy"
     and m.get("smoke-node-b", {}).get("status") == "assigned"'
fi

checksum=$(sha256sum "$TARBALL" | awk '{print $1}')
set +e
docker exec --env VERSION="$VERSION" --env CHECKSUM="$checksum" \
  --env AIFLOW4_NODE_ENV=/etc/aiflow/node.env "$NODE_B" bash -se <<'ROLLBACK_SMOKE'
set +e
/tmp/aiflow4-bootstrap/deploy/v4/bin/aiflow4-node-install \
  "/artifacts/aiflow4-$VERSION.tar.gz" "$VERSION" \
  "/artifacts/aiflow4-$VERSION.tar.gz.sigstore.json" "$CHECKSUM" >/dev/null
status=$?
set -e
[[ $status -eq 6 ]] || { echo "rollback installer returned $status instead of 6" >&2; exit 1; }
ROLLBACK_SMOKE
rollback_status=$?
set -e
[[ $rollback_status -eq 0 ]] \
  || { echo "failed-release install on node B did not complete the rollback check (exit $rollback_status)" >&2; exit 1; }
docker exec "$NODE_B" bash -euc 'test -e /run/aiflow4-smoke/candidate-started' \
  || { echo "node B did not start the unhealthy candidate" >&2; exit 1; }
wait_for_release smoke-node-b "$PREVIOUS_VERSION"
docker exec "$NODE_B" systemctl is-active --quiet aiflow4-node-agent \
  || { echo "node B agent did not recover after rollback" >&2; exit 1; }

# The installer's failure report must reach control: POST /v1/nodes/smoke-node-b/release-failures opens a
# critical release_verification incident for this node and release.
wait_for_state "the release_verification incident for node B" \
  '("release_verification", "smoke-node-b:" + v) in incidents'

if [[ $BASELINE -eq 0 ]]; then
  # The report fails node B's health check: the rollout halts, names node B and opens the fleet_rollout incident.
  wait_for_state "the rollout of $VERSION halted by node B" \
    'r.get("target") == v and r.get("status") == "halted" and r.get("failed_node") == "smoke-node-b"
     and "rejected the release" in (r.get("reason") or "") and ("fleet_rollout", v) in incidents'
  # No node keeps a halted release: the healthy canary is sent back to the previous release as well.
  wait_for_release smoke-node-a "$PREVIOUS_VERSION"
  wait_for_state "the fleet rollback of $VERSION to succeed" \
    'r.get("target") == v and r.get("status") == "halted" and r.get("rollback") == "succeeded"
     and all(m.get(n, {}).get("status") == "rolled_back" for n in ("smoke-node-a", "smoke-node-b"))
     and ("fleet_rollback", v) in incidents'

  # Resume as the runbook describes: fix node B, point CURRENT at the previous release (that rollout completes at
  # once), then name the candidate again so a fresh rollout moves both nodes through canary and wave.
  docker exec --env VERSION="$VERSION" "$NODE_B" bash -euc 'rm -rf "/srv/aiflow/releases/$VERSION"'
  self_update_on "$NODE_A"
  self_update_on "$NODE_B"
  set_current "$PREVIOUS_VERSION"
  wait_for_state "the rollout back to $PREVIOUS_VERSION to complete" \
    'r.get("target") == p and r.get("status") == "completed"'
  set_current "$VERSION"
  wait_for_release smoke-node-a "$VERSION"
  wait_for_release smoke-node-b "$VERSION"
  wait_for_state "the new rollout of $VERSION to complete" 'r.get("target") == v and r.get("status") == "completed"'
else
  # The first tagged release follows an unversioned develop build: control has no previous fleet release, so the
  # rollout of the candidate completed at once and there is nothing to halt. Finish node B by hand.
  wait_for_state "the rollout of $VERSION to be complete" 'r.get("target") == v and r.get("status") == "completed"'
  docker exec --env VERSION="$VERSION" "$NODE_B" bash -euc 'rm -rf "/srv/aiflow/releases/$VERSION"'
  docker exec --env VERSION="$VERSION" --env CHECKSUM="$checksum" "$NODE_B" bash -se <<'INSTALL_CANDIDATE_B'
set -euo pipefail
AIFLOW4_NODE_ENV=/etc/aiflow/node.env \
  /tmp/aiflow4-bootstrap/deploy/v4/bin/aiflow4-node-install \
  "/artifacts/aiflow4-$VERSION.tar.gz" "$VERSION" \
  "/artifacts/aiflow4-$VERSION.tar.gz.sigstore.json" "$CHECKSUM"
INSTALL_CANDIDATE_B
  wait_for_release smoke-node-a "$VERSION"
  wait_for_release smoke-node-b "$VERSION"
fi

echo "Install smoke passed: control and both nodes ran $VERSION; heartbeat, claim, upgrade, rollback and the" \
  "rollout halt on a failed install succeeded."
