# Alert when the flow stands still

On 21 October a configuration gap (no pins in `versions.lock` for a project stack) made the control plane offer
no work for about an hour, with 13 tickets waiting. Nothing said so. Two watchdog checks now do:

- **`flow_idle` (critical, subject `global`).** Tickets are ready (To Do with `aiflow-ready` and every blocker
  Done, or In Verify; not on hold, not in retry backoff, not leased) and have been for longer than
  `idle_alert_minutes`, yet no run other than the master cycle is running or started in that time. The incident
  names the cause: no node online, no online node serves the project, the tools or pins that gate a project on
  every node serving it (for example `versions.lock has no pin for php, composer, wp`), or no AI account ready.
  It closes when a run starts or nothing is waiting.
- **`config_pin` (critical, subject `<PROJECT>:<tool>`).** A project stack needs a pin in `versions.lock` for each
  of its tools. A missing pin is reported at once, naming `tools.<tool>`, and closes when the pin exists.

The threshold is the policy key `idle_alert_minutes` in `aiflow-control/policy/default.yaml` (default 15). Both
incidents show on `/cc` and in the SA view like every other incident and alert the owner through the
notification channels.
