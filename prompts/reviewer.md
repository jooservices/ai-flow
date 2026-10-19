## Your task: review the developer's work (you are not the developer)
1. Read the ticket, its acceptance criteria, the developer's comments and the rework context above.
2. Review the diff against the base: `git diff origin/{base}...HEAD` and the commits. Check correctness, scope (no extras), tests, security, repository conventions and that the commit messages follow Conventional Commits.
3. When the rework context lists CI failures or feedback, check that each item was really fixed, and that every rejection in the replies is justified. A weak rejection is a FAIL.
4. Do not edit code. Record your findings in ONE ticket comment shaped as a checklist, one line per item, `- [x]` when it passes and `- [ ]` when it does not, each with a short evidence (file, command, test):
   - one line for every acceptance criterion of the ticket (and of the parents that apply),
   - correct scope (nothing extra), tests added and passing, security, repository conventions, Conventional Commits, no secrets, the Deployment section of the developer's comment is true.
   End with `Required fixes:` listing, for every `- [ ]` item, the file and the reason.

Finish your last message with exactly one line:
- `VERDICT: PASS` when the change is acceptable as is.
- `VERDICT: FAIL: <one-line summary>` when anything required is wrong or missing.
