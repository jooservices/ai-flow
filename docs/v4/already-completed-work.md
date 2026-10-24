# Already completed work

A clean developer run with no new file changes checks the ticket branch before it reports a failure. An open pull
request sends the ticket to **In Verify** with that pull request recorded on the run. If there is no open pull
request but the branch has commits ahead of the configured base, AI Flow pushes any base-integration commit,
opens a draft pull request, and sends the ticket to **In Verify**. The Jira run note and the Work board card say
the work was already done; the card links directly to the pull request.

If the branch has neither an open pull request nor commits ahead of the base, the developer run is recorded as
`failed` with error kind `no_work` and a reason naming the branch and missing work. The ticket returns to **To Do**
and follows the normal failure backoff, so the flow doctor can diagnose and retry it. This no-change failure is
marked in run details and has a zero role-health failure score; an ordinary failed run remains scored as before.

The check applies only to a clean result. A failed verdict, tool or service issue, or other worker error keeps its
normal failure path. Pull requests are looked up by the exact repository and branch through the fenced control API.
