#!/usr/bin/env bash
# Clean-install acceptance on real machines (docs/v4/clean-install-acceptance.md): one clean Ubuntu 24.04 systemd
# container for the control plane and two clean ones for the machines, on a private network.
#   1. the control plane is installed with the documented command (`aiflow4-control-install --version V --domain D`),
#      which turns console sign-in on; the first Administrator is created in browser setup
#   2. anonymous callers are refused (/v1/state 401, /cc redirects to /login); the browser setup creates an
#      Administrator and keeps its password inside the control container
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
[[ $VERSION =~ ^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$ ]] || {
  echo "version must be vX.Y.Z" >&2; exit 2;
}
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
  docker exec "$CONTROL" docker rm -f aiflow4-acceptance-provider >/dev/null 2>&1 || true
  docker rm -f "${NODES[@]}" "$CONTROL" >/dev/null 2>&1 || true
  docker volume rm "$NAME-docker" >/dev/null 2>&1 || true
  docker network rm "$NETWORK" >/dev/null 2>&1 || true
  [[ -z $ARTIFACTS ]] || rm -rf "$ARTIFACTS"
}
trap cleanup EXIT
fail() { echo "CLEAN INSTALL FAILED: $*" >&2; exit 1; }

# Use the installer shipped for the version under test. Published runs follow the documented raw tag URL;
# candidate runs extract the helper from the exact tarball that the installer will verify and install.
if [[ $CANDIDATE == 1 ]]; then
  "$COSIGN" verify-blob "$ARTIFACTS/aiflow4-$VERSION.tar.gz" \
    --bundle "$ARTIFACTS/aiflow4-$VERSION.tar.gz.sigstore.json" \
    --certificate-identity "https://github.com/jooservices/ai-flow/.github/workflows/release-v4.yml@refs/tags/$VERSION" \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com \
    || fail "candidate tarball signature verification failed"
  CONTROL_INSTALLER="$ARTIFACTS/aiflow4-control-install"
  tar -xOf "$ARTIFACTS/aiflow4-$VERSION.tar.gz" deploy/v4/bin/aiflow4-control-install > "$CONTROL_INSTALLER" \
    || fail "candidate tarball does not contain deploy/v4/bin/aiflow4-control-install"
else
  ARTIFACTS=$(mktemp -d "${RUNNER_TEMP:-/tmp}/aiflow4-clean-release.XXXXXX")
  CONTROL_INSTALLER="$ARTIFACTS/aiflow4-control-install"
  curl -fsSL --max-time 120 \
    "https://raw.githubusercontent.com/jooservices/ai-flow/$VERSION/deploy/v4/bin/aiflow4-control-install" \
    -o "$CONTROL_INSTALLER" || fail "could not fetch the control installer from release tag $VERSION"
fi
[[ -s $CONTROL_INSTALLER ]] || fail "control installer for $VERSION is empty"
chmod 0755 "$CONTROL_INSTALLER"

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
  --volume "$CONTROL_INSTALLER:/opt/aiflow4-control-install:ro"
  --volume "$ROOT/deploy/v4/ci/acceptance-backup.py:/opt/aiflow4-acceptance-backup.py:ro"
  --volume "$ROOT/deploy/v4/ci/acceptance-providers.py:/opt/aiflow4-acceptance-providers.py:ro")
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
    "$CONTROL" bash /opt/aiflow4-control-install --version "$VERSION" --domain "$DOMAIN"
else
  docker exec "$CONTROL" bash /opt/aiflow4-control-install --version "$VERSION" --domain "$DOMAIN"
fi

echo "== 1a. connect disposable Jira and GitHub test accounts for the documented project flow"
CONTROL_IMAGE=$(docker exec "$CONTROL" docker compose -f /srv/aiflow/control/compose.yaml config --images | head -n1)
[[ -n $CONTROL_IMAGE ]] || fail "could not resolve the installed control image"
docker exec "$CONTROL" bash -se -- "$CONTROL_IMAGE" <<'FAKES' || fail "test provider setup"
set -euo pipefail
install -d -m 0700 /root/aiflow4-acceptance-pki /etc/aiflow/secrets/github
openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
  -subj '/CN=AI Flow acceptance test root' \
  -keyout /root/aiflow4-acceptance-pki/ca.key -out /root/aiflow4-acceptance-pki/ca.crt >/dev/null 2>&1
openssl req -newkey rsa:2048 -nodes -subj '/CN=api.github.com' \
  -keyout /root/aiflow4-acceptance-pki/server.key -out /root/aiflow4-acceptance-pki/server.csr >/dev/null 2>&1
