# Idle incident explanations

`idle_reasons` in `aiflow/v4/control/incidents.py` keeps the explanation order explicit:

1. `_idle_online_nodes` finds fresh nodes accepting work; the caller adds the no-node reason.
2. `_idle_parked_tool_wait_reasons` and `_idle_retry_backoff_reasons` explain ticket waits.
3. `_idle_project_pause_reasons` and `_idle_project_license_reasons` report project gates.
4. `_idle_node_reasons` checks serving nodes, using `_idle_compatible_nodes` and
   `_idle_node_tool_or_service_block` for version, tool and service gates.
5. `_idle_account_inventory` collects readiness and quota state; `_idle_quota_reasons`,
   `_idle_account_license_reasons` and `_idle_account_readiness_reasons` explain account gates.

To add a reason, put its query and formatting in a small named helper, then call it explicitly from
`idle_reasons` at the point matching the current evaluation order. Add literal expected output cases to
`tests/v4/test_incidents_idle_parity.py`.

Routes: none call `idle_reasons` directly. The watchdog and Flow Doctor loops use it to explain idle work.
