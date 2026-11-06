# Node heartbeat providers

The node agent builds heartbeat fields through the registry in `aiflow/v4/node/heartbeat_providers.py`. Each entry uses
the stable control API field name, a collector, a top-level value type, and an optional preparation step for shared
observations. `collect_heartbeat_fields` prepares those observations once, then validates and returns the registered
fields. Control declares the accepted request fields in `HeartbeatBody` in `aiflow/v4/control/system_check_api.py`.

To add a field, add a small collector and a `HeartbeatProvider` entry under its API field name, add the matching field
to `HeartbeatBody`, and cover collection, registration, and schema behavior in `tests/v4/test_node.py`. Existing
providers may share cached worker, metric, and environment observations without collecting them twice.

## Routes

`POST /v1/nodes/{name}/heartbeat` receives the heartbeat fields from the providers. Undeclared fields are ignored.
