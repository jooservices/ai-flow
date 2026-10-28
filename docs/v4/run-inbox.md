# Run inbox: guidance for an active worker

**Console screens:** `work`, `delivery`

The run inbox lets an authorized Command Center user add context, an idea, or a request after a worker has claimed a
ticket. It is available in the **Work board** (`/work`) and **Delivery** (`/delivery`) screens, scoped to that run,
ordered, persisted in PostgreSQL, and delivered after the current provider CLI
invocation completes, before the worker reports the run result. It does not interrupt a shell command or terminate
the provider process to inject text.

## User flow

1. In Work Board, choose a live run in **Guide a live run**.
2. Choose Context, Request, or Idea, enter up to 8,000 characters, and send.
3. The message appears in that run's inbox as waiting for a checkpoint.
4. When the current provider process finishes, the worker polls the inbox, starts a continuation against the same
   run and worktree with the original task context, previous turn output, and new messages in order. It acknowledges
   receipt at the checkpoint and inclusion after a provider process starts with the messages in its prompt.
5. The inbox changes through **Received by worker** to **Included in provider prompt**. The continuation's usage is
   included in the run result.

The worker uses this path for development, verification, triage, intake, impact, and master model turns. Message
delivery is bounded to eight continuation turns per run and 20,000 characters per checkpoint batch. If a message
arrives after the last safe checkpoint, or exceeds the continuation budget, the run closes it as **undelivered** and
the Command Center states why it was not included. The message remains visible in history. Users can then send
it to the next active run. Repeated submission with the same client ID is idempotent.

## API contract

| Method and path | Caller | Purpose |
| --- | --- | --- |
| `GET /v1/runs/{run_id}/inbox` | Console session with `review.actions` | Read the latest 100 messages, oldest first. |
| `POST /v1/runs/{run_id}/inbox` | Console session with `review.actions` | Add `{client_id?, kind?, body}`. Kinds: `context`, `request`, `idea`; body: 1–8,000 characters. |
| `POST /v1/runs/{run_id}/heartbeat` | Owning node bearer token | Renew the fenced lease, acknowledge `inbox_received` and `inbox_included` IDs, and receive queued/received messages. |
| `POST /v1/runs/{run_id}/result` | Owning node bearer token | Finish a run and atomically mark guidance that missed its last checkpoint as undelivered. |
| `POST /v1/runs/{run_id}/release` | Owning node bearer token | Release a run and mark guidance that missed its last checkpoint as undelivered. |
| `POST /v1/master/actions` | Owning node bearer token | A master restart closes the target run and marks its pending guidance undelivered. |

Console writes require a writable license and must match the user's project scope. The API accepts messages only while
the run row is `running` and its lease/fence remains valid. Run closure and enqueue serialize on the run row: messages
that are still queued or received at closure are changed to `undelivered`, with a reason, and a
`run.inbox.undelivered` event records their IDs.

## Storage and delivery semantics

`run_inbox_messages` is a per-run message history with a unique `(run_id, client_id)`, monotonically increasing database
ID, author, message kind, immutable body, lifecycle status, and timestamps. `queued` means the worker has not
acknowledged receipt; `received` means the worker fetched it at a checkpoint; `included` means a provider process
started with the message in its prompt; `undelivered` means the run closed before that could happen. Heartbeats only
collect and renew; they never stop an AI process. The worker's
heartbeat thread buffers messages safely, and the orchestration layer consumes them only after `Adapter.run` returns.

The continuation uses the existing worktree, appends to the same run log, retains original task instructions and the
previous final answer, then adds human messages in ID order. Provider-specific streaming and CLI behavior stays in
the adapter layer; the inbox does not rely on stdin being writable.
