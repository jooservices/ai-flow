# State snapshot structure

`control.state.snapshot(ctx, *, include_signin=False)` assembles the response used by the console. Keep its public
signature, output key order, values and database call order stable; `tests/v4/test_state_snapshot_parity.py` records
the current response for representative contexts.

The section builders run in this order:

1. `_node_section` gathers active-run context and builds nodes.
2. `_account_section` loads account data and builds account views.
3. `_active_runs_section` and `_recent_runs_section` build the run sections.
4. `_ticket_section` attaches run markers and status counts to tickets.
5. `_operational_section` loads totals, incidents, events and lifecycle data, then calls `_project_section`.
6. `snapshot` assembles the sections and calls the dashboard summaries.

Add a field to the builder that owns its response section. Keep small transformations in named helpers beside that
builder. Add literal parity cases for changed branches and a focused test for each helper that can be tested on its
own; preserve the existing budgets of 80 lines and McCabe 12.
