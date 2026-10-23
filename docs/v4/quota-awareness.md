# Account quota awareness

AI Flow records each completed run's account, provider, model, ticket, project, node, start and end times,
input/output tokens, and cost. Account probes and run results that include provider quota data are kept in
`account_quota_samples`; the seven-day history is computed from the run registry.

The account rows on Dashboard and Machines and AI show provider quota used and left, tokens and cost in the
current window, burn rates for the last hour, last day and whole window, the largest recent run, reset time,
prediction and confidence, plus a seven-day token chart. The Master weekly report carries the same account values.
Provider reset times take precedence. If a provider omits a reset time, AI Flow learns a reset from a recorded
utilization drop or a quota-stopped run with a reset offer.

For quota providers that expose utilization but not a token limit, recorded run volume is used to estimate the
token scale when possible; the provider's percentage remains the authoritative used/left value. For providers
without quota windows, AI Flow labels the result as an estimate. Its weekly estimated capacity is 125% of the
busiest prior complete UTC week. Until a complete week exists, it bootstraps at 125% of current-week use, with a
1,000 token floor. This is a planning estimate, not a provider limit.

Burn rates sum recorded run tokens and cost over each time range. The largest five percent of runs by input-token
context size are excluded from the steady-rate sum (whole runs only, so fewer than 20 runs are not trimmed); the
largest context run is shown separately. Confidence is high with at least 20 runs in the last 24 hours and a per-run token-rate
standard deviation below half its mean, medium with 8–19 similarly steady runs, and low with fewer than 8 runs or
a very uneven rate. The forecast compares remaining quota and that steady rate with the next reset. More than
20% projected remaining is “comfortable”, 0–20% is “tight”, and a predicted zero before reset is “will run out
before reset”.

When a forecast reaches zero before reset, the watchdog stores the account's at-risk flag and opens an
owner-notified quota prediction incident. Candidate routing prefers same-provider accounts that can take each
ticket; if no peer can take a ticket, the at-risk account remains eligible so a sole account cannot starve. An
exhausted account cools down, its released ticket returns to the normal work queue, and it becomes eligible again
after a successful post-reset probe clears its cooldown and the watchdog refreshes the forecast. Candidate polls
read the stored flag rather than rebuilding the account's run and probe history.

Codex and Grok adapters retain normalized quota windows when their JSON event stream provides limits. A provider
that reports only an exhausted balance continues to use account status and usage-based estimates. No provider
credential is stored in quota samples.
