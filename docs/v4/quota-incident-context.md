# Quota incidents in claim context

The Knowledge screen and API keep stored incident study cases available to authorized users. Database knowledge packs for `dev`, `verify`, and `qa` claims omit study cases linked to `account_quota`, `quota_high`, or `quota_prediction` incidents. Incident references that are malformed or do not resolve are omitted too; resolved incidents of other kinds remain eligible under the existing scope, approval, and status rules.

`claim_extras.context_pack` selects the claim steps. It enables the default-off `exclude_quota_incidents` option on `knowledge.context_pack`, which applies the same SQL predicate to the 500-row candidate selection and the study-case search before ranking and result limits. `search_items` and context-pack callers keep their existing behavior when the option is omitted.

To apply this selection to another claim step, add its step name to the target set in `features/work/claim_extras.py`. No new screen or route is needed; authorized users continue to review stored records in Knowledge.

## Routes

- `POST /v1/work/claim` Claims work for an authenticated node and returns the run payload with its selected context.
- `GET /v1/knowledge/items` Returns the authorized Knowledge dashboard and item search for the requested project and query.
