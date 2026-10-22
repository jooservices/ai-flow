# Grok adapter

The v4 Grok adapter consumes the CLI's newline-delimited `streaming-json` output. It appends response chunks from
`text.data`, sums per-response token usage, and takes aggregate usage and `total_cost_usd` from the final `end`
event. Thought and command-list events do not count as answer text.

A probe succeeds only when it receives non-empty answer text. An empty response without a CLI error is recorded
as `no answer` with kind `provider`; only an authentication error marks the account signed out. A successful probe restores the
account to the available idle state and clears its account health incident.
