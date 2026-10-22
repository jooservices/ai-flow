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
6. A second ticket labelled `require-human` reaches the **Human review** screen (`/review`, see
   [Human review queue](reviewer-queue.md)).

Further cases in the same file:

| Case | What is checked with sign-in on |
|---|---|
| `test_machines_policy_calls_and_heartbeats_work_with_sign_in_on` | policy decisions (`/v1/policy/decide`), claims and heartbeats from a client without the console cookie |
| `test_deploy_and_canary_scripts_read_the_state_with_the_owner_token_when_sign_in_is_on` | `aiflow4-autodeploy` and the develop variant read `/v1/state` with the owner token from the control secret file; all deploy scripts parse |
| `test_backup_and_restore_bring_back_users_projects_and_machines` | a logical backup of every table, a wipe, the restore; the Super Administrator signs in again and the secret files stay `0600` |
| `test_upgrade_keeps_sign_in_and_data_and_rollback_returns_to_the_previous_release` | upgrade keeps secrets and the sign-in setting, rollback returns to the previous release, an unhealthy upgrade undoes itself (exit 5) |
| `test_adding_a_machine_and_an_ai_account_while_sign_in_is_on` | a third machine joins and its new AI account is assigned by the next heartbeat |
| `test_pull_requests_nightly_and_releases_run_the_acceptance_test_and_failures_open_a_bug` | the CI wiring below |

The control host, Jira and GitHub are the fakes used by the other v4 tests, so the test needs only the shared
Redis and PostgreSQL. The systemd tier with real containers is `deploy/v4/ci/install-smoke.sh` (signed release,
two nodes, canary, failed install and rollback, [Release installation smoke test](install.md)) and
`deploy/v4/ci/control-install-smoke.sh` (install, rerun, failed upgrade, upgrade, rollback).

## When it runs

`deploy/v4/ci/clean-install-acceptance.sh fast|full` is the single entry point; it keeps one log per step in
`acceptance-logs/`.

| Run | Trigger | Content |
|---|---|---|
| fast | every pull request that changes `aiflow/v4/install`, `aiflow/v4/control`, `aiflow/v4/node`, `deploy/v4` or the workflows (`acceptance-fast` in `ci.yml`) | syntax check of every deploy script and the acceptance cases |
| full, nightly | `schedule` at 02:17 UTC and `workflow_dispatch` (`acceptance-full` in `ci.yml`) | fast, the whole `tests/v4` suite, and the systemd control install, upgrade and rollback between the two newest published releases |
| full, release | every `v*` tag (`acceptance` in `release-v4.yml`) | fast and the whole `tests/v4` suite; the signed-tarball systemd install smoke of the release job follows |

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
