# Split children carry a developer's job description

When intake splits a ticket that is too large for one short AI run, it acts as the Team Lead. Every child ticket in the
split plan must contain, besides an `AC1:` line, three sections with content (`core/ticket_splitting.py`):

- `Files:` the files to create or change, with the module that owns the feature.
- `Skeleton:` the classes and functions to write, one line each.
- `Test cases:` the tests to write, one line each.

A child with a missing or empty section is rejected before any Jira write (`InvalidSplitPlan` names the section). Bullets and bold
headings are accepted and a section may hold several lines. The developer prompt (`prompts/v4/dev.md`) tells the developer to
follow those sections exactly and to ask nothing about structure, which keeps a subtask within the 15-minute target
([small tickets](small-tickets.md)).
