# Provider routing by quota

AI Flow spends the provider with plenty of quota first and keeps the one that runs out fast for the work that needs
it, so nobody has to move tickets between providers by hand. Everything is on **Machines and AI**, panel
**AI provider routing**.

**Console screens:** `machines`.

## Spend preference (headroom)

For every window a provider reports (five hour, seven day, ...) the usage of its accounts is averaged. A window whose own
reset time has passed counts as reset. The provider's **headroom** is what is left in its fullest window.

The **spend preference** score starts at the headroom, loses up to 15 points when the provider's accounts are forecast
to run out before reset (JAIF-245, the `quota_at_risk` flag), and gains 10 points when a window with 20% or more left
resets within the hour. Providers are ranked in 10-point bands; inside a band the owner's order decides. The
default order is **Codex, Grok, Claude** (OpenCode after them). The panel shows each provider as *spend first*,
*normal*, *keep for need* or *held back*.

A provider that reports no quota windows has **unknown quota**. It is treated as having full headroom and the panel says so.

## Offers

Accounts poll for work, so routing filters the candidate list: an account is not offered a ticket while an account of
a more preferred provider can take it (same eligibility rules: ready, online, role, project, tools, licence). A
provider that is not held back defers only while a preferred account has a free worker slot, so a busy preferred
provider never leaves work waiting. The saved order and ceilings are read on every poll, so they apply to the next offers.

## Ceiling and hold-back

The owner sets a **ceiling per provider** (percent of the quota window, default 85). It applies to every window the
provider reports, each with its own reset time; there is no separate period. A provider above its ceiling in any
window is **held back**: it is offered only

* triage (the master cycle is never deferred),
* tickets labelled `ai-<provider>` for it,
* work no other provider can take.

A held-back provider returns to normal use when the held window resets, or when every window is below 60% (or below
its ceiling when that is lower). Between those it stays held, so it does not flap around the ceiling.

## Role coverage

Held-back accounts keep their status, so the existing `role_coverage` check in the watchdog (two ready accounts per
role) is unchanged and counts them. Because a held-back provider is only deferred while another provider can take the
ticket, its accounts remain the fallback and no role loses coverage.

## Why a ticket went to an account

Each claim records a reason, shown under **Why each ticket went to its account**: `headroom order` (the provider was
first), `label`, `needed` (triage on a held-back provider), `fallback` (no preferred provider could take it, or the
provider is held back) or `only` (a single provider).

## API

* `GET /v1/provider-routing`: ranking, headroom, windows, hold-back state, settings, recent decisions.
* `PUT /v1/provider-routing/settings` (owner): `{"order": ["codex", "grok", "claude"], "ceilings": {"claude": 80}}`.
  Each change is audited.
* `POST /v1/work/claim`: records the account and routing reason when an offered ticket is claimed.

Settings are in `provider_routing_settings`, the hold-back state in `provider_routing_state`, decisions in `routing_decisions`
(migration `018_provider_quota_routing.sql`). The code is `aiflow/v4/control/provider_routing.py`, wired in `eligible()` in `api.py`.
