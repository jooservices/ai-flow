# Runbook: service_undeclared

**Assumes:** you have access to the Projects screen.

## Symptom

The control plane raises one `service_undeclared` warning for a project when tickets are parked on `postgres` or
`redis` and the project configuration could not be updated automatically.

## Cause

The project does not declare a test service required by its parked tickets. The flow doctor normally adds it to the
project's `services` setting. This alert means the doctor could not write the project file, so its machines cannot
prepare that service yet.

## Fix

Open the Projects screen and add the named service under **Test services**, then save. The project's machines receive
the setting in their next heartbeat and prepare the service. The alert closes when the project declares the service.
See [Test services of a project](../test-services.md).
