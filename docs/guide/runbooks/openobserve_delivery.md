# Runbook: openobserve_delivery

**Assumes:** you are an Owner or Super Administrator and can reach the control machine. Raised in `control/events.py`.

## Symptom

Warning incident "OpenObserve is rejecting events from SOURCE". The detail names the stream and the last response. Logs and dashboards fall behind; the flow itself is not affected.

## Cause

OpenObserve rejects event batches continuously: it is down, out of disk or memory, or its credentials changed.

## Fix

1. Open the OpenObserve link in [Command Center](../screens/cc.md) or check the container `aiflow4-openobserve` on the control machine.
2. Fix the cause named in the last response (disk, memory, credentials). See `docs/v4/openobserve-delivery.md`.
3. The incident closes when OpenObserve accepts a batch again.
