# Runbook: service_undeclared

**Assumes:** you are an Owner or Super Administrator with access to the Projects screen. Raised in `control/tooling.py`.

## Symptom

Warning incident "PROJECT tickets wait for undeclared test services". Tickets of the project are parked on `postgres` or `redis`.

## Cause

The project does not declare the test service, so no machine prepares or reports it. The flow doctor normally declares it itself; this alert means it could not write the project file (for example GitHub is not connected).

## Fix

1. Open the Projects screen and add the named service under Test services of the project, then Save.
2. Machines of the project prepare and report it within two minutes and the parked tickets are offered again.
3. The incident closes when the project declares the service. See `docs/v4/test-services.md`.
