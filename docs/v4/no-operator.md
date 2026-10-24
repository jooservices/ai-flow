# Flow doctor and operator interventions

AI Flow v4 does not depend on an operator session. The control-loops process holds a fenced `flow_doctor` lease and
runs a doctor pass every minute. Findings and idempotent recovery actions are stored in PostgreSQL and appear on
Dashboard under **Flow health**. The same screen shows the checked causes, open findings, recent repairs, per-role
account coverage, and the last 24 hours and 7 days of **Operator interventions**.

## Diagnosis and recovery

When ready work has no active run, the doctor records the cause it can observe: parked tickets and tool waits,
retry backoff, impossible dependencies, licence state, release version skew, quota, project pause, role health,
account state, machine state, or missing account coverage. Findings retain the observed detail and a next step. If
the normal idle diagnosis has no specific observation, it lists every checked gate rather than reporting only “no
cause found”. The existing idle incident carries the same cause list.

Safe repairs use the existing routing and fencing rules:

- Unsupported tool names are removed from tool waits; waits resolve when a compatible online machine already reports
  the requirement, while machines that still lack it remain gated. Catalogue tools are requested through the node
  agent's normal heartbeat install plan.
- Links to blockers in Backlog, Parked, Declined, or equivalent terminal states are removed in Jira and the local
  mirror. The ticket receives a comment explaining why it is available again.
- Expired run backoff returns a still-ready ticket to normal routing. A stale run on a live node is fenced, its
  ticket is requeued, and a server-authored worker restart operation is consumed by that node's agent.
- A failed machine or account route is released to any other ready account and machine that normally qualify for
  the ticket, and the failed route is excluded for that ticket for two hours (an account failure excludes the
  account, a machine, tool or network failure excludes the machine; table `flow_doctor_route_exclusions`). The claim
  path honours the exclusion, so the failed route cannot pick the ticket up again. "Ready" uses the routing rules:
  an online machine with a fresh heartbeat, a signed-in account assignment, and free account and machine capacity.
  Managed account sign-in is restarted through its normal node-agent sign-in link; after provider authorization the
  first successful credential probe completes recovery and clears the pending sign-in without a console confirmation.
- A healthy but switched-off role can receive another probation slot immediately. Each required role reports the
  number of ready accounts and provider diversity. Where safe recovery cannot create a second provider, Dashboard
  states the reason and the next practical step.

The doctor does not bypass licence entitlements, project pause settings, provider quota, or release compatibility.
It records those as gates and leaves protected business choices to the owner. Technical questions continue through
the escalation classifier and are answered from the repository, machine catalogue, policy, or existing decisions.
Only a new business decision or protected irreversible/production action can reach a person.

## Intervention audit

