# Control plane structure: features, events and listeners

Target structure of `aiflow/v4/control` (epic JAIF-445). It extends [the layer rules](architecture.md); read those first.
The aim: a new reaction to something that happened is a new file, not an edit of a shared function.

## Features

A feature is a folder `control/features/<name>/` that owns one part of the business:

| File | Holds | May import |
| --- | --- | --- |
| `routes.py` | FastAPI `router`, request and response models, `can("permission")`; one call to a service per route | its service, `platform` |
| `service.py` | the use cases; no FastAPI, no `HTTPException`, no SQL; raises domain errors | its repository, its events, `platform`, `core` |
| `repository.py` | all SQL of the aggregate; returns frozen dataclasses from `models.py`, never raw dicts | `platform.db`, `models` |
| `models.py` | entities and value objects (frozen dataclasses) | nothing |
| `events.py` | the domain events the feature publishes | `platform.events` |
| `listeners.py` | `@on(OtherEvent)` reactions of this feature to events of any feature | its service, events of others |

A feature never imports another feature's `service`, `repository` or `routes`. It may import another feature's `events`
(to listen) and `models` (to read). Anything shared moves to `platform/` (`events`, `db`, `session`, `errors`, `settings`,
`route_deps`) or `core/`. `control/app.py` is the composition root (under 300 lines): it builds the services and mounts
the routers; it holds no route and no nested helper. The architecture test enforces these import rules.

## Events

A **domain event** is a fact in the past tense that already happened (`TicketMoved`, `DecisionAnswered`,
`RunFinished`). It is a frozen dataclass with an envelope: `id`, `type`, `version`, `occurred_at`, `aggregate` (kind and
id), `correlation_id`, `causation_id`; the payload carries ids and small values, never secrets or whole rows.

Five small components, one module each in `platform/events/`:

1. **catalog**: the event classes and their type names; one place lists every event and its owner.
2. **publisher**: `publish(tx, event)` writes the event to the outbox in the same transaction as the change.
3. **outbox**: rows in the existing `events` table (Postgres); no broker.
4. **registry**: `@on(Event, delivery=...)` registers a listener at import time.
5. **dispatcher**: delivers each event to its listeners per aggregate in order, with retry and backoff, a dead letter
   that opens an incident, and a replay command.

Two delivery modes: `in_transaction` (runs inside the publisher's transaction; for reactions that must commit with the
change) and `outbox` (runs after commit; the default). Every listener is idempotent: it can see the same event twice.
A listener failure never fails the command that published the event (outbox mode) and is visible as an incident.

What is an event: a state change another feature, the audit trail, a notification or the console may care about. What is
not: queries, commands, internal steps of one function. Start with about twelve events; add one only with a listener or a
named consumer. The telemetry types (`ctx.events.emit`) stay as log lines and are not domain events.

The **claim path stays one atomic synchronous transaction**; it publishes `RunStarted` at its end and nothing in it waits
for a listener.

The full list of events with their owner, payload and listeners is generated into [events.md](events.md); a test fails when
it is stale.

## Pilot: the tickets feature

`features/tickets/` has `models.Ticket` (frozen), `repository.TicketRepository` (get, update_status, remove_label,
update_description; typed results) and `events.TicketMoved`. It replaced `repositories/tickets.py`, whose six candidate queries
and two other functions had no caller. `escalations.py` and `ticket_lifecycle.py` use it (they serve `POST /v1/decisions/{decision_id}/answer`,
`POST /v1/escalations/raise`, `POST /v1/escalations/triage` and `POST /v1/work/claim`, whose behaviour is unchanged); the remaining direct `tickets`
SQL of `api.py` moves with the features that own it.

Two reactions that were inline in the Jira transition route and in the Jira sync are now outbox listeners of `TicketMoved`
(`features/tickets/listeners.py`): the completion report of an epic that reached Done, and the notice when a ticket enters
Human review. They run when the dispatcher delivers the event, about a second after the move, and the same listener now also
reacts to a move made by the flow itself (before, only the two inline places did). The epic status rules of `jira_sync`
(`_reconcile_epics`) and the merge flow follow in later slices.

Escalation pilot: when the owner answers (`answer_decision`) or answers in Jira (`owner_answered_on_ticket`), the code publishes
`DecisionAnswered` (`features/escalation/events.py`) and the outbox listener `escalation.resume_waiting_tickets` resumes the waiting
tickets. `publish_for(ctx, event, deliver_now=True)` attempts the delivery right after the commit, so an answer still resumes in the
same request; if the resume fails, the delivery stays pending and the dispatcher retries it with backoff (on top of the existing
`retry_resumptions`). The events are listed in [events.md](events.md).

