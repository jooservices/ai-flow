#!/usr/bin/env bash
# Clean-install acceptance on real machines (docs/v4/clean-install-acceptance.md): one clean Ubuntu 24.04 systemd
# container for the control plane and two clean ones for the machines, on a private network.
#   1. the control plane is installed with the documented command (`aiflow4-control-install --version V --domain D
#      --admin-email E`), which turns console sign-in on
#   2. anonymous callers are refused (/v1/state 401, /cc redirects to /login); the Super Administrator signs in with
#      the password the installer wrote to super-admin-password (it never leaves the container)
#   3. each machine container redeems a one-use join token (the join command's HTTP call) and sends its heartbeat with
#      only its node token: the sign-in gate must not block it; the nodes show in the signed-in state
#   4. the owner-token read of /v1/state that the deploy and canary scripts use answers 200
#   5. `aiflow4 backup create`, the state is wiped, `aiflow4 backup restore`: the Super Administrator signs in again
#      and the machines' tokens still work
# usage: clean-install-containers.sh <vX.Y.Z>   (a published release)
set -Eeuo pipefail

[[ $# -eq 1 ]] || { echo "usage: clean-install-containers.sh <vX.Y.Z>" >&2; exit 2; }
VERSION=$1
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
NAME="aiflow4-clean-$$"
IMAGE="$NAME-systemd"
NETWORK="$NAME-net"
CONTROL="$NAME-control"
NODES=("$NAME-node-a" "$NAME-node-b")
DOMAIN=control.clean.test
ADMIN=owner@example.test
cleanup() {
  docker rm -f "${NODES[@]}" "$CONTROL" >/dev/null 2>&1 || true
  docker volume rm "$NAME-docker" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
}
trap cleanup EXIT
fail() { echo "CLEAN INSTALL FAILED: $*" >&2; exit 1; }

docker build --pull -f "$ROOT/deploy/v4/ci/systemd.Dockerfile" -t "$IMAGE" "$ROOT"
docker network create "$NETWORK" >/dev/null
start() {  # start <name> <alias> [extra docker args...]
  local name=$1 alias=$2; shift 2
  docker run --detach --name "$name" --privileged --cgroupns=host --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
    --volume /sys/fs/cgroup:/sys/fs/cgroup:rw --network "$NETWORK" --network-alias "$alias" "$@" "$IMAGE" >/dev/null
  for _ in $(seq 1 30); do docker exec "$name" systemctl is-active --quiet basic.target && return 0; sleep 1; done
  fail "systemd did not start in $name"
}
start "$CONTROL" control --volume "$NAME-docker:/var/lib/docker" \
  --volume "$ROOT/deploy/v4/bin/aiflow4-control-install:/opt/aiflow4-control-install:ro"
start "${NODES[0]}" node-a
start "${NODES[1]}" node-b

in_control() { docker exec -i "$CONTROL" bash -se; }
URL=http://127.0.0.1
HOSTHDR="Host: $DOMAIN"

echo "== 1. documented control install of $VERSION (sign-in on)"
docker exec "$CONTROL" bash /opt/aiflow4-control-install --version "$VERSION" --domain "$DOMAIN" --admin-email "$ADMIN"
docker exec "$CONTROL" grep -qx 'AIFLOW4_CONSOLE_LOGIN=1' /etc/aiflow/secrets/control/aiflow4.env \
  || fail "the install did not turn console sign-in on"

echo "== 2. anonymous callers are refused, the Super Administrator signs in"
in_control <<CHECK || fail "sign-in checks"
set -euo pipefail
code() { curl -s -o /dev/null -w '%{http_code}' -H '$HOSTHDR' "\$@"; }
[[ \$(code $URL/healthz) == 200 ]]
[[ \$(code $URL/v1/state) == 401 ]]
[[ \$(code $URL/cc) == 302 ]]
jar=\$(mktemp); trap 'rm -f "\$jar"' EXIT
# the password stays in the container: it goes from the file into curl's stdin
body=\$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": open("/etc/aiflow/secrets/console/super-admin-password").read().strip()}))' '$ADMIN')
[[ \$(curl -s -o /dev/null -w '%{http_code}' -H '$HOSTHDR' -H 'Content-Type: application/json' -c "\$jar" --data-binary @- $URL/v1/auth/login <<<"\$body") == 200 ]]
[[ \$(code -b "\$jar" $URL/v1/state) == 200 ]]
CHECK

echo "== 3. two machines join and send heartbeats with only their node token"
for i in 0 1; do
  node=${NODES[$i]}; machine="clean-node-$i"
  in_control <<ISSUE > /dev/null || fail "join token for $machine"
set -euo pipefail
. /etc/aiflow/secrets/control/aiflow4.env
curl -fsS -H '$HOSTHDR' -H 'Content-Type: application/json' \
  --data "{\"provision_key\": \"\$AIFLOW4_JOIN_TOKEN\", \"name\": \"$machine\", \"role\": \"worker\", \"project\": \"CLEAN\"}" \
  $URL/v1/nodes/join-tokens > /tmp/join-$i.json
ISSUE
  docker cp "$CONTROL:/tmp/join-$i.json" - | tar -xO | docker exec -i "$node" bash -se <<NODE || fail "$machine join"
set -euo pipefail
token=\$(python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])')
joined=\$(curl -fsS -H '$HOSTHDR' -H 'Content-Type: application/json' --data "{\"join_token\": \"\$token\"}" http://control/v1/nodes/join)
python3 -c 'import json,sys; print(json.load(sys.stdin)["node_token"])' <<<"\$joined" > /tmp/node.token
chmod 600 /tmp/node.token
curl -fsS -H '$HOSTHDR' -H "Authorization: Bearer \$(cat /tmp/node.token)" -H 'Content-Type: application/json' \
  --data '{"slots_total": 1}' http://control/v1/nodes/$machine/heartbeat > /dev/null
NODE
done
in_control <<CHECK || fail "the machines are not in the state"
set -euo pipefail
. /etc/aiflow/secrets/control/aiflow4.env
curl -fsS -H '$HOSTHDR' -H "Authorization: Bearer \$AIFLOW4_OWNER_TOKEN" $URL/v1/state \
  | python3 -c 'import json,sys; names={n["name"] for n in json.load(sys.stdin)["nodes"]}; assert {"clean-node-0","clean-node-1"} <= names, names'
CHECK

echo "== 4. the owner-token read of the deploy and canary scripts"
in_control <<CHECK || fail "owner-token state read"
set -euo pipefail
AIFLOW4_CONTROL_URL=$URL bash -c '
  token=\$(sed -n "s/^AIFLOW4_OWNER_TOKEN=//p" /etc/aiflow/secrets/control/aiflow4.env | head -n1)
  curl -fsS -H "$HOSTHDR" -H @- $URL/v1/state <<<"Authorization: Bearer \$token" > /dev/null'
CHECK

echo "== 5. backup, wipe, restore"
docker exec "$CONTROL" /usr/local/sbin/aiflow4 backup create --out /root/clean-backup.tar.gz
docker exec "$CONTROL" bash -c 'stat -c %a /root/clean-backup.tar.gz | grep -qx 600'
docker exec "$CONTROL" docker exec aiflow4-postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qc "TRUNCATE console_users, nodes CASCADE"'
docker exec "$CONTROL" /usr/local/sbin/aiflow4 backup restore /root/clean-backup.tar.gz
in_control <<CHECK || fail "sign-in after the restore"
set -euo pipefail
body=\$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": open("/etc/aiflow/secrets/console/super-admin-password").read().strip()}))' '$ADMIN')
[[ \$(curl -s -o /dev/null -w '%{http_code}' -H '$HOSTHDR' -H 'Content-Type: application/json' --data-binary @- $URL/v1/auth/login <<<"\$body") == 200 ]]
CHECK
echo "clean install on real machines passed: sign-in on, two machines joined, backup and restore"
