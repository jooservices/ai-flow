# Atomic code and configuration deploy

`bin/aiflow-deploy` deploys code and configuration to machines listed under `[machines]` in `aiflow.toml`. Each
machine name is an SSH alias. Local and target checkout paths may differ: targets resolve their default paths
independently, and `AIFLOW_DEPLOY_REMOTE_CODE_DIR` or `AIFLOW_DEPLOY_REMOTE_CONFIG_DIR` can override them.

The deployer resolves one code commit for the whole deployment. It takes a per-target lock and fetches both repositories
into temporary worktrees. Preflight rejects dirty checkouts and checkouts ahead of or diverged from their candidate,
checks that each configuration TOML file has a `# aiflow-version: <version>` marker no newer than the candidate code's
`pyproject.toml` version, and runs the candidate worker configuration check with the live workspace root and staged
configuration directory. It reconciles candidate configuration inventories with the invoking inventory, so newly
listed machines also pass preflight and receive deployment. No target activates until all discovered targets pass.

After preflight, the deployer fast-forwards both repositories on each target and restarts Linux control services using
the deployed configuration directory. Rollback state and target locks remain until a worker restart request succeeds
for the fleet. Activation or worker restart failure rolls back activated repositories and restores previous unit files,
service states, and systemd configuration environment. Rollback refuses to overwrite checkout changes made after
staging and reports that condition. The final output has a separate result for every discovered machine.

To add a target, add its worker entry under `[machines.<name>]` and use `<name>` as the target's SSH alias. By default,
each target's code checkout is `~/Workspaces/JOOservices/projects/ai-flow` and its config checkout is
`~/Workspaces/JOOservices/config`; set `AIFLOW_DEPLOY_REMOTE_CODE_DIR` or `AIFLOW_DEPLOY_REMOTE_CONFIG_DIR` to override
those paths. Both checkouts need clean working trees and `origin` remotes. Put a version marker in each TOML file in
the configuration checkout; update it only after code that understands that version is available.
