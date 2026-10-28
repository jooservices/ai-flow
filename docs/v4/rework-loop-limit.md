# Rework loop limit

The supervisor counts one rework round each time a failed review, QA result, CI check or feedback sends a ticket back
to development. The global `loop_limit` defaults to 3: rounds one through three are allowed, and the fourth return
blocks the ticket as `Rework`. The Blocked comment includes the recorded failing reason for every round, and the
Rework incident is available to the configured rescue operator.

Set the global value in `[runtime]` of `aiflow.toml`:

```toml
[runtime]
loop_limit = 3
```

A project can override it in its `[project]` table:

```toml
[project]
jira_key = "JAIF"
repo = "ai-flow"
loop_limit = 5
```

The former `max_cycles` name remains accepted as an alias in either table. Do not set both names in one table; the
configuration check rejects that ambiguity. The owner unblocks a ticket by moving it back to **To Do**. That starts
a fresh count and clears the stored round history after the rescue has had access to the Blocked comment.