printf 'subjectAltName=DNS:api.github.com,DNS:jooservices.atlassian.net\nextendedKeyUsage=serverAuth\n' \
  > /root/aiflow4-acceptance-pki/server.ext
openssl x509 -req -days 1 -in /root/aiflow4-acceptance-pki/server.csr \
  -CA /root/aiflow4-acceptance-pki/ca.crt -CAkey /root/aiflow4-acceptance-pki/ca.key \
  -CAcreateserial -extfile /root/aiflow4-acceptance-pki/server.ext \
  -out /root/aiflow4-acceptance-pki/server.crt >/dev/null 2>&1
openssl genrsa -out /etc/aiflow/secrets/github/app.pem 2048 >/dev/null 2>&1
chmod 600 /etc/aiflow/secrets/github/app.pem
printf '%s\n' '{"id":"424242","slug":"aiflow-acceptance","installation_id":1,"account_login":"acceptance","account_type":"Organization"}' \
  > /etc/aiflow/secrets/github/app.json
chmod 600 /etc/aiflow/secrets/github/app.json
install -m 0644 /root/aiflow4-acceptance-pki/ca.crt /etc/aiflow/secrets/control/acceptance-ca.crt
cat >> /etc/aiflow/secrets/control/aiflow4.env <<'ENV'
AIFLOW4_JIRA_SITE=https://jooservices.atlassian.net
JIRA_EMAIL=acceptance@example.test
JIRA_API_TOKEN=test-jira-token
SSL_CERT_FILE=/etc/aiflow/secrets/control/acceptance-ca.crt
ENV
docker run --detach --user 0 --entrypoint python3 --name aiflow4-acceptance-provider --network aiflow4-control \
  --network-alias api.github.com --network-alias jooservices.atlassian.net \
  --volume /opt/aiflow4-acceptance-providers.py:/opt/providers.py:ro \
  --volume /root/aiflow4-acceptance-pki:/pki:ro \
  --volume /srv/aiflow/control/repos/aiflow-control:/repo:rw \
  "$1" /opt/providers.py --cert /pki/server.crt --key /pki/server.key \
  --repo /repo >/dev/null
docker compose -f /srv/aiflow/control/compose.yaml up -d --no-deps --force-recreate control-api-a control-api-b
for _ in $(seq 1 60); do
  curl -fsS -H 'Host: control.clean.test' http://127.0.0.1/healthz >/dev/null && exit 0
  sleep 2
done
echo 'the control API did not restart with the test accounts' >&2
exit 1
FAKES
docker exec "$CONTROL" grep -qx 'AIFLOW4_CONSOLE_LOGIN=1' /etc/aiflow/secrets/control/aiflow4.env \
  || fail "the install did not turn console sign-in on"

echo "== 2. browser setup creates the first Administrator"
in_control <<CHECK || fail "sign-in checks"
set -euo pipefail
code() { curl -s -o /dev/null -w '%{http_code}' -H '$HOSTHDR' "\$@"; }
[[ \$(code $URL/healthz) == 200 ]]
[[ \$(code $URL/v1/state) == 401 ]]
[[ \$(code $URL/cc) == 302 ]]
jar=/root/aiflow4-acceptance-cookie.jar
umask 077
password_file=/root/aiflow4-acceptance-admin-password
python3 -c 'import secrets,sys; open(sys.argv[1],"w").write(secrets.token_urlsafe(24))' "\$password_file"
chmod 600 "\$password_file"
setup=\$(curl -fsS -c "\$jar" -H '$HOSTHDR' $URL/v1/setup/status)
setup_token=\$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["bootstrap_token"])' "\$setup")
body=\$(python3 -c 'import json,sys; print(json.dumps({"public_url":"http://control.clean.test","email":sys.argv[1],"password":open(sys.argv[2]).read()}))' '$ADMIN' "\$password_file")
[[ \$(curl -s -o /dev/null -w '%{http_code}' -H '$HOSTHDR' -H "X-AIFLOW4-Setup: \$setup_token" -H 'Content-Type: application/json' -b "\$jar" -c "\$jar" --data-binary @- $URL/v1/setup/bootstrap <<<"\$body") == 200 ]]
[[ \$(code -b "\$jar" $URL/v1/state) == 200 ]]
CHECK

