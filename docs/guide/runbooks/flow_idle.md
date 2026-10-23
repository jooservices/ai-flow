# Runbook: flow_idle

**Assumes:** you are an Owner or Super Administrator and AI Flow v4 is running. Raised in `control/incidents.py`.

## Symptom

Critical incident "flow is idle: N tickets are ready but no run started for M minutes". Tickets in To Do with the
label `aiflow-ready` (or In Verify) wait and nothing runs. M is the policy value `idle_alert_minutes` (default 15).

## Cause

The incident detail ends with "Cause:" and names one or more of:

- no node is online;
- no online node serves the project;
- every node that serves the project is gated by missing tools, a missing pin in `versions.lock`, or unhealthy test services;
- no AI account is ready (all in cooldown, signed out or failing).

## Fix

1. Read the "Cause" in the incident detail.
2. No node online or serving: open [Command Center](../screens/cc.md), check the node; see [node_down](node_down.md).
3. Missing pin: see [config_pin](config_pin.md). Missing tools: [node_tools](node_tools.md). Test services: [node_services](node_services.md).
4. No AI account ready: see [account_quota](account_quota.md) and [role_coverage](role_coverage.md).
5. "no cause found": check the master cycle in Command Center.

The incident closes by itself when work starts again.