Audit as a listener: an event that overrides `DomainEvent.audit()` (returning an `Audit` row) gets its `write_audit` row from
`platform/events/audit_ledger.py`, one in-transaction listener per such type, through the same `route_deps.audit`, so the
ledger stays append-only and the row commits with the change. `DecisionAnswered` is the first: `POST /v1/decisions/{decision_id}/answer`
no longer calls `audit()` itself, and the deadline default and the Jira answer path are now audited too. The backups routes
(`PUT /v1/backups/config`, `POST /v1/backups/key`, `POST /v1/backups/run`) moved too. The scale-proof result route and
the business change-request and stakeholder routes moved too. The knowledge routes
moved too (JAIF-478). The remaining
`audit()` calls move with their features (JAIF-478).

`Events` (telemetry log) is split by responsibility: `events.py` writes the registry row and hands the event over; shipping to
OpenObserve (queue, thread, capped backoff, delivery incident) is `openobserve_shipper.OpenObserveShipper`, reached as
`events.shipper`. Telemetry stays a log and is not a domain event, so there is no listener for it (JAIF-476).

Incidents: `open_incident` inserts the row and, when the incident is new, publishes `IncidentOpened`; the telemetry row
(in transaction), the study case and the alert are three listeners in `features/incidents/listeners.py` (they were one function).
The alert and the study case run through `deliver_now`, so callers see the same behaviour, and a failing alert is retried by the
dispatcher instead of failing the caller. The routes that open incidents behave as before: `GET /v1/ba-chat/candidate`,
`GET /v1/master/candidate`, `GET /v1/version-releases/candidates`, `GET /v1/work/candidates`, `POST /v1/master/plan-outcome`,
`POST /v1/nodes/{name}/release-failures`, `POST /v1/version-releases/claim` and `PUT /v1/role-health/settings`.

Work (JAIF-481): the 298-line `eligible` function that was nested in `create_app` is `features/work/eligible.py` (moved
unchanged); the app keeps a three-line wrapper and hands it the few helpers it needs (`work_deps`: pause and quota checks, the
previous dev run, the epic of a ticket, capacity routing, and the releases directory, read at call time so tests may patch
`RELEASES`). `PRIORITY`, the back-off queries and `utcnow` moved to `features/work/constants.py` and are re-imported by `api.py`.

Work claim (JAIF-482): the 216-line handler of `POST /v1/work/claim` is `features/work/claim.py`. It is moved verbatim and
stays one atomic claim (lease, hot files, run row); the app helpers it needs (`eligible`, `master_candidate`, `last_dev`,
`ticket_epic`, `capacity_history`, `related_hits`, `bot_identity`, `RELEASES`) arrive as `deps`. 

Run events (JAIF-483): a claim publishes `RunStarted` and a run result (worker, or a reviewer action in the console)
publishes `RunFinished`; the `run.claimed` and `run.finished` timeline rows are in-transaction listeners in
`features/work/listeners.py`. Token counts travel as `usage_in` and `usage_out` because the catalog refuses field names with "token".

Work repository (JAIF-480): the SQL of the claim path (previous runs, triage answer, rework findings, the account being
assigned, the node tools, the board for the impact step) is `WorkRepository` in `features/work/repository.py` (typed results from `features/work/models.py`), except the
prior developer run that `dev_history.delivered_or_last` in `aiflow/v4/control/dev_history.py` still queries; `claim.py`
no longer holds raw queries on runs, accounts or nodes.

Fleet routes (JAIF-485): the routes for nodes (join tokens, join, heartbeat, health, operations, probes), machines and managed accounts
(`POST /v1/nodes/join`, `POST /v1/nodes/{name}/heartbeat`, `POST /v1/machines/{name}/accounts` and the sign-in routes) are
`features/fleet/routes.py`, moved as they were; their request bodies are `features/fleet/bodies.py`. `install(app, ctx, deps)`
registers them at the end of `create_app` and `deps` carries the helpers that still live in the app (`audit`, `console_user`,
`node_auth`, `require_capacity` and the release helpers). A service/repository split follows when those routes change.
It serves `GET /v1/work/candidates` and `POST /v1/work/claim`, whose behaviour is unchanged; `api.py` is 320 lines shorter. The constants are also read by `GET /v1/github/git/{git_path:path}`, `GET /v1/master/autonomy`,
`GET /v1/master/context`, `GET /v1/master/report`, `POST /v1/console/view-as`, `POST /v1/console/view-as/stop`,
`POST /v1/github/git/{git_path:path}`, `POST /v1/github/token`, `POST /v1/nodes/{name}/check-probes/token`,
`POST /v1/nodes/{name}/operations` and `PUT /v1/master/autonomy/budget`, which are unchanged.

