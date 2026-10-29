# Pull requests stay clean

A pull request carries its evidence in the description and a comment only when it helps the ticket.

- **One comment per kind, edited in place.** `POST /v1/github/comment` accepts `kind` (`verify` or `triage`). AI Flow keeps one
  comment of that kind on the pull request, found by a hidden marker (`<!-- aiflow:verify -->`), and edits it with the latest
  verdict instead of adding a new comment for every failed verification (129 such comments piled up on 40 pull requests).
  The history of attempts stays in the Jira ticket.
- **One identity.** Every write of AI Flow to GitHub uses the AI Flow bot. AI sessions that work on a pull request do not post
  status, triage or quota comments and reply only to threads with an actionable code finding (`AGENTS.md`).
- **Quiet bot reviews.** `.coderabbit.yaml` sets the `quiet` profile and switches off the walkthrough, summary, diagrams,
  status, labels and reviewer suggestions, so CodeRabbit leaves review threads and nothing else.
- Rate-limit, billing and summary notices from any bot are not findings: they are never answered and never counted.
- Code quality does not depend on any bot reviewer: see [Code quality gate](code-quality.md).
- `POST /v1/github/update-branch`, which keeps a pull request current with its base, writes no comment.
