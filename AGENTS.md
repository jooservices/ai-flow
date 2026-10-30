# Instructions for AI developers and reviewers of AI Flow

This repository holds two generations of AI Flow. The current work is **v4**, a rewrite next to the old code.

- Change only `aiflow/v4/`, `prompts/v4/`, `deploy/v4/`, `tests/v4/` and `docs/v4/`, unless the ticket says
  otherwise. The old flow (everything else) keeps running for other projects; never edit it by accident.
- Every new feature for end users must have a UI path. CLI is only for developers and CI.
- v4 is Python 3.12 with uv: FastAPI control API (`aiflow/v4/control`), workers and adapters
  (`aiflow/v4/worker`, `aiflow/v4/adapters`), node agent (`aiflow/v4/node`), shared core (`aiflow/v4/core`).
  Read the module docstrings first; they explain the design (leases and fencing, policy hook, verify, watchdog).
- Run `uv run --frozen pytest tests/v4 -q`. The environment provides `AIFLOW4_TEST_REDIS_URL` and
  `AIFLOW4_TEST_DATABASE_URL` (shared test services: Redis keys are prefixed per test; each pytest session
  creates its own database on that PostgreSQL server (copied from `AIFLOW4_TEST_DATABASE_TEMPLATE` when set, a database that already carries pgvector) and drops it at the end, so parallel runs do not interfere).
- New control-plane code follows `docs/v4/event-architecture.md`: a feature folder (`routes`, `service`, `repository`,
  `models`, `events`, `listeners`), a reaction to a state change is a listener of a domain event, and `control/api.py`
  does not grow. `tests/v4/test_feature_structure.py` enforces the import rules.
- Do not add dependencies unless the ticket asks for them; `uv.lock` must stay consistent with `pyproject.toml`.
- Document a new feature in its own file `docs/v4/<topic>.md` and add one line linking it under "Topics" in
  `deploy/v4/README.md`. Many tickets run in parallel: appending long sections to one shared file causes merge
  conflicts on every branch.
- Do not commit, push or change branches: AI Flow commits your work (a resolved merge included) and opens the
  pull request. Finishing with uncommitted changes is expected.
- Never print secrets, tokens or the contents of `/etc/aiflow`.
- Commit and pull request titles are `type: Subject`, normally ending with the ticket key `(KEY-1)` (a non-ASCII subject
  starts with the key instead: `type: KEY-1: subject`): type is feat, fix, docs, style, refactor, perf, test, build,
  ci, chore or revert; a commit subject does not start with a lower-case letter (a pull request title starts with a
  capital letter), has no full stop at the end, and the header is 10 to 120 characters. Run `python3 -m aiflow.v4.core.local_gate install-hooks` once per clone: the `commit-msg` hook rejects a bad
  message when you commit, and the `pre-push` hook runs the CI checks before anything is pushed (`docs/v4/local-gate.md`).
