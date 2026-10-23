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
  the ticket. Managed account sign-in is restarted through its normal node-agent sign-in link.
- A healthy but switched-off role can receive another probation slot immediately. Each required role reports the
  number of ready accounts and provider diversity. Where safe recovery cannot create a second provider, Dashboard
  states the reason and the next practical step.

The doctor does not bypass licence entitlements, project pause settings, provider quota, or release compatibility.
It records those as gates and leaves protected business choices to the owner. Technical questions continue through
the escalation classifier and are answered from the repository, machine catalogue, policy, or existing decisions.
Only a new business decision or protected irreversible/production action can reach a person.

## Intervention audit

The control API records accepted human API writes. Jira sync compares changelog authors with AI Flow's Jira identity
and records non-flow edits by field name only. Migration `017_flow_doctor.sql` installs statement-level audit triggers
on existing registry tables; direct SQL changes outside an `aiflow4-*` application connection record the database
user, table and operation, but never the SQL text or row values. Control and node installs enable Linux `auditd`
rules for interactive commands, `/etc/aiflow`, `/etc/systemd/system`, and (on the control host) the control repo.
The node agent forwards only the executable basename and changed filename, actor and time; it never forwards shell
arguments or file contents. Audit records are de-duplicated and each pending intervention creates a Bug in the
configured maintenance Jira project with the `flow-bug` and `operator-intervention` labels.

Each row records who, what, where and why. For actions where the system cannot recover a human-supplied reason, the
record says that no reason was supplied. The Dashboard counts all captured interventions over rolling 24-hour and
7-day windows and links the resulting Bug key. Reasons are attached to product incidents so recurring manual work
can be eliminated.

## Reboot and clean install

PostgreSQL and Redis are durable Docker services with restart policies; the control API and loops containers also
restart with Docker. The node installer enables the node agent at boot. The agent waits for network, Docker and
auditd, then reconciles accounts and worker units from the control plane. Worker units require the node agent, so
account sessions do not start before the machine has joined and heartbeated. On a node reboot, the order is host
services, node agent/machine registration, account reconciliation, then worker runs. On a control-host reboot,
database services restart before the control containers recover their database connections and run migrations.
Deploy installers keep enrolled control-host node agents current instead of disabling them during a control-only
upgrade.

The normal control installer enables audit rules and the regular node, develop, and offline installers install the
same audit feature. No lab-only control sync command, session patch, node-agent workaround, or database guard is
required for a fresh install.

## Acceptance drill

The 72-hour drill is a post-merge fleet run, as authorized for JAIF-260. Run it on `aiflow4-cc`, `aiflow4-node1`,
`aiflow4-node2`, and `aiflow4-node3`; do not use shared `workspaces-*` containers. Keep a steady real-ticket stream,
and inject one fault every six hours, rotating through worker kill, missing/broken catalogue tool, expired account,
impossible dependency, and machine restart. Capture the intervention Dashboard counts at start and end, every idle
interval, each fault and recovery action, and the resulting ticket/run IDs. Attach the timestamped report to the pull
request. A completed report is intentionally not claimed by the implementation run; the fleet drill runs after merge.
