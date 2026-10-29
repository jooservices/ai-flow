# Owner questions have a deadline

An owner question never blocks the flow for long. `control/escalation_deadline.py` runs in every watchdog pass:

- A question that carries a recommended option and is still unanswered after **12 hours** (`DEFAULT_DEADLINE_HOURS`) is decided
  with that recommendation, unless it is about an irreversible or production action. The register entry becomes `decided` by
  `deadline`, the waiting tickets resume, and the note says it was applied automatically so the owner can reverse it in
  Command Center.
- A question without a recommendation, or about an irreversible or production action, is never applied automatically. It opens
  one `owner_question_overdue` incident and keeps waiting.
- The question text, at most a few options and the recommendation are stored when the question is asked
  (`POST /v1/escalations/triage`); the deadline is counted from that moment.