Accepted writes made by an authenticated console user are recorded as operator interventions as well as control API
write-audit entries. This includes account, machine, project, integration, policy, backup, and other console
configuration changes; the next doctor pass groups them into a Bug session. Worker and control-loop writes remain
product actions. Jira sync compares changelog authors with AI Flow's Jira identity
and records non-flow edits by field name only. Migration `018_flow_doctor.sql` installs statement-level audit triggers,
and every migration pass attaches the trigger to any table that lacks it, so tables added later are audited too. The
triggers cover INSERT, UPDATE, DELETE and TRUNCATE; event triggers cover schema changes (CREATE, ALTER, DROP,
including a dropped audit trigger). Product connections are told apart by the database role they log in with
(`audit_product_roles`, filled by `Database.migrate` with the control plane's own role), never by the client-chosen
`application_name`: people must use their own database role. The migration fails and is not recorded as applied
unless it can install both DDL event triggers; install migrations with a role authorized to create PostgreSQL event
triggers. Direct changes record the database user, table and operation, plus a reason describing the observed
operation, but never the SQL text or row values. Control and node installs enable Linux `auditd`
rules for interactive commands, `/etc/aiflow`, `/etc/systemd/system`, and (on the control host) the control repo.
The node agent forwards the executable basename and changed filename with a reason describing the observed command
or file change; it never forwards shell arguments or file contents. The agent collapses one batch into one intervention per actor and source. Audit records are
de-duplicated and the doctor creates one Bug per actor and source session (a gap of 30 minutes starts a new session)
in the configured maintenance Jira project with the `flow-bug` and `operator-intervention` labels, so an SSH session
or a deploy never floods the project. Sessions are stored (`operator_intervention_sessions`): an action that the
doctor sees in a later pass joins its session and is commented on the session's Bug instead of opening a new one.
Each Bug is moved from Created to Intake (retried every pass until it works), the state intake workers claim from.

Each row records who, what, where and why, using the observed operation when a console request does not carry a
separate rationale. The Dashboard counts all captured interventions over rolling 24-hour and
7-day windows and links the resulting Bug key. Reasons are attached to product incidents so recurring manual work
can be eliminated.

## Reboot and clean install

PostgreSQL and Redis are durable Docker services with restart policies; the control API and loops containers also
restart with Docker. The node installer enables the node agent at boot. The agent waits for network, Docker and
auditd, then reports systemd readiness after its first successful control heartbeat. Worker units require that
readiness notification, and candidate routing independently requires a heartbeat younger than the online threshold,
so a worker cannot claim work from stale pre-reboot machine state. On a node reboot, the order is host
services, node agent/machine registration, account reconciliation, then worker runs. On a control-host reboot,
database services restart before the control containers recover their database connections and run migrations.
Deploy installers keep enrolled control-host node agents current instead of disabling them during a control-only
upgrade.

The normal control installer enables audit rules and the regular node, develop, and offline installers install the
same audit feature. No lab-only control sync command, session patch, node-agent workaround, or database guard is
required for a fresh install.

## Screens and API routes

**Console screens:** Command Center (`cc`), Delivery (`delivery`), Human review (`review`), Machines and AI (`machines`), Users and roles (`users`), and Knowledge (`knowledge`).

The Dashboard (`cc`) shows the Flow doctor panel (ready accounts per role, open findings with the next step, recent
doctor actions) and the Operator interventions tile (last 24 hours and 7 days).

Routes used by this feature, all on the control API:

- `GET /v1/state` carries `flow_doctor` and `operator_interventions` for the Dashboard; `GET /v1/deploy/state` reports
  the same deploy facts to the node agent.
- `POST /v1/nodes/{name}/operator-interventions` records a manual action (who, what, where, why) and opens its bug.
- `POST /v1/nodes/{name}/release-failures` reports a failed release step from a node.
- `POST /v1/nodes/{name}/heartbeat` refreshes machine readiness and starts account recovery; the first successful
  heartbeat is the systemd readiness point for worker units.
- `POST /v1/nodes/{name}/operations` and `POST /v1/nodes/join` let a node install a catalogue tool and join at start-up;
  `DELETE /v1/nodes/{name}` removes a machine.
- `GET /v1/work/candidates` and `POST /v1/work/claim` require a fresh node heartbeat before routing or claiming work.
- Account repair and sign-in again: `POST /v1/machines/{name}/accounts`, `PATCH /v1/accounts/{account_id}`,
  `DELETE /v1/accounts/{account_id}`, `POST /v1/accounts/{account_id}/sign-in` and
  `POST /v1/accounts/{account_id}/sign-in/complete`. `POST /v1/accounts/{account_id}/probe` completes a managed
  sign-in automatically after the machine reports a successful credential check.
- Project environment configuration and its validation are available at `GET /v1/projects/{key}/environment`,
  `PUT /v1/projects/{key}/environment`, `GET /v1/environment/missing`, `GET /v1/environment/scan`,
  `POST /v1/environment/scan` and `POST /v1/environment/removals`. Mutations made with a console session are
  recorded in the intervention ledger like other console configuration changes.
- Console users (behaviour unchanged, listed because the route module changed): `POST /v1/console/users`,
  `PUT /v1/console/users/{email}` and `PATCH /v1/console/users/{email}/disabled`.

## Acceptance drill

The 72-hour drill is a post-merge fleet run, as authorized for JAIF-260. Its environment comes from the project
environment configuration (JAIF-266); this document names no machines. Keep a steady real-ticket stream,
and inject one fault every six hours, rotating through worker kill, missing/broken catalogue tool, expired account,
impossible dependency, and machine restart. Capture the intervention Dashboard counts at start and end, every idle
interval, each fault and recovery action, and the resulting ticket/run IDs. Attach the timestamped report to the pull
request. A completed report is intentionally not claimed by the implementation run; the fleet drill runs after merge.
