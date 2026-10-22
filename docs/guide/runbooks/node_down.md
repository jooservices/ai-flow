# Runbook: node_down

**Assumes:** you are an Owner or Super Administrator and can reach the node machine. Raised in `control/loops.py`.

## Symptom

Critical incident "node NAME is down": no heartbeat for more than the offline limit. The node shows offline in [Command Center](../screens/cc.md).

## Cause

The machine is off or unreachable, the node agent stopped, or the network between the node and control is down.
Runs on the node lose their lease after 90 seconds and their tickets go back to To Do, keeping their branch.

## Fix

1. Check the machine is on and reachable from the control machine.
2. On the node, check that the node agent service is running and restart it if not.
3. Check the network and the control URL the node uses.
4. When a heartbeat arrives the incident closes ("heartbeat is back"). If the node is gone for good, remove it.
