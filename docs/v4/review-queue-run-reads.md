# Review queue run reads

The review queue repository owns three batched run-history projections used to build the existing human-review rows:
latest run with a nonempty pull-request URL, latest run of any step, and latest verification run. Each operation accepts
the selected ticket keys and returns frozen row models; the legacy queue helper maps those models to its existing keyed
dictionaries.

To add a run-history read, define its frozen projection in `features/review_queue/models.py`, add a named parameterized
query in `features/review_queue/repository.py`, and adapt it at the legacy queue boundary. Preserve the existing batch
scope, selected fields, ordering, and call sequence.

## Routes

- `GET /v1/reviews` Lists the human-review queue with its existing pull-request and verification context, scoped by the caller's projects.
