# Untried verifier priority after a missing verdict

Within a ticket's current development round, an account is untried when its verify history has no `no_verdict=true`
result. Earlier ordinary verify results do not make it tried. An eligible untried account has priority over an account
with any no-verdict result, regardless of how long ago that result ended.

The work eligibility path applies the policy in three steps:

1. `verify_ticket_history` reads per-account verify results for the candidate tickets, bounded to the current
   development round.
2. `_verify_retry_blocked` bars an account with two newest consecutive no-verdict results. For a verifier with one or
   more no-verdict results, `_untried_verify_peer` checks other unpaused verify accounts, excluding the development
   author and peers that have a no-verdict result.
3. `_peer_verify_tickets` uses the existing load and version checks, then asks normal ticket eligibility with routing,
   capacity routing, and the retry guard disabled. It caches results per account and machine for the poll. A peer that
   can take this ticket keeps the tried verifier out of both offers and claim revalidation. With no such peer, a
   verifier with only one consecutive no-verdict can retry immediately.

The first completed reviewer is preferred among eligible untried reviewers. A tried first reviewer cannot delay an
untried peer, and cannot prevent fallback when no untried peer is eligible. A successful new development round resets
the history used by this policy. No time window or stored field is added.

To change this policy, update the history or eligibility rule in `features/work/eligibility_candidates.py`, and keep
offer, claim, peer-probe, first-reviewer, and development-round reset coverage in
`tests/v4/test_verify_no_verdict.py` and `tests/v4/test_review_rounds.py` aligned.
