# Repositories by Jira epic

Open **Projects** and choose **Repositories** beside a configured project. The editor reads the current epics from
Jira and the repositories visible to the connected GitHub App. Choose a repository for each epic, then save the
settings. Unmapped epics and tickets without an epic use the project's default repository.

The editor can set a base branch, stack and checks for each repository. Leave a value blank to inherit the
project setting. The node toolchain includes every stack used by the project, so a node is ready for all of its
repository assignments before it can claim work.

Settings are committed to `projects/<KEY>.yaml` in `aiflow-control` and every save is recorded in `write_audit`.
The project file keeps `repo` as the default and adds the repository overrides and epic mapping:

```yaml
key: JOOWP
repo: jooservices/wordpress-core
base: develop
checks:
  - composer install
  - vendor/bin/phpunit
repositories:
  - repo: jooservices/wordpress-core
    base: main
    stack: php-8.3
    checks:
      - composer install
      - vendor/bin/phpunit
  - repo: jooservices/wordpress-mcp
    base: develop
    stack: node-24
    checks:
      - npm test
epic_repositories:
  JOOWP-1: jooservices/wordpress-core
  JOOWP-2: jooservices/wordpress-core
  JOOWP-3: jooservices/wordpress-core
  JOOWP-306: jooservices/wordpress-mcp
```

For the first JOOWP setup, Platform (`JOOWP-1`), Core (`JOOWP-2`) and Theme (`JOOWP-3`) use
`jooservices/wordpress-core`; MCP (`JOOWP-306`) uses `jooservices/wordpress-mcp`. The owner can choose the Bundle
(`JOOWP-305`) repository when configuring it in the editor.

The API resolves subtask parents up to their epic before selecting settings. The selected repository is recorded
when a run is claimed and is reused for its checkout, PR, verification, rework, merge and human review. A version
release evaluates and publishes each repository's tickets independently, with its own release tracker and
repository state. Jira marks the shared version Released only after every repository represented by that version
has completed its release, so releasing the Core epic does not mark the Theme repository's work as released.
Existing projects without `repositories` or `epic_repositories` continue to use the single `repo` setting.
