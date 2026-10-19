## Your task: implement the ticket
1. Read the ticket, the earlier comments and the rework context above. If a previous AI started, continue from its branch state (`git log`, `git status`); do not start over.
2. Implement exactly what the ticket asks and meets its acceptance criteria. No extras. If the ticket is unclear or contradicts the code and no sensible default exists, stop and finish with `RESULT: BLOCKED: <the exact question>` (also put the reason in a ticket comment).
3. Stay up to date: run `git fetch origin {base}` and `git rebase origin/{base}` before you start and again before you finish (resolve conflicts, keep the ticket's intent). Never push, merge or change the Jira status: the supervisor does that.
4. Rework: when the rework context lists CI failures or feedback, fix the real cause, not the symptom. For EVERY feedback item write one entry to `{replies_file}` as a JSON list of `{"id": "<item id>", "action": "fixed" | "rejected", "reply": "<what you changed, or why it is invalid>"}`. A rejection needs concrete evidence (code, test output, a rule of the repository). Do not reject a human reviewer's request: fix it, or reject with evidence and the supervisor will ask the owner.
5. Commits follow Conventional Commits, as the repository enforces them in CI: `type(scope): Subject (KEY)`. The Type of this ticket is {type}: use feat for Feature, fix for Bug and Hotfix, chore for Chore, docs for Docs, refactor for Refactor. Read the repository's commit rules first (`.github/commitlint.config.mjs`, `commitlint.config.*`, `CONTRIBUTING.md`, `AGENTS.md`). Usual rules: type is one of feat, fix, docs, style, refactor, perf, test, build, ci, chore, revert; the subject starts with an uppercase letter, has no trailing period; the header is at most 120 characters. End the header with `({key})`.
6. Run the checks the repository defines (tests, lint) through the shared runtimes. Add tests for behaviour you add.
7. Post the final hand-over comment.

Finish your last message with these lines:
- `PR_TITLE: <the pull request title, in the same Conventional Commits format, ending with ({key})>`
- exactly one of `RESULT: DONE` (every acceptance criterion is met and committed) or `RESULT: BLOCKED: <reason>` (only when a human decision is truly required).
