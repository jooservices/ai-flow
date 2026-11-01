# Work claim stages

`POST /v1/work/claim` stays an atomic worker operation with the same refusal responses, rollback points, side-effect order
and success payload. Its short orchestrator is in `aiflow/v4/control/features/work/claim.py`; shared state is carried by
`Claim` in `claim_steps.py`.

The claim steps run in this order:

1. `refuse_early` checks a live lease, machine load, work licence and release compatibility.
2. `select_ticket` finds the requested candidate, checks project entitlement and authorizes development.
3. `acquire_probation`, `acquire_lease` and `reserve_hot_files` make the ticket assignment exclusive and undo earlier
   acquisitions when a later acquisition refuses the claim.
4. `repository_settings` captures the model, repository and run provenance. `dev_assignment` adds the capacity estimate
   and assignment reason for development runs.
5. `register_run` applies the load and session policy; `record_claim` records the assignment and publishes `RunStarted`.
6. `claim_extras.py` builds the response fields for each work step, adds the knowledge context pack and builds the final
   project payload.

A new claim step must be a named function with a docstring and a focused test in `tests/v4/test_claim_steps.py`. Keep
refusals and rollback behavior in the step that owns the corresponding gate or acquisition.
