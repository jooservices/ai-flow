# Adapter run structure (JAIF-537)

`Adapter.run` in `aiflow/v4/adapters/base.py` starts one AI CLI process and reads its result. It is a short orchestrator over named steps; the order of side effects and where errors are caught are unchanged, and `tests/v4/test_adapter_run_parity.py` pins them with 22 recorded cases that run real child processes.

| Step | Owns |
|---|---|
| `_run_start_hook` | lets the container layer attach to the new process; if it fails the process group is stopped and the error is re-raised |
| `Adapter._stream_events` | reads output until the process ends, is stopped, times out or goes idle (`_stop_reason`) |
| `Adapter._handle_line` | logs a line, keeps the last 200 lines, parses JSON events and calls the adapter's `parse_event` and the progress callback (a failing callback is ignored) |
| `_finish_process` | stops a process that is still running, sets the exit code and duration, closes the log |
| `_explain_failure` | the provider's own message (`_last_error_message`) and the error kind (`_classify_error`: quota, auth, network, provider) |
| `_read_verdict` | the last `RESULT:` or `VERDICT:` line of the final text |

To change how a run ends or fails, change the step that owns it and add a case to the recorded table. This is a developer and CI structure note; end users do not interact with it.
