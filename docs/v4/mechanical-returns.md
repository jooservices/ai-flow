# Fewer mechanical verification returns

**Console screens:** Command Center (`cc`), Delivery (`delivery`).

Verification used to send work back for reasons the developer step can catch itself: a branch that had fallen
behind the base, a real merge conflict, or a documentation (or other fast pipeline) check that was already red.
Each return costs another developer run, another verification and another queue slot.

## Before hand-over (`aiflow/v4/worker/mechanical.py`)

After the developer AI finishes, and before the draft pull request is handed to verification, AI Flow:

1. Fetches the latest base and merges it into the ticket branch (`gitops.merge_base_resolving`).
2. Resolves conflicts whose both sides are the same blob (identical content) automatically.
3. Refuses hand-over when a real conflict remains, naming the conflicting files, and sends the ticket back to
   To Do.
4. Runs the pipeline's fast checks when the project uses `local_gate: ai-flow` (every local-gate step that needs
   no test service, including the documentation check). A red check refuses hand-over; nothing is pushed.
   Projects with a custom `local_gate` command list keep that gate. Projects without `local_gate` still get the
   base refresh.

The same refresh and checks run on the "already done" path that reuses an existing branch or pull request.

## Before verification (`aiflow/v4/worker/verify.py`)

When a branch becomes stale while the ticket waits in In Verify, the verify step merges the latest base into the
detached pull-request head. Identical-sided conflicts are resolved automatically. Only a real conflict goes back
to development, with the conflicting files named in the findings. A clean behind branch is still brought up to
date on GitHub when GitHub allows it (`docs/v4/merge-conflicts.md`).

## Dashboard yield

`GET /v1/dashboard/yield?days=14` returns, for the caller's project scope:

| Field | Meaning |
| --- | --- |
| `merged_per_100_runs` | Merged pull requests per 100 completed developer runs (`step=dev` with an end time) |
| `runs_per_merged` | Completed developer runs per merged pull request |
| `tokens_per_merged` | Tokens used on completed developer runs per merged pull request |
| `target_merged_per_100` | 40 |

The Command Center Dashboard Work tab shows these figures for the last 14 UTC days and a per-day table. Tone is
good at or above 40 merged per 100 completed developer runs.
