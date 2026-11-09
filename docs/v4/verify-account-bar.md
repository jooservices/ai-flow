# Bar a verifier after two missing verdicts

When a verification run ends without a `VERDICT:` line, control records `details.no_verdict = true`. Candidate
selection then keeps weak accounts from monopolizing the same ticket.

## Steps

1. `verify_ticket_history` loads newest-first no-verdict markers for each account, only for verify runs that started
   after the ticket's latest finished development run (`step = 'dev'`, `outcome = 'done'`). That finished development
   starts a new round and drops earlier verify history.
2. `_verify_account_barred` returns true when the two newest markers for an account are both no-verdict results.
3. `_verify_retry_blocked` refuses the ticket for a barred account. Otherwise it still prefers an eligible peer that
   has not ended this round without a verdict (JAIF-441).

To change the bar or the round boundary, update the history query and `_verify_account_barred` together, then keep
`tests/v4/test_verify_no_verdict.py` aligned. Role-health scoring of consecutive no-verdict results stays on the
account's verify-role history and is separate from this ticket offer bar.
