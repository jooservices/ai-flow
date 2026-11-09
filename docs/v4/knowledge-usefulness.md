# Knowledge usefulness

**Console screens:** `cc`, Knowledge (`knowledge`)

The Knowledge screen compares which stored memory reached a run and which ids the AI named in its final result. Each
prompt labels an item with its stable knowledge item id. The AI reports `KNOWLEDGE_USED_IDS: [id, ...]` at the end of
ordinary responses; strict JSON responses include `knowledge_used_ids`. An empty list means that no delivered item
was reported as useful. Only ids present in that run's delivered pack are stored as used.

## Report measures

The report lists delivery and use counts per item and per kind. It compares completed verification runs that had an
item or kind in their context pack with those that did not. Rework rate is the share with outcome `rework`. First-pass
rate is the share whose verification verdict is `PASS`, whose configured quality checks passed (`qc_passed=true`),
and whose ticket had no earlier rework; both rates use completed verification runs as their denominator. A missing
denominator is shown as unavailable. Release validation runs do not call an AI and are excluded from delivery counts.
Active or stale items with at least ten deliveries and zero reported uses appear in the retirement review list; the
report never retires them automatically.

The weekly notification includes the week's counts and comparison rates by kind and the delivered items from that
period. Its retirement review list uses lifetime deliveries and uses so an item is not flagged merely because it was
unused during one week. The Knowledge screen remains the complete searchable per-item report.

## Data and migration design

Reads use three indexed queries: item delivery/use counts grouped by item, verification outcomes grouped into runs
with and without each item or kind, and the total verification cohort for the selected project scope. Writes happen
atomically when a context pack is built and when a fenced result is accepted: the existing `knowledge_deliveries` rows are the
delivered ids, a new `knowledge_uses` row records each reported id, and `knowledge_context_packs.token_count` stores
the estimated size. The existing `runs.outcome` stores the run outcome and `runs.details.rework_count` stores the
ticket's cumulative verification rework count at result time.

`knowledge_uses` is keyed by `(run_id, item_id)` and has a composite foreign key to the matching delivery, so a run
cannot claim use of an item it did not receive. `knowledge_deliveries` keeps its existing `(run_id, item_id)` key;
the migration adds `(item_id, run_id)` for item report aggregation, a project/time index for context packs, a project,
step and start-time index for verification cohorts, and the usage table's reverse lookup index. Token counts are
nullable so old packs remain explicitly unknown; new counts estimate four characters per token from the delivered
item ids, titles and content without adding a tokenizer dependency.

Rows grow by at most one delivery and one usage per pack item, plus one pack row per run; at roughly ten items per
run, one hundred thousand runs add about one million delivery rows and no more than one million usage rows. These
records are retained with the existing learning history because it must not be lost. There is no backfill for old
usage or token counts: the old schema never recorded those facts. The migration is additive and forward-only, so an
older worker can omit `knowledge_used_ids` and a newer worker can use the new field with the updated control plane.

## Routes

- `GET /v1/knowledge/items` returns the selected project's scoped knowledge items, delivery and use counts, comparison rates, and retirement review candidates.
- `POST /v1/work/claim` claims worker work and delivers its scoped knowledge context pack.
- `POST /v1/version-releases/claim` claims a version release validation run for a worker.
- `POST /v1/runs/{run_id}/result` records the fenced run outcome, provider usage, and reported knowledge ids.
- `POST /v1/runs/{run_id}/heartbeat` renews a run lease and receives or acknowledges worker inbox messages.
- `POST /v1/runs/{run_id}/release` releases the worker's run lease and closes its inbox.
- `POST /v1/master/actions` validates and performs a fenced master follow-up action.

## Adding report measures

Keep report SQL in `control/repositories/knowledge_usefulness.py`; add a small test with the repository query and a
screen assertion for each new field. Keep run-result parsing in the worker prompt/result path and preserve the
delivered-id foreign-key check when recording a new usage measure.
