# Keep open pull request titles in sync

Each development run builds its pull request title from the current Jira summary and submits it through
`POST /v1/github/pr`. When GitHub already has an open pull request for the run's branch, AI Flow compares that
PR's title with the requested title. A changed title is updated through GitHub's pull request API; a matching
title is left alone. This keeps the PR title current after a Jira summary change without creating a duplicate PR.

The update uses the GitHub App installation's existing `pull_requests: write` permission and is covered by the
same fenced and audited control API write as PR creation.
