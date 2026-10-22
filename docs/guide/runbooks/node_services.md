# Runbook: node_services

**Assumes:** you are an Owner or Super Administrator and can reach the node; the project declares test services (PostgreSQL with pgvector, Redis). Raised in `control/loops.py`.

## Symptom

Warning incident "node NAME has unhealthy test services for PROJECT". The node takes no work for that project.

## Cause

A test service that the project requires is missing, stopped or unhealthy on the node (for example Docker is stopped or a disk is full).

## Fix

1. Read the detail: each service is listed with its reason.
2. The node agent recreates missing or stopped services by itself; wait for a few heartbeats.
3. If it persists, check on the node that Docker runs and has free disk space, then restart the node agent.
4. The incident closes when a heartbeat reports the services healthy. See `docs/v4/test-services.md`.
