# AI Flow v4 control plane architecture

This is the target structure for v4 changes. It preserves the current service boundaries while control routes are
extracted from the large `control/api.py` module. New work follows the target patterns below; existing routes may
move incrementally, with behavior kept behind the same API contracts.

The target feature structure and the event and listener design are in [Control plane structure: features, events and
listeners](event-architecture.md).

## Layers and dependencies

| Layer | Location | Responsibility |
| --- | --- | --- |
| Routes | `aiflow/v4/control/` (target: `control/routes/<feature>.py`) | HTTP paths, request/response validation, authentication and mapping to a service call. Routes do not contain business workflows or SQL. |
| Services | `aiflow/v4/control/<feature>.py` | Control-plane use cases and policy orchestration. Services call repositories and shared core functions. |
| Data access | `aiflow/v4/control/repositories/` (target) | SQL and persistence mapping, one repository per aggregate. The current `control/db.py` is the database connection boundary; move query ownership here as routes and services are extracted. |
| Adapters | `aiflow/v4/adapters/` | Provider-specific process and result handling behind the shared `Adapter` interface. Keep Claude, Codex, Grok and OpenCode details here. |
| Core | `aiflow/v4/core/` | Shared, provider-independent contracts and utilities such as settings, fencing, environment handling, backups, text and service definitions. |
| Worker and node clients | `aiflow/v4/worker/`, `aiflow/v4/node/` | Workers claim and report work through the control API; the node agent joins, heartbeats and supervises workers through that API. |

`aiflow/v4/core/docsite.py` is a build-time documentation tool, not a runtime core service. It may inspect the
mounted control app while generating the API reference; no runtime core module may import control.

Dependencies point toward stable contracts: routes call services; services call repositories and core; workers and
nodes use core and adapters where appropriate and communicate with control over its HTTP API. Control must not import
`aiflow.v4.worker` or `aiflow.v4.node`. Runtime core modules must not import control. Core stays usable without a
control process.
Do not create reverse imports to avoid a cycle; move a shared contract into core instead. Adapters do not import
control, workers or nodes. Keep runtime state in a run, service object, database or request context, not mutable
module globals. Keep provider credentials and CLI behavior behind adapters. Keep tests close to the behavior they
protect; as v4 grows, test code should remain comparable in size and coverage to production logic.

## Where a feature belongs

Start with its owning use case. Put HTTP parsing and authorization at the route edge, business decisions in a
control service, and SQL in the aggregate repository. Put a function in core only when it is shared by more than one
layer and has no dependency on control-specific policy. Put provider behavior in an adapter. Put work orchestration
in the worker; put machine supervision and heartbeat collection in the node agent. Every end-user feature also has
a Command Center UI path and user-facing documentation. CLI entry points are for developers and CI.

Do not make a route responsible for eligibility policy, a service responsible for persistence mechanics, or a
repository responsible for a Jira or GitHub workflow. Keep boundaries explicit so the implementation can be moved
without changing behavior.

## Patterns and registration points

| Concern | Pattern | Registration point |
| --- | --- | --- |
| Work eligibility | A pipeline of small eligibility rules, using the specification pattern or chain of responsibility. Each rule answers one question and explains a rejection; compose them instead of growing one `eligible` function. | Register named rules in the eligibility pipeline in the control service. |
| Work roles | Strategy: one class per work role implements the shared worker-role interface. The dispatcher selects a strategy by its registered role name. | Register each strategy by name in the worker role registry; do not add another role branch to a single large worker function. |
| HTTP routes | One FastAPI `APIRouter` per feature. Route modules expose their router and the central route loader discovers and includes routers under `control/routes/`. During extraction, existing `install(app, ctx)` and `mount(app, ctx)` modules remain explicitly called from `create_app`; the architecture test ensures these current installers are not orphaned. | New routers are registered by discovery from the route package; current installer modules are registered in `control/api.py:create_app`. |
| Console screens | One feature-owned screen/view entry, with its API contract and permission checks. The current shared console keeps its view ids in `control/ui/console.html`; page paths are listed in `control/ui/CONSOLE_PAGE_PATHS`. | Register a new view id and navigation/permission metadata in the console, and add its path through the UI route registration point. |
| Heartbeat extensions | Provider pattern: each heartbeat field is supplied by a small provider that owns collection and validation, rather than another block in the node polling loop. | Register providers by stable field name in the node heartbeat provider registry; control validates and stores only declared fields. |
| Ticket lifecycle | Table-driven state machine: represent allowed transitions and guards as data, and use one transition function to apply and audit them. Do not distribute status changes among unrelated route handlers. | Register transitions in the ticket lifecycle table in the control service. |
| Persistence | Repository per aggregate, separating query shape from services and routes. Keep transaction boundaries explicit. | Add a repository for each aggregate: tickets, runs, accounts, nodes and incidents. Services receive the repository they use. |
| AI providers | Existing adapter interface and implementations. A worker asks the adapter factory for a provider; it does not parse provider event streams itself. | Add provider names and implementations through `aiflow/v4/adapters/` and `get_adapter`; keep normalization in the adapter contract. |
| Documentation topics | A feature has its own `docs/v4/<topic>.md` page. New topic index entries normally live in one-line files under `deploy/v4/topics/` and are discovered by `aiflow.v4.core.topics`. | This architecture page is linked under Topics in `deploy/v4/README.md`, as required by its ticket. |

During the route extraction, central discovery applies to new feature routers while existing inline routes remain
in `create_app`. Extract an area when it changes; do not combine unrelated behavior changes with a structural move.

## Principles as checks

- **Single responsibility:** keep each function and class focused on one decision or operation. Review function size
  and cyclomatic complexity budgets on every change; split mixed responsibilities into services, rules or helpers.
- **DRY:** use the shared worker run setup and shared helpers for repeated behavior. In particular, do not copy
  prompt setup, environment preparation, run-result handling, auth checks or persistence queries between routes and
  workers.
- **KISS:** no function exceeds 80 lines or cyclomatic complexity 12. Treat either budget breach as a request to
  split the function before adding more behavior.
- **YAGNI:** do not add code without a caller or a test tied to a requirement. Remove dead generations and obsolete
  paths instead of extending them; the unused pre-v4 generation must not regain shipped responsibilities.

For each change, check the dependency direction, registration points, responsibility and size/complexity budgets,
shared run setup, and whether every new abstraction has a caller or requirement test. Review test coverage alongside
the production change so tests remain a comparable part of the v4 codebase.
