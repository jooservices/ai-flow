# Deploy lint

**Console screens:** none (CI only)

Dockerfiles and shell scripts under `deploy/` and `scripts/` are linted by the workflow `.github/workflows/lint-deploy.yml`,
which runs on pull requests that change `deploy/`, `scripts/`, `.hadolint.yaml` or the workflow itself, and always in the merge queue (the merge group has no path filter). Everything it uses is free for a public repository. The actions are pinned to a commit; the runner image (`ubuntu-latest`) and the preinstalled shellcheck are not, so a runner update can change the lint result.

- **Hadolint** checks every Dockerfile under `deploy/` with `.hadolint.yaml`; a finding of severity warning or above fails the job.
  `DL3008` (pin apt package versions) is ignored on purpose: Ubuntu removes old package versions within weeks, so pinning would
  break rebuilds. The base image is pinned and kept current by Dependabot.
- **Shellcheck** checks every file whose first line is a `sh` or `bash` shebang (Python scripts in the same folders are skipped),
  at severity warning. A finding is fixed or carries a `# shellcheck disable=` comment with the reason.

Run it locally with `shellcheck --severity=warning <files>` and the hadolint binary from its GitHub release.
