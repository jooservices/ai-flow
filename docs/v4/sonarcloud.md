# SonarCloud analysis

Pull requests run a SonarCloud job in parallel with the tests (it does not wait for them and the test shards are not
wrapped in a coverage tool, so the pull request run does not get slower). The scanner uploads analysis for project `jooservices_ai-flow` in the
`jooservices` organization. The Quality Gate status appears on the pull request through SonarCloud's GitHub
decoration, and the README shows the Quality Gate badge.

`sonar-project.properties` sets the project key, organization, Python sources (`aiflow`), tests (`tests`), and exclusions for generated files (including `docs/v4/events.md`). The workflow resolves
`secrets.SONAR_TOKEN` and `secrets.SONAR_HOST_URL` the same way as DTO: the host defaults to `https://sonarcloud.io`
when the host secret is empty, and a missing token fails with a clear message that does not print the secret value.
Bot pull requests (no secrets) skip every step after the checkout. Coverage reaches the quality gate later through the
Codecov job of JAIF-574.

## One-time project import (organization admin)

If `jooservices_ai-flow` does not exist yet on SonarCloud, an organization admin imports the GitHub repository once:

1. Open SonarCloud for the `jooservices` organization and choose to analyze a new project from GitHub.
2. Select `jooservices/ai-flow` and keep the project key `jooservices_ai-flow`.
3. Confirm the organization already provides `SONAR_TOKEN` (and optional `SONAR_HOST_URL`) to this repository.
4. Never print the token value, and never store it in the repository, tickets, or logs.

The job is free for a public repository and is not a required check.
