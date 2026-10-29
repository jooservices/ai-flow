# Technical decisions are made by the SA role

After a ticket is Ready, a question about a test, a refactor, a module, a name, a library, a schema or how to split a change
is an engineering decision. `core/technical_decision.py` recognizes it and the escalation classifier gives it the class
`technical_decision` (eight classes now; only `new_business_decision` and `irreversible_or_production` reach the owner).

- The triage AI acts as the SA: it must answer with `DECISION: ANSWER`, the options it considered, the choice and the reason.
  `ESCALATE` is refused for this class.
- The answer is settled on the escalation, posted on the ticket, and stored in the decision register (source `sa`, decided by
  `sa`) so the next similar question is answered from the register without a new run.
- Text about money, payment, security, secrets, licensing, production, customers or a requirement conflict is never classified
  as technical; it stays with the owner classes.
- `POST /v1/escalations/raise` and `POST /v1/escalations/triage` apply the rule.
