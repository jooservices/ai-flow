# Automatic Jira version releases

AI Flow v4 releases an explicitly configured Jira project version without an owner approval step. The enabled
trial scope is project `TESTWP` in `jooservices/test-aiflow-wp`, for Jira versions `WordPress Core 0.5.0-beta`
and `WordPress Theme 0.5.0-beta`. The exact project and repository match is required; `jooservices/ai-flow`
is rejected even if someone adds release settings to its project entry. In v4, the configured unreleased Jira
version is the trigger; AI Flow creates one `Release` tracker and sets its Jira `Version` field when that field
exists in the project.

## Readiness gate

The control loop checks every 30 seconds and reads `fixVersion` membership and current status from Jira each
time. It reads the member list again when a release worker claims the version and after full checks finish.
Tickets added or removed while the version is open affect the next check, and a change during the test run stops
publication. An empty version stays blocked.

Every current version ticket must be `Done` and have these registry records from a run after its latest
development run:

- **Reviewer:** a `verify` run by a different account from the latest `dev` run, with outcome `merged` or
  `human` and `details.verdict == "PASS"`.
- **QA:** the verify run merged successfully, or a `qa` run recorded the successful human-review merge.
- **QC:** the verify run recorded at least one GitHub check run or commit status, and every recorded check ended
  as `success` or `neutral`.

The control plane comments the current ticket list and changed readiness results on the Release tracker. No
repository files, PR, merge, tag, GitHub Release or Jira Released update is made while a gate is blocked.
The release path does not use the `production_release` owner policy and never moves the Release tracker to
`Human review`.

## Full checks and publication

When the live gate passes, a master-role worker claims a Redis lease and fencing token scoped to that Jira
version. The control plane reads the source head (default `develop`) and cuts a release branch named
`release/<normalized-version>-<source-sha>`. The worker checks out that branch and runs every configured project
check in order. Commands come from `version_release.release_check`, then project `release_check`, then
`version_release.checks`, then project `checks`; at least one command is required. The destination defaults to
`master`. The control plane checks both the source SHA and release branch head again before publishing, so a
changed branch starts a fresh run.

If checks fail, AI Flow creates one Jira `Bug` for each failing command in `Backlog`, labels it `release-qa`, and
links it with `Blocks` to the Jira Release tracker. The bug is outside Jira version membership; its status must
become `Done` before the release is eligible again. The normal version scan then claims a new run and repeats all
checks. Repeating the same completion for the same fenced run reuses its recorded Bug keys. A failed check does
not update the changelog, README or manifests and does not open a PR, merge, tag or GitHub Release.

A GitHub or Jira publication error (conflict, protection rule, missing permission, or API failure) leaves the
version `failed` behind one blocking `release-publish` Task. The Task becomes the retry gate and the next worker
runs the checks again after it is `Done`.

After checks pass, the control plane creates a dated `CHANGELOG.md` section from the Jira version tickets,
grouped as Added, Changed and Fixed. It updates a managed Current release block in the README with the version
and included tickets. It discovers existing `package.json`, `composer.json`, `package-lock.json`, `pyproject.toml`,
`Cargo.toml`, `setup.cfg`, `Chart.yaml` and `.csproj` manifests, and updates only those that already contain a
version field. Only the project's own version is changed: `[project]`/`[tool.poetry]` in `pyproject.toml`,
`[package]`/`[workspace.package]` in `Cargo.toml`, top-level `version`/`appVersion` in `Chart.yaml` (never
`dependencies[].version`), `[metadata]` in `setup.cfg`. A project may provide an explicit `manifest_files` list. The control plane commits changed files
on the release branch before it opens a non-draft PR from that branch to `master`.

Release PRs use GitHub's **merge commit** method. Squash is never used, so the commits from `develop` remain in
`master` history. Before merging, AI Flow enables the repository's `allow_merge_commit` setting if it is disabled.
The GitHub App needs `administration: write` for this setting; the release flow does not alter branch protection.
AI Flow merges the PR automatically. Its body and the GitHub Release notes list the version tickets and project
check results.

After the PR merge, the tag points at the merge commit on `master`. By default, the tag combines the project key
and normalized version name, for example `testwp-wordpress-core-0.5.0-beta`. A configured `tag_prefix` or
`tag_prefixes` replaces the project component and keeps the numeric version, for example `widget-core-1.2.0`.
The GitHub Release uses that exact tag. Jira's Released flag and release date are set last. An existing tag must
point to the same commit; existing PRs, tags, Releases and Jira Released state are reused on retry. The registry
records the merge commit, tag, GitHub Release URL and Jira version state. If Jira reports Released but the
registry write was interrupted, the periodic scan reconstructs the state from the audited PR merge, tag and
GitHub Release.

After publication, AI Flow compares `master` with `develop`. If `master` has commits that `develop` does not,
it creates one ready Jira merge-back Task (`master` into `develop`). It creates no Task when the branches
already contain the same history. The outcome is audited before comments or events, and the periodic scan retries
an unrecorded merge-back Task without creating duplicates.

The version lease fences worker reports and publication writes. A completion is accepted only from the run that
holds the lease of the Jira version named in the URL; a run claimed for another version is refused before any
write. Jira-controlled ticket summaries are escaped as literal Markdown in release notes. Jira comments, Bugs,
release file commits, PR merge, tags, GitHub Releases, Jira Released state and merge-back Tasks are recorded in
registry events and `write_audit`; the Release tracker also records the checked tickets, test result, master
commit, tag, Release URL and Jira status.

## Configure other projects

The exact TESTWP repository has the two trial version names enabled by default. For another project, configure
its project entry in the AI Flow control repository:

```yaml
release_check: # optional project-level override
  - composer install
  - vendor/bin/phpunit
tag_prefix: widget
version_release:
  enabled: true
  versions:
    - "Widget Core 1.2.0"
    - "Widget Theme 1.2.0"
  source_branch: develop
  target_branch: master
  release_check: [] # omit or leave empty to use project.release_check, then project.checks
  changelog_file: CHANGELOG.md
  readme_file: README.md
  manifest_files: [] # omit for automatic discovery of supported manifests
  tag_prefixes:
    "Widget Core 1.2.0": widget-core
    "Widget Theme 1.2.0": widget-theme
  issue_type: Release
  bug_issue_type: Bug
```

Only listed unreleased Jira version names are eligible. Set `enabled: false` to disable the TESTWP preset.
Project checks are trusted control-repository configuration and run on the node as the unprivileged AI worker,
with the project's configured environment. The Jira integration account needs permission to update project
versions. The GitHub App installation needs Contents, Pull requests and Workflows write permissions. It also
needs Administration write if the repo must have merge commits enabled; the App manifest requests this
permission from the owner during setup.
