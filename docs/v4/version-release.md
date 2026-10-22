# Automatic Jira version releases

AI Flow v4 can release an explicitly configured Jira project version without an owner approval step. The enabled
trial scope is project `TESTWP` in `jooservices/test-aiflow-wp`, for Jira versions `WordPress Core 0.5.0-beta`
and `WordPress Theme 0.5.0-beta`. The exact project and repository match is required; `jooservices/ai-flow`
is rejected even if someone adds release settings to its project entry.

## Readiness gate

The control loop checks every 30 seconds and reads `fixVersion` membership and current status from Jira each
time. It reads the member list again when a release worker claims the version and after the full checks finish.
Tickets added or removed while the version is open therefore affect the next check, and a change during the test
run stops publication. An empty version stays blocked.

Every current ticket must be `Done` and have these registry records from a run after its latest development run:

- **Reviewer:** a `verify` run by a different account from the latest `dev` run, with outcome `merged` or
  `human` and `details.verdict == "PASS"`.
- **QA:** the verify run merged successfully, or a `qa` run recorded the successful human-review merge. This is
  the existing v4 merge/QA evidence; version release does not introduce a new ticket approval button.
- **QC:** the verify run recorded at least one GitHub check run or commit status, and every recorded check ended
  as `success` or `neutral`. No CI evidence, pending checks, or failing checks leave QC missing.

The control plane creates one Jira `Release` tracker per configured version (outside that version's `fixVersion`)
and comments the current ticket list and each changed readiness result there. Blocked output names the tickets
and their missing status, reviewer, QA, or QC evidence. No repository merge, tag, GitHub Release, or Jira
Released update happens while a gate is blocked.

## Full checks and publication

When the live gate passes, a master-role worker claims a Redis lease and fencing token scoped to that Jira
version. It fetches the configured repository with read-only access, checks out the current source branch
detached, and runs every configured project check in order. The source defaults to `develop`; the destination
defaults to `master`. `version_release.checks` can override the project's `checks`; at least one command is
required. The control plane verifies the source branch still points to the tested commit before merging.

If a check fails, AI Flow creates one `Bug` for each failing command, assigns the Jira version, adds
`aiflow-ready` and `release-check` labels, moves each Bug to `To Do`, and links each Bug as blocking the Release
tracker. The version stays `failed` and is checked again by the normal loop after the Bugs have reached `Done`
and satisfy the full readiness gate. Because the Jira search can lag behind issue creation, every Bug key
recorded in the `jira-release-check-failed` audit must also appear in the live member list before a `failed`
version can be claimed again; a version whose Bugs are not indexed yet is blocked, not re-checked.

If publication stops at an integration error (merge conflict, protected destination branch, a failing GitHub or
Jira API, a tag that points elsewhere), the version also stays `failed` instead of returning to `pending`, so
workers do not reclaim it and rerun the full checks on every poll. AI Flow creates one Jira `Task` labelled
`release-publish`, links it as blocking the Release tracker, comments the error once, and records the Task in a
`jira-release-publish-failed` audit. The version waits until that Task is `Done`. A repeat of the same error
while the Task is open reuses it and posts no new comment. If Jira itself cannot take the follow-up Task, the
audit records a retry time one hour ahead and the version waits until then.

After all commands pass, AI Flow merges the configured source branch into the destination, creates a unique tag,
publishes a GitHub Release with notes listing the current version tickets and check results, and marks the Jira
version Released. The default tag combines the Jira project key and a normalized version name, for example
`testwp-wordpress-core-0.5.0-beta`; Core and Theme therefore cannot collide in their shared repository. A
configured `tag_prefixes` value replaces the component part and keeps the version suffix, so `widget-core`
for `Widget Core 1.2.0` produces `widget-core-1.2.0`. An
existing tag must point to the same release commit, and an existing GitHub Release is reused on retry. The
control plane then merges the destination back into the source. If that merge fails, it creates a Jira follow-up
Task and comments its key on the Release tracker.

The version lease fences worker reports and publication writes. Jira comments, Bugs, branch merges, tags,
GitHub Releases, and Jira Released state are recorded in the registry events and `write_audit`; the Release
tracker also records the checked tickets, test result, master commit, tag, Release URL, and Jira status.

## Configure other projects

The exact TESTWP repository has the two trial version names enabled by default. For another project, configure
its project entry in the AI Flow control repository:

```yaml
version_release:
  enabled: true
  versions:
    - "Widget Core 1.2.0"
    - "Widget Theme 1.2.0"
  source_branch: develop
  target_branch: master
  checks: [] # omit or leave empty to use the project's checks list
  tag_prefixes:
    "Widget Core 1.2.0": widget-core
    "Widget Theme 1.2.0": widget-theme
  issue_type: Release
  bug_issue_type: Bug
```

Only the listed unreleased Jira version names are eligible. Set `enabled: false` to disable the TESTWP preset.
Project checks are trusted control-repository configuration and run on the node as the unprivileged AI worker,
with the project's configured environment. The Jira integration account needs permission to update project
versions. The GitHub App installation needs Contents write access; release creation also requests Workflows write
because GitHub requires it when the tagged commit adds or changes workflow files.
