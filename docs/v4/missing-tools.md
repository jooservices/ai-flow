# A tool missing on a machine is a machine problem

A developer AI that cannot run a check because a tool or service is missing on its machine does not ask the ticket
owner. AI Flow records the missing requirement and the machine, parks the ticket for a machine that has it, and
only opens a maintenance ticket when no machine can ever supply the tool. The owner is asked only about
requirement conflicts, infrastructure changes or production releases.

## What the developer AI sees and reports

Before the run, the claim carries the machine's latest heartbeat (`machine_tools`) and the dev prompt lists the
tools and test services that are available and those that are not, so the AI does not try the missing ones.
When a check of the ticket needs a missing tool or service, the AI ends with:

```
RESULT: BLOCKED: MISSING-TOOL: php, composer - phpunit cannot run
```

The worker also recognises a blocked reason that plainly says a known tool is missing ("composer is not installed",
"php: command not found"). Such a run ends with outcome `tool_missing`, the ticket goes back to To Do with a comment
(no question to the owner), and the control plane stores one open row per ticket and requirement in `tool_waits`
(requirement, machine, project, run). A genuine requirement conflict still goes to Needs owner.

## Waiting and offering again

A ticket with an open wait is not offered to a machine that does not report the tool (a version other than
`missing`/`timeout`) or the healthy test service. As soon as a machine that serves the project reports it, the ticket
is offered to that machine again; claiming it closes the wait. Waits of Done tickets are closed by the watchdog.

**Command Center** shows the panel *Tickets waiting for a tool*: tickets per tool, projects, machines where it was
missing and the maintenance ticket once one exists (`tool_waits` in `/v1/state`).

## Maintenance ticket

The watchdog keeps a 24 hour clock per missing tool (`tooling_gaps`). The clock runs only while all of these hold:
no online machine reports the tool, and no project's declared stack (`STACK_TOOLS` in
`aiflow/v4/core/toolchains.py`, or its declared test services) includes it. If a machine could supply it, the clock
is reset. After 24 hours AI Flow opens **one** Task in the AI Flow Jira project (`AIFLOW4_MAINTENANCE_PROJECT`,
default `JAIF`) labelled `tooling`, naming the tool, the affected projects and the waiting tickets. It never opens a
second one for the same tool while the gap lasts.

## Project checks on a clean checkout

When a project is added, when a machine joins, and when a project's check commands change, the heartbeat answer
carries `desired.check_probes`. The node agent clones the base branch of each repository with a short-lived
read-only token (`/v1/nodes/<node>/check-probes/token`), runs each check command once as the unprivileged AI user,
and reports per command (`/v1/nodes/<node>/check-probes`):

- `ran`: the command started (a failing assertion still counts as ran);
- `cannot_run`: the executable is missing (not on `PATH`, exit 127, "command not found"), with the tool name.

The **Projects** screen has a *Checks* column per project: *not checked yet*, *all can run*, or the commands that
cannot run with the machine and the missing tool. A failed probe does not block tickets by itself; the tickets that
need the tool wait as described above.

## Configuration

| Setting | Default | Meaning |
| --- | --- | --- |
| `AIFLOW4_MAINTENANCE_PROJECT` | `JAIF` | Jira project that receives the `tooling` maintenance tickets |
