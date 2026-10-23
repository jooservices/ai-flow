# Clean-install acceptance test

Unit tests missed the problems of the first real day: tools, services and permissions missing on machines, a sign-in
gate that blocked the machines' own calls, deploy scripts that failed with sign-in on, and helpers that were never
shipped. The clean-install acceptance test proves, before every release, that a fresh installation works from the
first command to a merged ticket.

## What it proves

`tests/v4/test_clean_install_acceptance.py` builds a clean control machine and two clean machines with console
sign-in on, and takes only the documented steps:

1. Install the control plane with `aiflow4-control-install --version vX.Y.Z --domain D --admin-email E`
   ([Install the control plane](control-install.md)). The install turns sign-in on and writes the first Super
   Administrator's password to `super-admin-password`.
2. Open `/login` and sign in as the Super Administrator. Anonymous callers get a redirect or `401`.
3. In Command Center, open **Projects** and add the project (Jira key, repository, base branch).
4. For each machine, copy the one-use join command from Command Center ([Join a node](node-join.md)). Machines
   never sign in; they use their node token and the run token.
5. Create a sample ticket in Jira. It passes **Intake** (the refiner rewrites it), **development** on one machine,
   **verification** on the other machine, and **merge**.
6. A second ticket labelled `require-human` goes through development and verification the same way; the verifier
   hands it to a person (the fenced Jira transition to **Human review**), nothing is merged, and the ticket is listed
   on the **Human review** screen (`/review`, see [Human review queue](reviewer-queue.md)).

Further cases in the same file:

| Case | What is checked with sign-in on |
|---|---|
| `test_machines_policy_calls_and_heartbeats_work_with_sign_in_on` | policy decisions (`/v1/policy/decide`), claims and heartbeats from a client without the console cookie |
| `test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on` | `aiflow4-autodeploy` and `aiflow4-autodeploy-develop` are run against a mock control endpoint with sign-in on: every `/v1/state` read carries the owner token from the installed secret file, the promoted release is kept, the token is never printed |
| `test_a_failed_state_health_check_rolls_the_deploy_back` | the same scripts with a refused `/v1/state` call the release unhealthy and deploy the previous release again |
| `test_ci_backup_and_restore_keep_sign_in_machine_and_integration_credentials_working` | CI-only backup/restore: after restoring freshly migrated tables, the Super Administrator signs in, a machine's old node token works, Jira refreshes its OAuth token, the GitHub App issues an installation token, and the restored event sequence generates a new id |
| `test_upgrade_keeps_sign_in_and_data_and_rollback_returns_to_the_previous_release` | upgrade keeps secrets and the sign-in setting, rollback returns to the previous release, an unhealthy upgrade undoes itself (exit 5) |
| `test_adding_a_machine_and_an_ai_account_while_sign_in_is_on` | a third machine joins and its new AI account is assigned by the next heartbeat |
| `test_pull_requests_nightly_and_releases_run_the_acceptance_test_and_failures_open_a_bug` | the CI wiring below |

The pytest tier runs the real installer against a fake host, the real control API with sign-in on, and Jira and
GitHub fakes (the test accounts), so it needs only the shared Redis and PostgreSQL. The deploy scripts run for real
with stubs for `docker`, `sleep` and the deploy helpers; `AIFLOW4_CONTROL_URL`, `AIFLOW4_CONTROL_ENV`,
`AIFLOW4_ALERT_HOST` and `AIFLOW4_ALERT_PORT` point them at the mock control endpoint (the defaults are the
production values).

The real-machine tier needs Docker and a signed release. Nightly runs use a published release; before publication the
release workflow supplies its signed candidate tarball, Sigstore bundle and checksum manifest:

- `deploy/v4/ci/clean-install-containers.sh vX.Y.Z` tests a published release. For an unpublished candidate, use
  `deploy/v4/ci/clean-install-containers.sh vX.Y.Z TAR SIGSTORE_BUNDLE SHA256SUMS`. It builds a clean control machine and two clean machines (Ubuntu
  systemd containers on a private network), installs the control plane with the documented command and
  `--admin-email`, checks that anonymous callers are refused, signs in with the password file, runs the documented join bootstrap (`curl .../v1/install.sh | sh -s -- --token-file F`, with a one-use join token) on both machines, which installs the signed release and starts the node agent,
  checks that both agents report online with their node token only, reads `/v1/state` with the owner token,
  then runs the CI-only backup helper, a wipe and restore. The restore refuses, before changing anything, an archive
  with any entry other than `manifest.json`, `db/<table>.copy` of a listed table and
  `secrets/{control,console,jira,github}/<file name>`. Active Jira credentials and GitHub App metadata and private
  keys are included; the test verifies that Jira can refresh and the GitHub App can issue an installation token after
  restore. The backup is written through a private temporary file, so it is `0600` even over an older file.
