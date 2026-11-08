# AI review rounds
**Console screens:** `cc`

Verification records finding IDs, original wording, current status and first-seen round in each completed review run.
The next reviewer receives that history in `prompts/v4/verify.md`, checks earlier findings first, then reports fixed,
unresolved and new findings together. The developer receives active findings as a numbered list; `core/handover.py`
passes the per-ID answers from the rework summary back to verification.

`control/features/work/eligibility_candidates.py` keeps the first reviewer preferred while that account passes normal
ticket eligibility. After the second return, `worker/verify.py` sends the next would-be return to Intake with the
`needs-split` label. The `intake_refiner` plans a split under JAIF-335; `control/ticket_split_apply.py` applies the
validated plan, removes the temporary label and leaves the split parent out of future Intake claims.

The Dashboard reads merged-ticket history through `control/features/review_rounds/repository.py` and
`service.py`, exposed at `GET /v1/dashboard/review-rounds`. Its console panel shows the two-round target, rounds per
ticket, active findings per review and new findings from round two onward.

To add a review-history field, update `core/review_findings.py`, pass it through the verifier run details and cover
both the parser and claim context. To add a metric, update the review-round model, repository, service, Dashboard route
and console panel together; keep project scoping in the route.

## Routes

- `GET /v1/dashboard/review-rounds` Returns merged-ticket review-round and finding-count metrics within the caller's Dashboard project scope.
- `POST /v1/work/claim` Claims an eligible ticket step for a worker and returns its work payload, including prior finding history for verification claims.
- `POST /v1/runs/{run_id}/result` Records a worker's result and applies a validated split plan when an Intake result carries one.
