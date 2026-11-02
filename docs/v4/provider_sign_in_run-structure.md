# Provider sign-in run structure

Routes: `POST /v1/nodes/{name}/heartbeat` supplies sign-in requests that the node agent passes to this runner.

`run_provider_sign_in` keeps its public signature and invokes named phases in order: validate the request,
resolve the provider command, prepare the account environment, start the PTY process, monitor output, and report
the result. Monitoring calls the cancellation, read and output-relay helpers directly. Startup errors keep the
existing redacted status and log handling.

To add a phase, give it a module-level function with explicit inputs and call it directly from the adjacent phase.
Add its call trace to the parity fixture and a focused unit test for the helper. Keep module-level collaborators
looked up at call time so node tests can patch them.
