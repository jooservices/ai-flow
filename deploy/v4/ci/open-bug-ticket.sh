#!/usr/bin/env bash
# Open a bug ticket (a GitHub issue labelled `bug`) for a failed acceptance run, with the log tail in the body.
# usage: open-bug-ticket.sh "<title>"   (GH_TOKEN, GITHUB_REPOSITORY, GITHUB_RUN_ID, GITHUB_SERVER_URL from Actions)
# The failed job uploads its logs; the issue job downloads them into $ACCEPTANCE_LOG_DIR before calling this script.
set -Eeuo pipefail

TITLE=${1:?usage: open-bug-ticket.sh "<title>"}
LOGS=${ACCEPTANCE_LOG_DIR:-${RUNNER_TEMP:-${TMPDIR:-/tmp}}/aiflow-acceptance-logs}
RUN_URL="${GITHUB_SERVER_URL:-https://github.com}/${GITHUB_REPOSITORY:-}/actions/runs/${GITHUB_RUN_ID:-0}"
if ! compgen -G "$LOGS/*.log" >/dev/null; then
  echo "no acceptance log files found in $LOGS; refusing to open a bug ticket without logs" >&2
  exit 1
fi
BODY=$(mktemp)
FULL=$(mktemp)
trap 'rm -f "$BODY" "$FULL"' EXIT

{
  echo "The clean-install acceptance test failed: $RUN_URL"
  echo
  echo "Ref: \`${GITHUB_REF_NAME:-unknown}\` at \`${GITHUB_SHA:-unknown}\`, event \`${GITHUB_EVENT_NAME:-unknown}\`."
  echo "Fix the cause and add a case for it to \`tests/v4/test_clean_install_acceptance.py\`."
  echo
  echo "## Log (tail)"
  echo '```'
  for f in "$LOGS"/*.log; do echo "--- $(basename "$f")"; tail -n 60 "$f"; done
  echo '```'
} > "$FULL"
# cut to the size limit from a file: `head` on a pipe would end the writer with SIGPIPE and, with pipefail, abort here
head -c 60000 "$FULL" > "$BODY"
# a closing fence cut off by the size limit would hide the end of the issue
[[ $(grep -c '^```' "$BODY") -eq 2 ]] || echo '```' >> "$BODY"

if [[ -z ${GH_TOKEN:-} ]]; then
  echo "no GH_TOKEN: would open '$TITLE'" >&2; cat "$BODY"; exit 1
fi
gh label create bug --repo "$GITHUB_REPOSITORY" --color d73a4a --description "Something is broken" >/dev/null 2>&1 || true
gh issue create --repo "$GITHUB_REPOSITORY" --title "$TITLE" --label bug --body-file "$BODY"
