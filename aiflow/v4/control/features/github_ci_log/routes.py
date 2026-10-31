"""Routes of the github_ci_log feature, moved out of the control app as they were (nested in `create_app`).

`install` registers them on the app. What they still need from the app (helpers and shared state defined there) arrives
as `deps`; the names the routes define for the rest of the app are returned."""
import re
from types import SimpleNamespace

from fastapi import Depends, HTTPException, Query


def install(app, ctx, deps) -> SimpleNamespace:
    from ...gateway_helpers import github_gateway  # noqa: PLC0415 - api imports this module
    audit = deps.audit
    fenced = deps.fenced
    node_auth = deps.node_auth
    repository_for_run = deps.repository_for_run
    # --- escalation classes and the owner decision register (docs/v4/escalations.md) --------------------------


    @app.get("/v1/github/ci-log")
    def github_ci_log(run_id: str, fence: int, branch: str = "", sha: str = "", lines: int = Query(200, ge=1, le=1000),
                      node: str = Depends(node_auth)) -> dict:
        """The tail of the failing CI job log of the run's pull request: a developer never has to ask for it."""
        run = fenced(run_id, fence, "github", "ci-log", {"branch": branch, "sha": sha[:12], "lines": lines})

        def refuse(reason: str) -> None:
            audit("github", "ci-log", run["ticket_key"], run_id, fence, run["account_id"], False, reason,
                  {"branch": branch, "sha": sha[:12]})
            raise HTTPException(403, reason)

        if run["node"] != node:
            refuse("CI logs are available only to the node running this developer run")
        if run["step"] != "dev":
            refuse("CI logs are available only to a developer run")
        project = ctx.repo().projects[run["project"]]
        expected_branch = str(run.get("branch") or "")
        expected_pr = str(run.get("pr_url") or "")
        pr_match = re.fullmatch(r"https://github\.com/([^/]+)/([^/]+)/pull/(\d+)/?", expected_pr, re.I)
        expected_repository = repository_for_run(run, project)

        if not pr_match or not expected_branch or not expected_repository:
            refuse("developer run has no recorded pull request")
        pr_repository = f"{pr_match.group(1)}/{pr_match.group(2)}"
        if pr_repository.lower() != expected_repository.lower():
            refuse("recorded pull request does not match the run repository")
        if branch and branch != expected_branch:
            refuse("requested branch is not the run's pull request branch")
        if not branch and not sha:
            raise HTTPException(400, "give the recorded pull request branch or commit sha")
        github = github_gateway(ctx)
        if not github:
            raise HTTPException(503, "GitHub App is not connected")
        repository = expected_repository.split("/", 1)[-1]
        pull_request = github.pr(repository, int(pr_match.group(3)))
        head = pull_request.get("head") or {}
        head_repo = head.get("repo") or {}
        pull_request_sha = str(head.get("sha") or "")
        if (int(pull_request.get("number") or 0) != int(pr_match.group(3))
                or head.get("ref") != expected_branch
                or str(head_repo.get("full_name") or "").lower() != expected_repository.lower()
                or not pull_request_sha):
            refuse("recorded pull request no longer matches the run's repository and branch")
        if sha and sha.lower() != pull_request_sha.lower():
            refuse("requested commit is not the current head of the run's pull request")
        sha = sha or pull_request_sha
        log_tail = github.failing_job_log(repository, sha, lines)
        audit("github", "ci-log", run["ticket_key"], run_id, fence, run["account_id"], True,
              log_tail.get("job") or "no failing job", {"sha": sha[:12], "lines": lines})
        return {"sha": sha, **log_tail}


    return SimpleNamespace(github_ci_log=github_ci_log)
