# Fleet upgrade

How the control plane and the nodes of AI Flow v4 move to a new release safely: the control plane first, after a
pre-upgrade check, then the nodes in a rolling upgrade (one canary, then waves), with an automatic rollback when a
node fails its health check. The code is `aiflow/v4/control/rollout.py`; releases themselves are described in
[Versioned releases](release.md) and the pre-promotion node canary in [Canary auto-deploy](canary-autodeploy.md).

## Order of an upgrade

1. `aiflow4-autodeploy` (or an operator) picks a new `vX.Y.Z` and may observe it on one node first through
   `CANARY.json` ([Canary auto-deploy](canary-autodeploy.md)).
2. `aiflow4-deploy vX.Y.Z` verifies the signed assets, runs the **pre-upgrade check** and only then replaces the
   control containers and writes `CURRENT`.
3. A changed `CURRENT` starts a **fleet rollout**: the nodes follow it canary first, then in waves.

## Rolling upgrade

The control plane keeps one `fleet_rollouts` row per target release (migration `007_fleet_rollouts.sql`) and one
`fleet_rollout_nodes` row per node that was asked to install it. The heartbeat's `release` field is chosen per
node:

| Rollout state | Rollout members | Other nodes |
|---|---|---|
| `canary`, `rolling` | the target | the previous fleet release |
| `completed` | the target | the target |
| `halted` | the release they ran before the rollout | the previous fleet release |

- **Canary.** The `control-loops` process checks the rollout every 15 seconds. When a rollout starts, it picks
  exactly one canary: `AIFLOW4_ROLLOUT_CANARY_NODE` (default: `AIFLOW4_CANARY_NODE`) when that node is eligible,
  otherwise a node that already reports the target (such as the auto-deploy canary), otherwise the first node by
  name. An eligible node is online, has sent a heartbeat in the last 75 seconds and runs a signed-release agent
  (`release_protocol` 1). Every other node keeps receiving the previous release.
- **Waves.** When the canary passes its health check, the next `AIFLOW4_ROLLOUT_BATCH_SIZE` eligible nodes
  (default 2, sorted by name) receive the target. The next wave starts only when every node of the wave
  before it has passed. The rollout is `completed` when no eligible node is left. A node that comes online later
  then receives the target on its heartbeat.
- **Already upgraded nodes.** A node that already reports the target during a rollout joins the current wave and
  is health checked with it, instead of being sent back. Examples are the control host's own agent, which
  `aiflow4-deploy` installs, and the promoted auto-deploy canary.
- **Health check.** A member passes when it reports `release_version` equal to the target and keeps sending
  heartbeats on it for `AIFLOW4_ROLLOUT_SOAK_S` seconds (default 120). A member fails when:
  - it does not report the target within `AIFLOW4_ROLLOUT_INSTALL_TIMEOUT_S` (default 1800);
  - it loses its heartbeat (no heartbeat for 75 seconds, or the watchdog marked it offline);
  - it reports another release after it had installed the target (the installer's own health check switched
    `/srv/aiflow/current` back; the installer now also reports this to the control plane);
  - it rejects the release (`POST /v1/nodes/{name}/release-failures`: a bad checksum or signature, or an
    installer error).

`GET /v1/state` returns the latest rollout under `rollout`, with the wave, the status of each member and the
reason of a halt. The events `fleet.rollout.started`, `.assigned`, `.healthy`, `.completed`, `.halted`,
`fleet.rollback.node` and `fleet.rollback.finished` appear in the event stream.

## Halt and rollback

One failed health check halts the rollout at once, and no node receives the target after that. Every member is
told to return to the release it ran before the rollout. The node agent reinstalls that release through the
normal path: it fetches the manifest, compares the SHA-256 digest, runs `cosign verify-blob`, and then
`aiflow4-node-install` checks the checksum and signature again before it switches `/srv/aiflow/current`. Nothing
skips verification. The control plane keeps the last ten signed releases in `/srv/aiflow/control/releases/` and
each node keeps its last five, so the previous release is still available.

The control plane opens a critical `fleet_rollout` incident (subject: the target). Its title names the failed
node, and its detail names the release, the previous release, the wave, the reason and the rollback plan. When
every member has reported its previous release, or has not done so within the install timeout, the control
plane records the result:

- `rollback of vX.Y.Z succeeded`: a warning `fleet_rollback` incident. The `fleet_rollout` incident detail is
  updated with "Rollback result: succeeded" and the state of each node.
- `rollback of vX.Y.Z FAILED`: a critical `fleet_rollback` incident that lists the nodes still on the target
  (`rollback_failed`). If such a node later reports its previous release, the rollback is marked succeeded and
  the incident closes.

A halted release stays off the nodes until `CURRENT` names another release. That can be a newer fix, or the
previous release again if the control plane is rolled back. In that case the rollout is complete immediately and
every node keeps or returns to that release.

### Running tickets

