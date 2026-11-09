# Node heartbeat field registration

`aiflow/v4/node/heartbeat_providers.py` registers each heartbeat collector beside its field declaration. The field
order is explicit and preserves the payload keys: `slots_total`, `slots_used`, `release_version`,
`release_protocol`, `tools`, `services`, `metrics`, `workers`, `credential_expiries`, `system_conditions`,
`credential_checks`, `auto_fixed_tools`, `auto_fixed_conditions`, `runtime_settings`,
`runtime_settings_public_key`, and `environment_items`.

Preparation is also registered on its owning provider and runs before field collection in this order: `metrics`,
`tools`, `workers`, then `environment_items`. Metrics updates `previous_metrics`; tools refresh only after their
cached inventory is more than 600 seconds old; workers are collected once and shared by their fields; environment
items are filtered against each project's local environment file.

To add a provider, define its collector next to the other collectors and call `register_heartbeat_provider` with its
stable field name, value type, and a unique payload `order`. Providers with preparation also supply a preparation
function and a unique `prepare_order`. Registration rejects duplicate names and order values. The public
`HEARTBEAT_PROVIDERS` mapping is read-only. Add newly accepted fields to the control heartbeat schema separately.
