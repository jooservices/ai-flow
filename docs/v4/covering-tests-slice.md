# Covering tests first and a reserved full-suite slice

The developer prompt used to require every project check before `RESULT: DONE`. For ai-flow that is the whole
`tests/v4` suite, which was still running when the hard clock killed the run.

The prompt now tells the AI to run the checks that cover the changed code first, then run the full configured
suite exactly once at the end, and to reserve a 15-minute test time slice for that attempt. An unfinished suite
is reported as `FULL SUITE: UNFINISHED - <why>` and is not a reason to withhold `RESULT: DONE`. A finished
suite is `FULL SUITE: FINISHED`.

This page does not change claim SQL, the timeout increment, or the Dashboard.

**Console screens:** none (developer prompt; no UI in this ticket).