Installing or rolling back a release restarts the node's workers and agent. A run cut off this way stops
renewing its lease. The watchdog uses the existing lease-lost path: after the 90-second lease, the run becomes
`lost`, the account becomes idle, and the ticket moves from In Progress to To Do with its branch. The next dev
run continues that branch after the usual retry backoff. Runs on nodes that were not restarted keep their lease
and stay In Progress. After an incident, every ticket that was In Progress is either still In Progress with a
running run that holds the lease, or back in To Do. No ticket is lost. The orphan check, which moves an In
Progress ticket without a run for 10 minutes back to To Do, still covers a worker that died between two writes.

## Version skew policy

The control plane compares each node's reported release with its own release (`AIFLOW4_VERSION`, set from the
release tag) by `X.Y`:

- the same major, and at most `AIFLOW4_VERSION_MAX_MINOR_LAG` minors behind (default 1): compatible;
- more minors behind, newer than the control plane, or another major: **incompatible**.

Patch versions are not compared. If the control plane's version is not `vX.Y.Z`, as in development builds, the
policy is off. If a node reports something other than `vX.Y.Z` (an old agent or a develop commit), it is shown as
`unknown` and is not blocked. The auto-deploy canary node may run its `CANARY.json` candidate ahead of the
control plane.

An incompatible node gets no new work: `/v1/work/claim` answers 409 `version skew: ...` and
`/v1/work/candidates` returns no ticket. A run that is already in progress finishes normally. The watchdog opens
a `version_skew` warning incident for the node and closes it when the node runs a compatible release. Command
Center marks the node's release "incompatible, no new work", and `/v1/state` has `version_policy` on every node.
The node still receives releases, so the rollout brings it back inside the policy. Nodes inside the policy, for
example one minor behind while their wave has not started yet, keep working normally during a rollout.

## Migrations only go forward

Registry migrations are `aiflow/v4/control/migrations/NNN_*.sql` files, applied in name order and recorded in
`schema_migrations`. That table now also stores each file's SHA-256 checksum. A release adds new files and never
edits or deletes one that has been applied. There are no down migrations: a rollback, of the control plane or of
a node, never undoes a migration. The previous release starts on the migrated schema: `DB.migrate` skips the
versions it already knows and leaves recorded versions it does not ship. New migrations must therefore keep the
previous release working: add tables and columns, and make columns nullable or give them defaults.

## Pre-upgrade check

`aiflow4-deploy` runs `aiflow4 upgrade-check` from the new image before it starts a new control container or
changes `CURRENT`. It uses the control environment file and the `aiflow4-control` network. When the build tree of
the running release is still in `/srv/aiflow/build/<CURRENT>`, it is mounted as the baseline. The check never
changes the schema. It blocks the upgrade when:

- a migration recorded in `schema_migrations` is missing from the new release;
- an applied migration's content differs from its recorded checksum, or from the running release's file for
  versions that were applied before checksums were recorded;
- a new migration file is not named `NNN_*.sql`;
- an online or draining node would be outside the version skew policy against the new release (for example, a
  node two minors behind, or a node newer than the release).

Offline nodes are not checked. They receive the fleet release when they return.

When the check blocks the upgrade, the command exits with code 9, and `aiflow4-deploy` stops before it changes
anything: the running release, its containers and `CURRENT` stay as they are. The reasons are recorded in
`write_audit` (`action = 'upgrade_check'`) and in a critical `upgrade_blocked` incident. `aiflow4-autodeploy`
writes the release to `CANARY_REJECTED` so the timer does not try it again until a newer release is published,
and it sends a CRITICAL alert. A check that passes is recorded too and closes an open `upgrade_blocked` incident
for that release. For an emergency, set `AIFLOW4_SKIP_UPGRADE_CHECK=1` for one manual `aiflow4-deploy` run.

Run the check by hand:

```sh
docker run --rm --network aiflow4-control --env-file /etc/aiflow/secrets/control/aiflow4.env \
  ghcr.io/jooservices/ai-flow/control-api:v4.3.0 upgrade-check --target v4.3.0 --current "$(cat /srv/aiflow/control/releases/CURRENT)"
```

## Settings

Set these in the control environment file:

| Variable | Default | Meaning |
|---|---|---|
| `AIFLOW4_ROLLOUT_BATCH_SIZE` | `2` | nodes per wave after the canary |
| `AIFLOW4_ROLLOUT_SOAK_S` | `120` | how long a node must stay healthy on the target to pass |
| `AIFLOW4_ROLLOUT_INSTALL_TIMEOUT_S` | `1800` | deadline to report the target, and to report the previous release in a rollback |
| `AIFLOW4_ROLLOUT_CANARY_NODE` | `AIFLOW4_CANARY_NODE` | preferred canary node |
| `AIFLOW4_VERSION_MAX_MINOR_LAG` | `1` | how many minors a node may run behind the control plane |
