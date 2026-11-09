# Explained broad exception handlers, first batch

Ten `except Exception` (or `except BaseException`) handlers now carry the reason they are broad, in the form the
broad-except guard accepts (`# noqa: BLE001 - <reason>`), and one handler (`worker/quota_cli.py`) is narrowed to
`OSError`, `TypeError` and `ValueError`. Each change has a small test that pins the behaviour the reason describes, and
the baseline `docs/v4/broad-except-baseline.json` shrank accordingly (it can only shrink; see [broad-except.md](broad-except.md)).

Behaviour is unchanged. The routes below contain a handler that was explained; their answers are the same as before.

## Routes

- `POST /v1/knowledge/resources` saves a control-repository resource; any failure of the GitHub, database or event step answers one 502.
- `POST /v1/knowledge/resources/{kind}/{resource_id}/restore` restores a saved resource; any failure of its steps answers one 502.
- `POST /v1/work/claim` hands out the next piece of work; a failing optional comment lookup never blocks a claim.
- `POST /v1/projects/{key}/close` closes a project and stores its report; a failing notification never fails the close.
- `POST /v1/setup/sample-ticket` creates the sample ticket of the setup wizard; any Jira, credential or storage error becomes a 502 and not a 500.
