#!/usr/bin/env bash
# Clean-install acceptance test (docs/v4/clean-install-acceptance.md).
#   fast  every pull request touching install, deploy, control or node code: the acceptance cases of
#         tests/v4/test_clean_install_acceptance.py (clean control machine and two machines with sign-in on,
#         tickets through Intake, development, verification and merge, Human review, policy calls, deploy
#         scripts, backup and restore, upgrade and rollback, adding a machine and an AI account)
#   full  nightly and before every release: fast, then the whole tests/v4 suite and, when
#         ACCEPTANCE_PREVIOUS and ACCEPTANCE_TARGET name two published releases, the systemd containers
#         (control-install-smoke.sh: install, rerun, failed upgrade, upgrade, rollback) and the real clean machines
#         (clean-install-containers.sh: control plane and two machines in containers, sign-in on, backup and restore)
# The output of every step goes to $ACCEPTANCE_LOG_DIR (default acceptance-logs) for the bug ticket.
set -Eeuo pipefail

MODE=${1:-}
[[ $MODE == fast || $MODE == full ]] || { echo "usage: clean-install-acceptance.sh fast|full" >&2; exit 2; }
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
LOGS=${ACCEPTANCE_LOG_DIR:-$ROOT/acceptance-logs}
mkdir -p "$LOGS"
cd "$ROOT"

step() {  # step <log name> <command...>: run it, keep the output, name the failed step
  local name=$1; shift
  echo "==> $name"
  if ! "$@" 2>&1 | tee "$LOGS/$name.log"; then
    echo "ACCEPTANCE FAILED: $name (log: $LOGS/$name.log)" >&2
    return 1
  fi
}
pipefail_step() { set -o pipefail; step "$@"; }

for script in deploy/v4/bin/* deploy/v4/ci/*.sh; do
  if head -n1 "$script" | grep -q python; then  # a few helpers are Python
    pipefail_step "syntax-$(basename "$script")" python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$script"
  else
    pipefail_step "syntax-$(basename "$script")" bash -n "$script"
  fi
done
pipefail_step acceptance-cases uv run --frozen pytest tests/v4/test_clean_install_acceptance.py -q -p no:warnings

if [[ $MODE == full ]]; then
  pipefail_step v4-suite uv run --frozen pytest tests/v4 -q -p no:warnings
  if [[ -n ${ACCEPTANCE_PREVIOUS:-} && -n ${ACCEPTANCE_TARGET:-} ]]; then
    pipefail_step control-install-systemd bash deploy/v4/ci/control-install-smoke.sh "$ACCEPTANCE_PREVIOUS" "$ACCEPTANCE_TARGET"
    pipefail_step clean-machines-containers bash deploy/v4/ci/clean-install-containers.sh "$ACCEPTANCE_TARGET"
  else
    echo "no two published releases to install; the systemd containers run in the release workflow" | tee "$LOGS/control-install-systemd.log"
  fi
fi
echo "clean-install acceptance ($MODE) passed"
