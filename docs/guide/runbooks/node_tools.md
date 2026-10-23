# Runbook: node_tools

**Assumes:** you are an Owner or Super Administrator and can reach the node. Raised in `control/loops.py`.

## Symptom

Warning incident "node NAME misses tools for PROJECT" or "node NAME misses AI tools". The node takes no work for that project (or no work for the missing AI provider).

## Cause

- For a project: the node's preflight found a tool missing or outside its pin in `versions.lock`.
- For AI tools: the AI command line program (Claude, Codex or Grok) of an account assigned to the node is not installed.

## Fix

1. Read the detail: it lists the tools.
2. Project tools: the node agent installs stack tools and retries after a release or toolchain update. Wait a few minutes; if it stays, check the pin ([config_pin](config_pin.md)) and the node agent log.
3. AI tools: install the program on the node, on the `PATH` of the node agent and the workers (see `docs/v4/node-join.md`).
4. The incident closes when a later heartbeat passes preflight.
