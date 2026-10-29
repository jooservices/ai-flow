# Provider routing by quota

AI Flow spends the provider with plenty of quota first and keeps the one that runs out fast for the work that needs
it, so nobody has to move tickets between providers by hand. Everything is on **Machines and AI**, panel
**AI provider routing**.

**Console screens:** `cc`, `machines`.

## Spend preference (headroom)

Each provider window is evaluated for each account that reports it, with that account window's own reset time. A reset
window counts as zero use. The provider's **headroom** is the lowest remaining share among its reported live windows;
the Machines and AI panel shows each account window separately. Ceiling and release comparisons use the full reported
precision; the panel rounds percentages to one decimal place for display.

The **spend preference** score starts at the headroom, loses up to 15 points when the provider's accounts are forecast
to run out before reset (JAIF-245, the `quota_at_risk` flag), and gains 10 points when a window with 20% or more left
resets within the hour. Providers are ranked by the full score; the owner's order decides only when scores are equal. The
default order is **Codex, Grok, Claude** (OpenCode after them). The panel shows each provider as *spend first*,
*normal*, *keep for need* or *held back*.

A provider that reports no quota windows has **unknown quota**. It is treated as having full headroom and the panel says so.

## Offers

Accounts poll for work, so routing filters the candidate list: an account is not offered a ticket while an account of
a more preferred provider can take it (same eligibility rules: ready, online, role, project, tools, licence). A
provider that is not held back defers only while a preferred account has a free worker slot, so a busy preferred
provider never leaves work waiting. Peer probes include the final quota-capacity decision, so a preferred account that
cannot finish the ticket with its reported quota does not suppress a provider that can. The saved order and ceilings
are read on every poll, so they apply to the next offers.

`GET /v1/work/candidates` runs the registered quota-risk and provider-headroom eligibility rules before capacity
routing; peer probes apply account, machine and project entitlements and the same quota-capacity decision as an offer.
The master cycle remains separately claimable through `GET /v1/master/candidate` and is never deferred by provider
headroom; BA chat jobs continue through `GET /v1/ba-chat/candidate` and their existing claim flow.

## Ceiling and hold-back

The owner sets a **ceiling per provider** (percent of the quota window, default 85). It applies to every window the
provider reports, each with its own reset time; there is no separate period. A provider with any account window above
its ceiling is **held back**: a 95% account window cannot be averaged with another account's 0% window to avoid the
ceiling. It is offered only

* triage (the master cycle is never deferred),
* tickets labelled `ai-<provider>` for it,
* work no other provider can take.

A held-back provider returns to normal use when the held window resets, or when every window is below 60% (or below
its ceiling when that is lower). Between those it stays held, so it does not flap around the ceiling.

## Role coverage

The watchdog's existing `role_coverage` check covers master, intake_refiner, verify, dev and release (post_merge),
counting distinct providers among each role's ready accounts. It excludes globally paused accounts and roles switched
off by role health. Held-back accounts keep their ready status and remain available as fallback, so quota routing does
not remove role coverage.

## Why a ticket went to an account

Each claim records a reason, shown under **Why each ticket went to its account**: `headroom order` (the provider was
first), `label`, `needed` (triage on a held-back provider), `fallback` (no preferred provider could take it, or the
provider is held back) or `only` (a single provider).

## API

* `GET /v1/capacity`: returns the quota-capacity dashboard. Work-offer routing separately tests each account's
  forecast and applies provider order and hold-back before selecting among capacity-safe accounts.
* `GET /v1/provider-routing`: ranking, headroom, windows, hold-back state, settings, recent decisions.
* `GET /v1/provider-routing/decisions?ticket=KEY&limit=30&offset=0`: every recorded decision, newest first, with
  `total`; `ticket` finds one ticket's explanations however old. The screen has a ticket search and "Show older
  assignments". Both reads need the Machines and AI permission, like the rest of the panel.
* `PUT /v1/provider-routing/settings` (owner): `{"order": ["codex", "grok", "claude"], "ceilings": {"claude": 80}}`.
  Each change is audited.
* `POST /v1/work/claim`: records the account and routing reason when an offered ticket is claimed.
* `POST /v1/runs/{run_id}/result`: records completed-run token totals and any updated quota report, which feed later
  quota forecasts and provider spend preferences.

Settings are in `provider_routing_settings`, the hold-back state in `provider_routing_state`, decisions in `routing_decisions`
(migration `018_provider_quota_routing.sql`). Spend preference and hold-back evaluation are in
`aiflow/v4/control/provider_routing.py`; capacity-safe provider selection is in
`aiflow/v4/control/capacity_routing.py`. Provider settings, state and decisions live in
`aiflow/v4/control/repositories/provider_routing.py`, while quota and readiness reads come from
`aiflow/v4/control/repositories/accounts.py`. Role-specific candidate building is in
`aiflow/v4/control/work_eligibility.py`. The named `provider_headroom` eligibility rule is registered in
`aiflow/v4/control/eligibility.py`. Endpoints are in the discovered router
`aiflow/v4/control/routes/provider_routing.py`; the panel behavior lives in its feature-owned asset
`aiflow/v4/control/ui/provider-routing.js`, which the console entry bundles with the licensing notice.

## What provider routing does not touch

Version releases are not ticket work, so provider headroom does not apply to them: `GET /v1/version-releases/candidates`
and `POST /v1/version-releases/claim` keep offering and claiming a release to any account with a free session.

## Cost at scale

Deferring an offer asks the better-ranked providers whether they could take it. The answer of each peer account and
machine is shared by the worker polls of the next fifteen seconds (`PEER_OFFERS_TTL_S` in `eligibility.py`), and the
routing state, ready accounts and machine capacity by the polls of the next two seconds (`SNAPSHOT_TTL_S`), so ticket
pickup stays within its 30-second budget (6 s without provider routing, 9 s with it, measured on the scale proof) at 100 projects, 50 machines and 200 sessions. Headroom is advisory: a claim
is always checked again without the preference.

## Cost of a pickup

`POST /v1/work/claim` and `POST /v1/work/candidates` ask for the prior development run of a ticket only through
`control/dev_history.py`, in one query (the latest run that delivered work, else the latest finished one), so a node that
lacks the container runtime does not pay two queries per ticket and account (JAIF-449). On the 100/50/200 scale proof this
removed about 9% of the database statements of a run (147,343 to 134,068).
