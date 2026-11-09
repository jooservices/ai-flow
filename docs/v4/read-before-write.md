# Read-before-write brief and study cases

A developer run proves it looked before it wrote, and receives only the lessons that match its ticket. Both parts are
scripts: they cost no AI tokens to run.

## Steps

1. **Read brief** (`prompts/v4/dev.md`, step 0). Before changing code the AI reads the files the ticket names and writes
   one line per file: `READ: <path>: <name>, <name>`. It checks the brief from the worktree with
   `python3 -m aiflow.v4.core.read_brief` (stdin), which fails when a file is missing, a path leaves the worktree, or a
   function or class is not defined in the file. The AI may not cite a name it did not find in the code.
2. **Study cases** (`prompts/v4/study-cases.json`). Each case has an `id`, `title`, `defect`, `cause` (required: a case
   without one is never delivered), `fix`, `signature` (lower-case terms that appear in a finding when the defect comes
   back), `areas` (path prefixes; empty means general) and `task_types` (`bug` or `change`; empty means any).
3. **Retrieval** (`aiflow/v4/core/study_cases.py`, `deliver_lessons`). Before a developer run starts, the paths and the
   task type of the ticket select the cases. The most specific cases come first, and the section stops before
   `TOKEN_CAP` tokens (600, estimated at 4 characters per token). The section is appended to the end of the prompt, so
   a run with no matching case sees the prompt unchanged.
4. **Ledger** (`<srv_root>/logs/study-case-ledger.jsonl`). Each run appends the case ids it was given and the ids
   whose signature appears again in the findings of an earlier round of the same ticket (`repeated`).

## How to add

- A new defect: add one object to `prompts/v4/study-cases.json` with a unique `id`, a short `cause` and a `fix` that
  names a check or a test. Keep `defect` and `fix` to one sentence each.
- A new area: add a path prefix to `areas`; the ticket must name a path that starts with it.
- A new check for the brief: extend `core/read_brief.py`; keep it a plain function with explicit arguments.

The tests are `tests/v4/test_study_cases.py`, `tests/v4/test_read_brief.py` and
`tests/v4/test_read_before_write.py`, which pins the developer prompt for a run without lessons.
