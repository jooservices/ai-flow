# AI Flow v4 Command Center: node registration and last seen

A node is registered by `/v1/nodes/join`; the IP it reports at join is stored with the node and is not
overwritten by a later join that leaves the IP empty. The startup sync of the control repo does not replace
that reported IP either, so an API restart keeps the address where the node was last seen. Each heartbeat
updates `last_heartbeat`.

`GET /v1/state` returns every registered node, including nodes that stopped reporting. The node fields
operators need are:

- `ip`: the address reported at join
- `joined_at`: when the node first registered
- `last_heartbeat`: ISO 8601 time of the last heartbeat, or `null` if it never sent one
- `heartbeat_age_s`: seconds since that heartbeat
- `metrics.hostname`: the hostname carried by the heartbeat

A node is shown as offline when its heartbeat is older than 75 seconds, without removing it from the list.
The node drawer shows the IP, joined time, hostname and last heartbeat. An offline node shows
`last seen <time> (<age>)`, so operators can tell when and where it was last alive. The watchdog opens a
`node_down` incident while the node is offline and closes it on the next heartbeat.
