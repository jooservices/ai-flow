# Validated missing requirements and parked tickets

Developer runs can park a ticket only for a requirement AI Flow recognizes in its tool and service catalogue, or
for a tool in the project's pinned stack. The control plane validates the report against the catalogue and the
node's latest heartbeat. Unknown marker values such as `control`, `shared`, or `wordpress` become ordinary failed
runs; their explanation stays in the run summary and no wait is created. A known requirement the node reports as
present is treated the same way, and any older wait for that ticket and requirement closes automatically.

When a real tool is missing, the run returns to To Do and the ticket remains visible in the flow. The next node
heartbeat asks its agent to install catalogue tools the node reports missing. Install requests are server-authored
and limited to fixed installer recipes; releases downloaded from GitHub are checksum-verified, cosign uses AI
Flow's shared pinned version and digest, and project stack tools keep their `versions.lock` pins. Existing project
stack and test-service reconciliation also runs from the heartbeat. A fresh heartbeat that reports the requirement
present resolves its wait, and tickets remain offered only to nodes whose reports satisfy their requirements.

The Command Center's **Parked tickets** panel lists one row per ticket with its requirement, parked time, machine,
and the next system action. The `flow_idle` critical incident includes parked ticket keys and their missing
requirements alongside other queue causes such as retry backoff, quota, licence limits, node version skew, and
project pause. A warning opens when a ticket requirement has remained unresolved for more than two hours, and closes
when the wait resolves or the ticket is completed.

Tool-wait rows remain fenced by the normal run lease: only the run that still owns its ticket can report a result.
The node agent receives only a server-built list of names and versions; it never executes a command from AI text.