## Conventions

- Commands and queries are direct calls to a service; only reactions go through events.
- A service takes only the repositories it uses (no god context). Domain errors (`Invalid` 400, `Unauthorized` 401, `Forbidden` 403, `NotFound` 404,
  `Conflict` 409, `Unprocessable` 422, `Unavailable` 503) live in `platform/errors.py`, the one place that knows their status.
  They derive from `HTTPException`, so existing `except HTTPException` handlers and FastAPI's response keep working while
  services stop importing FastAPI. `run_inbox.py` is the first module moved (it serves `POST /v1/master/actions`,
  `POST /v1/runs/{run_id}/heartbeat`, `POST /v1/runs/{run_id}/release` and `POST /v1/runs/{run_id}/result`, whose responses are unchanged); a module moves when it is touched.
- Who may call a route is one parameter: `user: ConsoleUser | None = can("view.dashboard")` (`platform/auth.py`). With
  console sign-in off it returns the session user if present, else None; with sign-in on it requires a signed session whose
  role holds the permission (401, 403, audit of a refused write, licence check for a write) exactly as before. It moved
  `GET /v1/analytics/daily`, `GET /v1/analytics/conflicts` and `GET /v1/analytics/usage/daily` first; other routes move when
  touched. A separate transaction helper is not needed: `db.transaction()` nests with savepoints, and work that must happen
  after a commit is an outbox listener.
- Route models (pydantic) are separate from domain entities.
- Behaviour-preserving moves: the route set (`application-context-route-snapshot.json`) and the REST audit stay unchanged.
- A new feature starts from the scaffold: `uv run python -m aiflow.v4.core.scaffold_feature <name>` creates the folder,
  its registration in `control/routes/<name>.py` and a first test, and refuses to overwrite anything.

## Decisions

- **ADR-1 Outbox in Postgres.** Events are rows written in the change's transaction, so a crash loses none. No broker:
  one database already holds the state, and adding a queue adds an operations burden and a second source of truth.
- **ADR-2 No event sourcing.** State stays in tables; events describe what happened and are not the source of truth.
- **ADR-3 Admission rule.** An event needs a past-tense name, an owner, and a listener or named consumer. About twelve to
  start, to measure cost against the scale proof before widening.
- **ADR-4 Feature slices over layers.** Files that change together live together; a ticket touches one feature folder,
  so parallel tickets stop conflicting in `api.py`.
- **ADR-5 Keep the worker contract.** Worker and node talk to control only through the HTTP API; the refactor does not
  change that contract.

Code: `control/platform/events/` holds `catalog.py` (the `@event` decorator, `Envelope`, the admission checks: frozen
dataclass, owner, aggregate, no secret-looking field, unique type) and `registry.py` (`@on(Event, delivery=...)`). `publisher.py` writes an event to the outbox tables `domain_events` and
`domain_event_deliveries` (migration 033; a separate table from the telemetry `events`) in the caller's transaction, one pending delivery
per outbox listener, and runs in-transaction listeners before it returns. `dispatcher.py` delivers: one delivery per transaction with the listener's writes in a
savepoint (a failure leaves none of them), ordered per aggregate and listener, retried with doubling backoff, parked as `dead` after
`max_attempts` (the `on_dead` callback opens the incident, and later events of the aggregate are no longer held back), and
`replay()` puts dead deliveries back. Running it from the control loops comes with the walking skeleton (JAIF-467).

Walking skeleton (JAIF-468): `merge_flow.record_move` now writes the `ticket_moves` row and publishes `TicketMoved`
(`features/tickets/events.py`) in one transaction; the `ticket.moved` telemetry row is its in-transaction listener
(`features/tickets/listeners.py`), so the Command Center timeline is unchanged. `platform/events/wiring.py` gives each
application context its own registry (filled by the `register_listeners` function of each feature), `publish_for`,
`dispatcher_for` and the one-second `dispatch_loop` that `run_loops` starts for outbox listeners. The routes that move a ticket through it (`POST /v1/jira/{key}/transition`, `POST /v1/reviews/{key}/answer`,
`POST /v1/reviews/{key}/approve`, `POST /v1/reviews/{key}/ask-ai`, `POST /v1/reviews/{key}/reject`,
`POST /v1/reviews/{key}/rework`, `POST /v1/reviews/{key}/skip-gate`) behave as before and now publish the event. Measured on PostgreSQL:
a move costs 1.36 ms instead of 1.29 ms (300 moves each), and the 100/50/200 scale proof is unchanged (ticket pickup p95
13.9 to 14.9 s locally, no statement to the event tables because it never moves a ticket through the merge flow).