- `deploy/v4/ci/control-install-smoke.sh` exercises install, rerun, failed upgrade, upgrade and rollback.
- `deploy/v4/ci/install-smoke.sh` supplements the documented fresh install with candidate canary, wave, unhealthy-node and fleet rollback checks.

## Backup and restore in acceptance

Backup and restore are CI-only acceptance helpers, not operator commands. The control plane has no Command Center
backup flow yet, so the helper is intentionally not registered under `aiflow4`. The acceptance archive holds every
registry table (users, projects, machines and their tokens, tickets, runs), the secret files in
`/etc/aiflow/secrets/{control,console,jira,github}`, and the release (`CURRENT`) it was taken on. Restore runs after
the release schema exists, replaces the registry contents, resets each owned sequence to the restored maximum and
writes secret directories as `0700` with files at `0600`. The test verifies console sign-in, machine tokens, Jira OAuth
refresh and GitHub App token issuance after restore. The archive does not contain the PostgreSQL, Redis or OpenObserve
data directories; Redis leases and caches are rebuilt.

## When it runs

`deploy/v4/ci/clean-install-acceptance.sh fast|full` is the single entry point; it keeps one log per step in
`aiflow-acceptance-logs/` in the runner temp directory.

| Run | Trigger | Content |
|---|---|---|
| fast | every pull request that changes `aiflow/v4/install`, `aiflow/v4/control`, `aiflow/v4/node`, `deploy/v4` or the workflows (`acceptance-fast` in `ci.yml`) | syntax check of every deploy script and the acceptance cases |
| full, nightly | `schedule` at 02:17 UTC and `workflow_dispatch` (`acceptance-full` in `ci.yml`) | fast and the whole `tests/v4` suite; when two published releases are available, the sign-in-enabled clean control and two-machine container run against the newer release, plus systemd control install, upgrade and rollback between them |
| full, release | every `v*` tag (`acceptance` in `release-v4.yml`) | fast and the whole `tests/v4` suite; when two published releases are available, the sign-in-enabled clean install and control upgrade/rollback checks run against them |

After signing the candidate tarball, the release job passes that exact tarball, Sigstore bundle and checksum manifest
to `clean-install-containers.sh` before it pushes the control image or creates the GitHub Release. The test stages the
candidate locally, then uses the documented `aiflow4-control-install` command and `/v1/install.sh` join bootstrap on
the control machine and both nodes. The candidate's control image is built from the signed release contents inside
the temporary control machine, so no unpublished artifact is fetched from GitHub or GHCR.

The release job `needs` the `acceptance` job, so a failure blocks the GHCR image and the GitHub Release. When a
nightly or release run fails, `deploy/v4/ci/open-bug-ticket.sh` opens a bug ticket (a GitHub issue labelled `bug`)
naming the run, the ref and the tail of every step log.

## Adding a case for a problem found later (AC5)

For every problem found in real use: write the case in `test_clean_install_acceptance.py` that fails without the
fix, then add one line to `REGRESSIONS` there (problem and test name) and one row to the table above. The last test
fails when a listed case is missing or undocumented.

Known problems covered so far:

- the sign-in gate blocked the machines' own calls: `test_machines_policy_calls_and_heartbeats_work_with_sign_in_on`
- deploy and canary scripts failed with sign-in on (`/v1/state` answered `401`):
  `test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on`
- backup omitted active Jira and GitHub App credentials: `test_ci_backup_and_restore_keep_sign_in_machine_and_integration_credentials_working`
- backup/restore had no UI path, so its command surface stays CI-only:
  `test_restore_refuses_a_file_that_is_not_a_backup_and_backup_is_not_an_operator_cli`
- the bug ticket was not opened when a failed run's log was longer than the body limit (`head` closed the pipe and
  `pipefail` aborted the script): `test_the_bug_ticket_survives_a_log_longer_than_the_size_limit`