echo "== 2a. add the test project through Command Center"
in_control <<PROJECT || fail "Command Center project setup"
set -euo pipefail
jar=/root/aiflow4-acceptance-cookie.jar
catalog=\$(curl -fsS -b "\$jar" -H '$HOSTHDR' $URL/v1/projects/catalog)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert "CLEAN" in {p["key"] for p in d["jira_projects"]}; assert "acceptance/clean-install" in {r["full_name"] for r in d["github_repos"]}' "\$catalog"
curl -fsS -b "\$jar" -H '$HOSTHDR' $URL/projects -o /dev/null
body='{"site":"https://jooservices.atlassian.net","project":"CLEAN","repo":"acceptance/clean-install","base":"main","stack":"","checks":[],"priority":3,"parallel_limit":2,"merge_method":"squash"}'
preflight=\$(curl -fsS -b "\$jar" -H '$HOSTHDR' -H 'Content-Type: application/json' --data "\$body" $URL/v1/projects/preflight)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["ok"] is True and d["project"] == "CLEAN"' "\$preflight"
saved=\$(curl -fsS -b "\$jar" -H '$HOSTHDR' -H 'Content-Type: application/json' --data "\$body" $URL/v1/projects)
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); assert d["saved"] is True and d["project"] == "CLEAN"' "\$saved"
grep -q '^key: CLEAN$' /srv/aiflow/control/repos/aiflow-control/projects/CLEAN.yaml
project_ready=0
for _ in $(seq 1 30); do
  projects=\$(curl -fsS -b "\$jar" -H '$HOSTHDR' $URL/v1/projects)
  if python3 -c 'import json,sys; assert "CLEAN" in {p["key"] for p in json.loads(sys.argv[1])["projects"]}' "\$projects"; then
    project_ready=1
    break
  fi
  sleep 2
done
[[ \$project_ready == 1 ]] || { echo 'Command Center project did not become visible' >&2; exit 1; }
PROJECT

echo "== 3. two machines get one-use join commands from Command Center and run the documented bootstrap"
state_nodes() {  # the nodes of /v1/state, read with the owner token (name:status per line)
  in_control <<STATE
set -euo pipefail
. /etc/aiflow/secrets/control/aiflow4.env
curl -fsS -H '$HOSTHDR' -H "Authorization: Bearer \$AIFLOW4_OWNER_TOKEN" $URL/v1/state \
  | python3 -c 'import json,sys; [print(n["name"] + ":" + str(n.get("status"))) for n in json.load(sys.stdin)["nodes"]]'
STATE
}
join_command() {  # join_command <machine> <file>: use the authenticated Command Center action
  in_control <<ISSUE
set -euo pipefail
umask 077
curl -fsS -b /root/aiflow4-acceptance-cookie.jar -H '$HOSTHDR' -H 'Content-Type: application/json' \
  --data '{"name":"$1","role":"worker","project":"CLEAN"}' \
  $URL/v1/nodes/$1/join-command > /tmp/aiflow4-join-command.json
python3 -c 'import json,shlex,sys; p=shlex.split(json.load(open(sys.argv[1]))["command"]); print(p[2]); print(p[-1])' \
  /tmp/aiflow4-join-command.json > "$2"
chmod 600 "$2"
rm -f /tmp/aiflow4-join-command.json
ISSUE
}
for i in 0 1; do
  node=${NODES[$i]}; machine="clean-node-$i"; tokenfile="/tmp/join-$i.token"
  issued=0
  for _ in $(seq 1 30); do  # project state is reloaded independently by each control API replica
    if join_command "$machine" "$tokenfile"; then issued=1; break; fi
    sleep 2
  done
  [[ $issued == 1 ]] || fail "Command Center join command for $machine"
  # The token is streamed from one container into a private file on the other; it is never printed.
  docker exec "$CONTROL" cat "$tokenfile" \
    | docker exec -i "$node" bash -c 'umask 077; cat > /root/join-token' || fail "$machine token delivery"
  docker exec "$CONTROL" rm -f "$tokenfile"
  # Run the documented bootstrap without the prepared flag so the pinned server-installer installs and verifies
  # packages, time sync, Docker and Docker permissions on this clean machine.
  docker exec -i "$node" bash -se <<'JOIN' || fail "$machine join bootstrap"
set -euo pipefail
mapfile -t join_fields < /root/join-token
rm -f /root/join-token
[[ ${#join_fields[@]} == 2 ]] || { echo "Command Center returned an invalid join command" >&2; exit 1; }
install_url=${join_fields[0]}
token=${join_fields[1]}
curl -fsSL "$install_url" \
  | sh -s -- --token "$token"
JOIN
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
body=\$(python3 -c 'import json,sys; print(json.dumps({"email": sys.argv[1], "password": open("/root/aiflow4-acceptance-admin-password").read().strip()}))' '$ADMIN')
[[ \$(curl -s -o /dev/null -w '%{http_code}' -H '$HOSTHDR' -H 'Content-Type: application/json' --data-binary @- $URL/v1/auth/login <<<"\$body") == 200 ]]
CHECK
echo "clean install on real machines passed: sign-in on, Command Center join commands, two machines online, backup and restore"
