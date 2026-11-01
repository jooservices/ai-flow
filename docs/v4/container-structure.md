# Container runtime structure

`worker/containers.py` remains the compatibility entry point and re-exports names used by workers and tests.
Command policy and Compose snapshots live in `worker/container_compose.py`; storage checks and supervised process
execution live in `worker/container_exec.py`; client shims and trusted broker launch construction live in
`worker/container_client.py`. Existing mount policy and pinned worktree logic stay in `worker/container_policy.py`
and `worker/container_pin.py`.

To extend the runtime, change the module that owns the behavior, keep the call from `containers.py` explicit, and
preserve its existing re-export when callers use the old import path. Keep helpers small and test exact command argv
in `tests/v4/test_containers_characterization.py`; add a focused test for each new helper that can run without a
container service.
