#!/usr/bin/env bash
# Clean-install acceptance on real machines (docs/v4/clean-install-acceptance.md): one clean Ubuntu 24.04 systemd
# container for the control plane and two clean ones for the machines, on a private network.
#   1. the control plane is installed with the documented command (`aiflow4-control-install --version V --domain D
#      --admin-email E`), which turns console sign-in on
#   2. anonymous callers are refused (/v1/state 401, /cc redirects to /login); the Super Administrator signs in with
#      the password the installer wrote to super-admin-password (it never leaves the container)
#   3. each machine container runs the documented join bootstrap (`curl .../v1/install.sh | sh -s -- --token-file F`
#      with a one-use join token): it installs the signed release, and the node agent it starts heartbeats with only
#      its node token (the sign-in gate must not block it); both nodes show online in the state
#   4. the owner-token read of /v1/state that the deploy and canary scripts use answers 200
#   5. the CI-only backup helper is exercised: after wiping the test state, restore keeps sign-in and machine tokens
#      working
# usage: clean-install-containers.sh <vX.Y.Z>   (a published release)
#        clean-install-containers.sh <vX.Y.Z> <signed-tarball> <sigstore-bundle> <SHA256SUMS>
#        The second form installs the signed, unpublished release candidate before the GitHub Release is created.
set -Eeuo pipefail

