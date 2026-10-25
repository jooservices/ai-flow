# Developer hand-over

A change that goes back from review costs a full development run and a full review. Measured on merged tickets, the
average ticket went back about four times, mostly for defects the developer could have found: a missing edge case,
an access check, a criterion half built, a document not updated. Reviewers named the acceptance criteria in few
developer summaries, so nobody could tell at a glance which criterion was proven where.

## What the developer does

`prompts/v4/dev.md` makes the developer work in a fixed order:

1. **Requirements ledger.** Every acceptance criterion, rule, test case and definition-of-done item of the ticket,
   one line each, before any code.
2. **Build and test.** Code and tests per ledger line, including the failing input, empty value, boundary and
   permission cases.
3. **Self-review of the diff** as a hostile reviewer: completeness, access and security, robustness, scope and
   structure, documentation and the project checks. Findings are fixed before the hand-over.

The reply ends with `AC-MAP:` (one line per criterion: the code that implements it and the test that proves it) and
`SELF-REVIEW:` (what the review of the diff found and fixed), next to `SUMMARY:` and `TESTS:`.

On a rework the developer numbers the reviewer's findings, asks of each whether the same kind of defect exists
elsewhere in the change, fixes those too, and updates the map.

## What the flow does with it

- `aiflow/v4/core/handover.py` parses the reply. The map and the review are stored in the run `details` (`ac_map`,
  `self_review`) together with `ac_missing`, the `ACn` ids of the ticket the map does not mention.
- The pull request body gets an **Acceptance criteria map** and a **Self-review** section.
- `POST /v1/work/claim` for a verify run returns the map and the review inside `pr.dev_summary`, after the developer's
  summary.
- The reviewer receives the map as delimited data (`developer-ac-map`, `developer-self-review` tags, never instructions) with the developer's summary and checks every entry against the code and the test it
  names. A criterion missing from the map is a finding, and a map that is absent is a signal to map the criteria
  itself.

## Measuring it

Rework rounds per merged ticket, findings by type (edge case, access, documentation, criterion not met) and the
share of developer runs with an `ac_map` in `details` show whether the change works. A rising share of runs with a
non-empty `ac_missing` means the prompt is not followed and a hand-over gate is the next step.
