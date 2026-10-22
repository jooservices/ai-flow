# OpenObserve event delivery

The control plane writes events to the registry first, then sends them to the `aiflow4_events` OpenObserve
stream. A `MemoryTableOverflowError` is an OpenObserve ingestion memory budget rejection. The reported 300 MiB
resident use under a 768 MiB container limit does not mean there is room in the ingestion table: OpenObserve's
`ZO_MEM_TABLE_MAX_SIZE` limits the aggregate memtables and defaults to about half of available memory. The stream
is populated by regular control events and node heartbeat telemetry. The control request batch size was 500;
that affects request pressure, but the overflow is the aggregate memtable ceiling rather than one oversized batch.
See the [OpenObserve configuration reference](https://openobserve.ai/docs/administration/configuration/environment-variables/)
for the memtable settings and the [single-node performance guidance](https://openobserve.ai/docs/enterprise-setup/performance/)
for memory cache sizing.

## Lab sizing

`deploy/v4/o2/lab.env` sets a 512 MiB aggregate memtable budget, caps each memtable at 128 MiB, and limits the
query and DataFusion caches. Set the OpenObserve container memory limit to 1 GiB and load this file in the
existing OpenObserve service. OpenObserve is attached as an external service in this repository's control
Compose file, so that service's deployment must consume the env file and memory limit.

## Delivery and alerting

The control shipper sends at most 100 events per request. It retries the same batch with exponential delays from
1 to 60 seconds, keeping the batch intact and limiting queued events to the existing 10,000-row queue. It logs
the first rejection and then at most once every five minutes while the failure continues. After two minutes of
continuous rejection it opens a warning incident in the registry and sends it through the configured alert
channels. The incident is scoped to the exporter and stream, so each control API replica and the loops exporter
can report independently. The first accepted batch closes the incident and logs recovery.

Registry events remain available to the Command Center during an OpenObserve outage. High-volume heartbeat
telemetry is OpenObserve-only and can be lost if the in-memory queue fills during a prolonged outage.

## Verification

The tests check the checked-in lab memory budget, batch preservation and bounded retry delay, warning rate limit,
and incident open/resolve behavior. Confirm the OpenObserve service has loaded `lab.env`, then monitor the
`aiflow4_events` stream and the exporter incidents during a normal one-hour lab run; the automated test suite
cannot prove the live service remains below its memory ceiling for that hour.