[[ $# -eq 1 || $# -eq 4 ]] || {
  echo "usage: clean-install-containers.sh <vX.Y.Z> [signed-tarball sigstore-bundle SHA256SUMS]" >&2
  exit 2
}
VERSION=$1
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
NAME="aiflow4-clean-$$"
IMAGE="$NAME-systemd"
NETWORK="$NAME-net"
CONTROL="$NAME-control"
NODES=("$NAME-node-a" "$NAME-node-b")
DOMAIN=control.clean.test
ADMIN=owner@example.test
CANDIDATE=0
ARTIFACTS=
COSIGN=
if [[ $# -eq 4 ]]; then
  CANDIDATE=1
  [[ $VERSION =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
    echo "candidate version must be vX.Y.Z" >&2; exit 2;
  }
  COSIGN=$(command -v cosign || true)
  [[ -n $COSIGN && -x $COSIGN ]] || { echo "cosign must be installed to verify the candidate" >&2; exit 2; }
  COSIGN=$(realpath "$COSIGN")
  ARTIFACTS=$(mktemp -d "${RUNNER_TEMP:-/tmp}/aiflow4-clean-candidate.XXXXXX")
  cp "$(realpath "$2")" "$ARTIFACTS/aiflow4-$VERSION.tar.gz"
  cp "$(realpath "$3")" "$ARTIFACTS/aiflow4-$VERSION.tar.gz.sigstore.json"
  cp "$(realpath "$4")" "$ARTIFACTS/SHA256SUMS"
  (cd "$ARTIFACTS" && sha256sum --check --status SHA256SUMS) || {
    echo "candidate checksum manifest does not match its tarball" >&2; exit 2;
  }
fi
cleanup() {
  docker rm -f "${NODES[@]}" "$CONTROL" >/dev/null 2>&1 || true
  docker volume rm "$NAME-docker" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  [[ -z $ARTIFACTS ]] || rm -rf "$ARTIFACTS"
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
CONTROL_MOUNTS=(--volume "$NAME-docker:/var/lib/docker"
  --volume "$ROOT/deploy/v4/bin/aiflow4-control-install:/opt/aiflow4-control-install:ro"
  --volume "$ROOT/deploy/v4/ci/acceptance-backup.py:/opt/aiflow4-acceptance-backup.py:ro")
if [[ $CANDIDATE == 1 ]]; then
  CONTROL_MOUNTS+=(--volume "$ARTIFACTS:/candidate:ro"
    --volume "$COSIGN:/usr/local/bin/cosign:ro"
    --volume "$ROOT/deploy/v4/ci/candidate-deploy.sh:/opt/aiflow4-candidate-deploy:ro")
fi
start "$CONTROL" control "${CONTROL_MOUNTS[@]}"
CONTROL_IP=$(docker inspect -f "{{(index .NetworkSettings.Networks \"$NETWORK\").IPAddress}}" "$CONTROL")
[[ -n $CONTROL_IP ]] || fail "no address for the control container"
# the machines reach the control plane by its public name, as machines do in production
start "${NODES[0]}" node-a --add-host "$DOMAIN:$CONTROL_IP"
start "${NODES[1]}" node-b --add-host "$DOMAIN:$CONTROL_IP"

in_control() { docker exec -i "$CONTROL" bash -se; }
URL=http://127.0.0.1
HOSTHDR="Host: $DOMAIN"

echo "== 1. documented control install of $VERSION (sign-in on)"
if [[ $CANDIDATE == 1 ]]; then
  # Stage only the signed assets where the documented installer looks for downloaded releases. Its own checksum and
  # cosign checks run before the release code is installed; the candidate deploy adapter supplies the local image.
  docker exec --env VERSION="$VERSION" "$CONTROL" bash -se <<'STAGE_CANDIDATE'
set -euo pipefail
target="/srv/aiflow/control/releases/$VERSION"
install -d -m 0755 "$target"
install -m 0644 "/candidate/aiflow4-$VERSION.tar.gz" \
  "/candidate/aiflow4-$VERSION.tar.gz.sigstore.json" /candidate/SHA256SUMS "$target/"
install -m 0755 /opt/aiflow4-candidate-deploy /usr/local/sbin/aiflow4-deploy
STAGE_CANDIDATE
  docker exec --env AIFLOW4_ACCEPTANCE_CANDIDATE="$VERSION" \
    --env AIFLOW4_ACCEPTANCE_REVISION="${GITHUB_SHA:-$(git -C "$ROOT" rev-parse HEAD)}" \
    --env AIFLOW4_ACCEPTANCE_ARTIFACTS=/candidate \
    "$CONTROL" bash /opt/aiflow4-control-install --version "$VERSION" --domain "$DOMAIN" --admin-email "$ADMIN"
else
  docker exec "$CONTROL" bash /opt/aiflow4-control-install --version "$VERSION" --domain "$DOMAIN" --admin-email "$ADMIN"
fi
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

echo "== 3. two machines run the documented join bootstrap and heartbeat with only their node token"
# The control repo of a clean install is empty: give it the project and the two machines (what Command Center's
# Projects and Nodes screens commit to the control repository on a real installation).
in_control <<REPO || fail "control repository"
set -euo pipefail
repo=/srv/aiflow/control/repos/aiflow-control
mkdir -p "\$repo/projects" "\$repo/nodes"
cat > "\$repo/projects/CLEAN.yaml" <<'YAML'
key: CLEAN
repo: jooservices/clean-install
base: main
YAML
for n in clean-node-0 clean-node-1; do
  printf 'name: %s\nrole: worker\nslots: 1\nprojects: [CLEAN]\n' "\$n" > "\$repo/nodes/\$n.yaml"
done
REPO
state_nodes() {  # the nodes of /v1/state, read with the owner token (name:status per line)
  in_control <<STATE
set -euo pipefail
. /etc/aiflow/secrets/control/aiflow4.env
curl -fsS -H '$HOSTHDR' -H "Authorization: Bearer \$AIFLOW4_OWNER_TOKEN" $URL/v1/state \
  | python3 -c 'import json,sys; [print(n["name"] + ":" + str(n.get("status"))) for n in json.load(sys.stdin)["nodes"]]'
STATE
}
issue_token() {  # issue_token <machine> <file>: the one-use join token is written inside the control container
  in_control <<ISSUE >/dev/null 2>&1
set -euo pipefail
. /etc/aiflow/secrets/control/aiflow4.env
umask 077
curl -fsS -H '$HOSTHDR' -H 'Content-Type: application/json' \
  --data "{\"provision_key\": \"\$AIFLOW4_JOIN_TOKEN\", \"name\": \"$1\", \"role\": \"worker\", \"project\": \"CLEAN\"}" \
  $URL/v1/nodes/join-tokens | python3 -c 'import json,sys; print(json.load(sys.stdin)["token"])' > $2
ISSUE
}
for i in 0 1; do
  node=${NODES[$i]}; machine="clean-node-$i"; tokenfile="/tmp/join-$i.token"
  issued=0
  for _ in $(seq 1 30); do  # the control API reads the new repository files on its next refresh
    if issue_token "$machine" "$tokenfile"; then issued=1; break; fi
    sleep 2
  done
  [[ $issued == 1 ]] || fail "join token for $machine"
  # the token is streamed from one container into a file of the other; the script text travels separately
  docker exec "$CONTROL" cat "$tokenfile" \
    | docker exec -i "$node" bash -c 'umask 077; cat > /root/join-token' || fail "$machine token delivery"
  docker exec "$CONTROL" rm -f "$tokenfile"
  # the documented join command; server-installer's OS setup is skipped (the container is already prepared)
  docker exec "$node" bash -c \
    "set -o pipefail; curl -fsSL http://$DOMAIN/v1/install.sh | AIFLOW4_SERVER_INSTALLER_PREPARED=1 sh -s -- --token-file /root/join-token" \
    || fail "$machine join bootstrap"
  [[ $(docker exec "$node" cat /srv/aiflow/current/RELEASE) == "$VERSION" ]] || fail "$machine does not run $VERSION"
  docker exec "$node" systemctl is-active --quiet aiflow4-node-agent || fail "$machine node agent is not running"
done
online=0
for _ in $(seq 1 60); do  # the agents heartbeat on their own
  online=$(state_nodes | grep -cE '^clean-node-[01]:online$' || true)
  [[ $online == 2 ]] && break
  sleep 3
done
[[ $online == 2 ]] || { state_nodes >&2 || true; fail "the node agents did not both report online"; }

echo "== 4. the owner-token read of the deploy and canary scripts"
in_control <<CHECK || fail "owner-token state read"
set -euo pipefail
AIFLOW4_CONTROL_URL=$URL bash -c '
  token=\$(sed -n "s/^AIFLOW4_OWNER_TOKEN=//p" /etc/aiflow/secrets/control/aiflow4.env | head -n1)
  curl -fsS -H "$HOSTHDR" -H @- $URL/v1/state <<<"Authorization: Bearer \$token" > /dev/null'
CHECK

echo "== 5. backup, wipe, restore"
docker exec "$CONTROL" bash -se <<'BACKUP'
set -euo pipefail
set -a; . /etc/aiflow/secrets/control/aiflow4.env; set +a
python=/srv/aiflow/control/cli/current/.venv/bin/python
"$python" /opt/aiflow4-acceptance-backup.py create /root/clean-backup.tar.gz
BACKUP
docker exec "$CONTROL" bash -c 'stat -c %a /root/clean-backup.tar.gz | grep -qx 600'
docker exec "$CONTROL" docker exec aiflow4-postgres sh -c \
  'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qc "TRUNCATE console_users, nodes CASCADE"'
docker exec "$CONTROL" bash -se <<'RESTORE'
set -euo pipefail
set -a; . /etc/aiflow/secrets/control/aiflow4.env; set +a
python=/srv/aiflow/control/cli/current/.venv/bin/python
"$python" /opt/aiflow4-acceptance-backup.py restore /root/clean-backup.tar.gz
RESTORE
in_control <<CHECK || fail "sign-in after the restore"
set -euo pipefail
body=\$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": open("/etc/aiflow/secrets/console/super-admin-password").read().strip()}))' '$ADMIN')
[[ \$(curl -s -o /dev/null -w '%{http_code}' -H '$HOSTHDR' -H 'Content-Type: application/json' --data-binary @- $URL/v1/auth/login <<<"\$body") == 200 ]]
CHECK
echo "clean install on real machines passed: sign-in on, two machines joined, backup and restore"
