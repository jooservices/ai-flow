# Project report build

`build_report` in `aiflow/v4/control/report.py` returns the project or epic progress report. The public name, arguments and result stay the same. One named function builds each section.

Routes: `GET /v1/projects/{key}/report`, `POST /v1/projects/{key}/close`

The steps run in this order:

1. `load_project_tickets` reads the project's tickets.
2. `build_project_close_suggestion` counts Done epics for the whole project.
3. `require_epic` rejects an unknown epic, or a key that is not an Epic, when an epic was requested.
4. `tickets_in_scope` keeps non-epic tickets, and only descendants of that epic when one was requested.
5. `empty_report` returns the report when that scope has no tickets. `filled_report` reads runs, verify rework, the latest finished verify, owner transitions and incidents, then calls one builder per section.

Section builders:

| Section | Builder |
| --- | --- |
| Period | `build_period` |
| Ticket counts | `build_ticket_summary` |
| Lead time | `build_lead_time` |
| AI time | `build_ai_time` |
| Runs by outcome | `build_runs_by_outcome` |
| Rework rounds | `build_rework_rounds` |
| Tokens and cost | `build_usage` |
| Incidents | `build_incidents_opened` |
| Owner input | `build_owner_needed` |
| Slowest tickets | `build_slowest_tickets` |
| Requirement coverage | `_requirement_coverage` |
| Lessons | `build_lessons` on a filled report; the empty report keeps its own sentence |

`completion_times`, `summarize_runs`, `completion_samples` and `lead_time_by_ticket` prepare the timings and usage those builders read. `load_runs`, `load_rework`, `load_latest_verify`, `load_owner_needed` and `load_incidents` read the history in that order.

Add a section by giving it a named builder with a docstring and a focused test in `tests/v4/test_report_build_parity.py`. Call that builder by name from `empty_report` and from `filled_report`, in the key order each report already uses. Do not loop over a list of builders or look them up in a dictionary.
