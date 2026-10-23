# Ticket estimates and AI capacity planning

The Dashboard's **AI and machines → Capacity plan** panel estimates the work in ready tickets, projects how many
can finish with quota over the next seven days, identifies the account with the least capacity, explains recent
development assignments, and compares completed ticket estimates with recorded usage. The control API exposes the
same view at `GET /v1/capacity` to the console; it is not a developer-only command.

## Ticket facts and history

Each sample is one ticket, with the token use and elapsed time summed over its completed `dev` and `verify` runs.
The sample is marked as reworked if any of those runs ended with outcome `rework`. Incomplete and still-running runs
do not train the estimator. Samples use the ticket's current Jira type, labels, fields, summary and description.

Size is selected in this order:

1. A `size-xs`, `size-s`, `size-m`, `size-l` or `size-xl` label (the `size:<value>` spelling is also accepted).
2. Jira story points in `story_points`, `storyPoints`, `customfield_10016` or `customfield_10014`: 0–1 is XS,
   over 1–3 is S, over 3–5 is M, over 5–8 is L, and over 8 is XL.
3. Summary plus description length: up to 600 characters is XS, 601–1,800 S, 1,801–5,000 M, 5,001–12,000 L,
   and longer is XL.

Area comes from the first `area-*` or `area:*` label, then the first Jira component, then the project key. The
ticket type is Jira's issue type. This gives installations useful groups without requiring a new Jira field.

## Estimate ranges and error

For each category AI Flow uses the empirical 10th and 90th percentiles of ticket-level tokens and minutes. The
estimate first looks for matching type, size and area. It requires three samples to stop at that level; otherwise
it backs off to type and size, type, and then all ticket types. Any available broader history is used when no level
has three samples. When runs have identical values, the range is widened by ten percent on each side so it remains
a range. The rework percentage is the 90% Wilson interval for the observed fraction of tickets that needed
rework. With no recorded ticket history, the screen uses a size-based starting range (XS 2,000–8,000 tokens and
5–30 minutes, S 4,000–16,000 and 15–90 minutes, M 8,000–32,000 and 30–180 minutes, L 16,000–64,000 and
60–360 minutes, XL 32,000–128,000 and 120–720 minutes) and a 0–60% rework range.

The range is stored in the `details.capacity.estimate` field of the first development run, so later training does
not change what the owner saw when work was assigned. When the ticket is Done or Closed, the console sums its
completed `dev` and `verify` tokens and elapsed time from that assignment through completion. It reports whether
the actual is inside the saved range and the signed percentage difference from the range midpoint. Rework error
compares whether rework happened with the saved risk interval. Every finished ticket adds a sample, so subsequent
estimates and ranges update from new work.

## Quota-aware assignment

The control plane keeps existing eligibility checks for project, account role, machine, worker capacity, tools,
license and account cooldown. Among eligible accounts that can claim a development ticket, it reads the latest
quota assessment persisted by the 15-second watchdog, so candidate polling does not rebuild weeks of run and probe
history. An account is quota-safe only if the upper token estimate fits every quota window with a known
remaining-token balance and the forecast exhaustion time is later than the upper time estimate.
If at least one quota-safe account is eligible, the ticket is offered to the best safe account: senior tier for
large/XL or Highest-priority work, middle tier for other work, then greater quota headroom. The assignment reason
and estimate are retained with that run. The latest quota forecast is retained on the account. If forecasts are missing or every eligible account is predicted to run
out, the existing eligible route remains available so a ticket is not starved; the reason records that fallback.

## Weekly capacity

For each account the plan uses the tightest quota window. It starts with that window's remaining tokens and adds
the capacity of each full reset window that can occur within the next seven days. It divides this token budget by
the average upper estimate for ready tickets (or the M-size starting estimate when none are ready). The per-account
ticket counts are summed for quota capacity; the limiting AI is the account with the fewest estimated tickets.
Finishable tickets are capped at the number of currently ready tickets. Accounts that only have an estimated quota
window are labelled as estimates, with the confidence from quota awareness shown beside it.

The capacity panel is visible to console users with the Machines and AI permission. Project-scoped users see only
ready tickets, decisions and errors from projects they can access.
