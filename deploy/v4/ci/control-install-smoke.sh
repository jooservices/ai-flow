#!/usr/bin/env bash
# `aiflow4 install control` in a clean Ubuntu 24.04 systemd container (docs/v4/control-install.md):
#   1. install <previous> from scratch within 10 minutes; every component healthy
#   2. the same command again changes nothing (secret files, container ids, Postgres and Redis data)
#   3. an upgrade to <target> whose health check fails (simulated) rolls back to <previous> by itself
#   4. the upgrade to <target> succeeds, keeps the data and leaves <previous> as the rollback target
#   5. `aiflow4 install control --rollback` returns to <previous> and it runs
# Both versions must be published releases that ship deploy/v4/control/infra.yaml.
set -Eeuo pipefail

[[ $# -eq 2 ]] || { echo "usage: control-install-smoke.sh <previous vX.Y.Z> <target vX.Y.Z>" >&2; exit 2; }
PREVIOUS=$1
TARGET=$2
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
NAME="aiflow4-control-smoke-$$"
IMAGE="$NAME-systemd"
DOMAIN=control.smoke.test
cleanup() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker volume rm "$NAME-docker" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker build --pull -f "$ROOT/deploy/v4/ci/systemd.Dockerfile" -t "$IMAGE" "$ROOT"
# Docker runs inside the container (server-installer installs it); its data needs a real volume.
docker run --detach --name "$NAME" --privileged --cgroupns=host \
  --tmpfs /run --tmpfs /run/lock --tmpfs /tmp \
  --volume /sys/fs/cgroup:/sys/fs/cgroup:rw --volume "$NAME-docker:/var/lib/docker" \
  --volume "$ROOT/deploy/v4/bin/aiflow4-control-install:/opt/aiflow4-control-install:ro" "$IMAGE" >/dev/null
for _ in $(seq 1 30); do docker exec "$NAME" systemctl is-active --quiet basic.target && break; sleep 1; done

in_host() { docker exec -i "$NAME" bash -se; }
install_control() {  # install_control <version> [ENV=VALUE...]; prints the exit code
  local version=$1; shift
  docker exec "${@/#/--env=}" "$NAME" /usr/local/sbin/aiflow4 install control --version "$version" --domain "$DOMAIN" \
    && echo 0 || echo $?
}
# Hashes and ids only: the secret values never leave the container.
fingerprint() {
  in_host <<'STATE'
set -euo pipefail
sha256sum /etc/aiflow/secrets/control/*.env | awk '{print $1}'
stat -c '%a %n' /etc/aiflow/secrets/control /etc/aiflow/secrets/control/*.env
docker ps --filter name=aiflow4- --format '{{.Names}} {{.ID}} {{.Image}}' | sort
docker exec aiflow4-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "SELECT note FROM smoke_marker"'
docker exec aiflow4-redis redis-cli GET aiflow4:smoke-marker
cat /srv/aiflow/control/releases/CURRENT
STATE
}
current() { docker exec "$NAME" cat /srv/aiflow/control/releases/CURRENT; }
expect_running() {  # expect_running <version>: CURRENT, Command Center and Gatus
  [[ $(current) == "$1" ]] || { echo "CURRENT is $(current), expected $1" >&2; exit 1; }
  docker exec "$NAME" curl -fsS -o /dev/null http://127.0.0.1/cc || { echo "Command Center does not answer" >&2; exit 1; }
  docker exec "$NAME" curl -fsS -o /dev/null http://127.0.0.1:8081/health || { echo "Gatus does not answer" >&2; exit 1; }
}

echo "== 1. clean install of $PREVIOUS"
START=$SECONDS
docker exec "$NAME" bash /opt/aiflow4-control-install --version "$PREVIOUS" --domain "$DOMAIN"
ELAPSED=$((SECONDS - START))
echo "install took ${ELAPSED}s"
((ELAPSED <= 600)) || { echo "install of $PREVIOUS took longer than 10 minutes" >&2; exit 1; }
expect_running "$PREVIOUS"
in_host <<'MARK'
set -euo pipefail
docker exec aiflow4-postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -qc "CREATE TABLE smoke_marker (note text); INSERT INTO smoke_marker VALUES ('"'"'kept'"'"')"'
docker exec aiflow4-redis redis-cli SET aiflow4:smoke-marker kept >/dev/null
MARK
BEFORE=$(fingerprint)

echo "== 2. the same command again"
[[ $(install_control "$PREVIOUS") == 0 ]] || { echo "second run failed" >&2; exit 1; }
AFTER=$(fingerprint)
[[ $BEFORE == "$AFTER" ]] || { echo "second run changed state" >&2; diff <(echo "$BEFORE") <(echo "$AFTER") >&2 || true; exit 1; }
echo "second run left secrets, containers and data unchanged"

echo "== 3. failed upgrade to $TARGET rolls back"
RC=$(install_control "$TARGET" AIFLOW4_INSTALL_SIMULATE_UNHEALTHY=control-api-a)
[[ $RC == 5 ]] || { echo "a failed upgrade must exit 5 after rolling back, got $RC" >&2; exit 1; }
expect_running "$PREVIOUS"
echo "simulated unhealthy $TARGET rolled back to $PREVIOUS"

echo "== 4. upgrade to $TARGET"
[[ $(install_control "$TARGET") == 0 ]] || { echo "upgrade to $TARGET failed" >&2; exit 1; }
expect_running "$TARGET"
grep -qx kept <<<"$(fingerprint)" || { echo "upgrade lost the Postgres or Redis data" >&2; exit 1; }

echo "== 5. owner rollback"
docker exec "$NAME" /usr/local/sbin/aiflow4 install control --rollback
expect_running "$PREVIOUS"
[[ $(fingerprint | grep -cx kept) == 2 ]] || { echo "rollback lost the Postgres or Redis data" >&2; exit 1; }
echo "control install smoke passed: install ${ELAPSED}s, idempotent rerun, upgrade, automatic and owner rollback"
