# Worker and control release skew

Workers and the control API can briefly run different v4 releases during a canary or rollout. Additive
worker-facing endpoints must not make the newer worker stop polling work supported by the older control API.

Version-release candidate discovery is optional for workers. If an older control API returns HTTP 404 for
`GET /v1/version-releases/candidates`, the worker treats it as an empty candidate list for that poll and
continues with BA chat, master and regular work discovery. It does not claim version releases until the control
API supports the endpoint. The 404 fallback is enabled only for this discovery call; required API calls and
other errors keep their normal failure behavior.

For an API change that cannot be made optional and backward-compatible, deploy the control API before workers
that require it, or keep those workers off a node whose control API has not been upgraded. A canary should
remain able to poll its existing queues while waiting for a newly introduced optional endpoint.
