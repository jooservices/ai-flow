# Connect GitHub from the console

Owners manage GitHub connections from **Integrations** in the v4 console. App keys and access tokens stay in the
control plane; workers receive a short lived repository credential or use the control plane Git relay, and the
control API continues to fence and audit GitHub writes.

## Before connecting

GitHub must be able to redirect the browser back to the control plane after App creation and installation. Set
`AIFLOW4_PUBLIC_URL` to the HTTPS URL owners use to open AI Flow in
`/etc/aiflow/secrets/control/aiflow4.env`, which Compose loads into the control services. The manifest flow is
disabled when that URL is not a valid HTTPS origin.

For example, add this line to `/etc/aiflow/secrets/control/aiflow4.env`:

```sh
AIFLOW4_PUBLIC_URL=https://control.example.com
```

## Create a new App

1. Sign in as an owner and open **Integrations**.
2. Enter the GitHub organization that will own the App and choose **Connect GitHub**.
3. GitHub opens its manifest flow. Review and create the private AI Flow App.
4. GitHub returns to AI Flow, which exchanges the one time manifest code on the control plane and redirects to
   the App installation page.
5. Select the account or organization and the repositories the App can access. GitHub returns to AI Flow after
   installation.

The manifest flow starts with `GET /v1/integrations/github/manifest/start` and saves an imported App through
`POST /v1/integrations/github/imports` when an owner chooses **Use an existing GitHub App**.

The manifest requests repository contents write, pull requests write, workflow write, Actions read, checks read,
statuses read, metadata read and administration write permissions. Actions read lets a developer run read the log
of a failing GitHub Actions job for its recorded pull request. Administration write lets a release run turn on merge commits
when a repository currently permits only squash or rebase; it does not change branch protection. The App subscribes
to no webhook events. The App registration belongs to the GitHub
organization selected in step 2. GitHub's manifest registration and callback contract is documented in
[Registering a GitHub App from a manifest](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest).
The setup callback includes an installation id; AI Flow verifies it against the App's signed JWT before saving it,
as recommended in [About the setup URL](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url).

## Connect an existing App

The existing App must have these repository permissions: contents write, pull requests write, workflows write,
Actions read, checks read, statuses read and administration write. Metadata read is always granted by GitHub.
Actions read is required to retrieve failing job logs for a developer run's recorded pull request. Administration
write is used only when a project release needs to enable merge commits. In **Integrations**, expand **Use an
existing GitHub App**, enter its App ID and select its PEM private key file. The control plane verifies the key
with GitHub and returns only the App name and its current installations. The key content is not rendered in the UI.

Use **Install or configure on GitHub** to select an account or organization and choose the repositories. Return to
AI Flow, refresh installations, select the correct installation and choose **Connect selected installation**.
AI Flow checks that the installation belongs to the supplied App and can issue an installation token before
activating it.

## Add and refresh repositories

The installation settings link in **Integrations** opens the GitHub page for adding or removing repositories from
the App installation. The **Add project** form also has **Add repository on GitHub** and **Refresh repositories**
controls. The project catalog asks GitHub for the installation's current repositories on each refresh, so a repo
added on GitHub is available without restarting AI Flow.

The console loads a project's repository assignments with `GET /v1/projects/{key}/repositories` and saves them with
`PUT /v1/projects/{key}/repositories`. It loads the available repositories from GitHub connections, which can be
added with `POST /v1/integrations/github/connections/token` or updated with
`PUT /v1/integrations/github/connections/{connection_id}/token`; an unused connection can be removed with
`DELETE /v1/integrations/github/connections/{connection_id}`.

Opening the installation settings page and changes to the repository list are recorded in `write_audit` under the
`github` target. The audit stores the owner, App and installation identifiers, and added or removed repository
names; it never stores private keys or installation tokens.

## Credential handling

The private key is held only by the control plane in `AIFLOW4_GITHUB_APP_DIR` (`/etc/aiflow/secrets/github` in the
v4 deployment). `app.pem` and the non-secret App metadata in `app.json` are written with owner-only file
permissions. Temporary setup files expire after one hour and are pruned on the next setup attempt. The client
secret and webhook secret returned by GitHub's manifest conversion are discarded.

Workers continue to receive the same short lived installation tokens as before, narrowed to the project repository
and required permissions. Tokens are returned only to the authenticated worker request; they are not included in
the UI, audit payloads or application log messages. The App configuration is shared between the API replicas and
reloaded when it changes, so a successful connection is available without restarting the control plane.

## Several connections: accounts, organizations and tokens

Owners can hold more than one GitHub connection, for example the organization `jooservices` through a GitHub App
and a personal account through an access token. **Integrations** lists every connection with its account or
organization, credential type (GitHub App or access token), status and the repositories it reaches, and offers
**Test**, **Replace** (a new token, or **Replace App** which runs the App flow again for that connection) and
**Remove** for each. Nodes never store any of these credentials.

- The installation you already have is the first connection, `default`; nothing changes for single-App installations.
  Its files stay in `AIFLOW4_GITHUB_APP_DIR`.
- **Add an access token**: paste a personal or fine-grained token, optionally the account or organization it is for
  (blank: the token owner) and a label. AI Flow verifies the token with GitHub, keeps it in the secret store
  (`connections/<id>/token`, owner-only) and never shows it again. The UI recommends GitHub Apps and warns on every
  token connection: a token reaches more, has no bot identity and cannot be limited per run.
- **Add a GitHub App**: use the same **Connect GitHub** manifest flow or **Use an existing GitHub App** (App ID and
  PEM) as before. When a connection exists, the new App is added as another connection (`connections/<id>/app.json`
  and `app.pem`) instead of replacing the first one.
- A connection serves the repositories of one account or organization that it lists.

### Which connection a repository uses

Each repository uses its assigned connection, or else the first connection of its owner that reaches it. On
**Projects**, **Repositories** shows a **GitHub connection** per repository (default repository, epic repositories
and any other) with the connections able to reach it; choosing one and saving records the assignment in the secret
store (`assignments.json`, by repository) and in the audit log (`assign_connection`). The control repository
`projects/<KEY>.yaml` never contains connection details. A repository no connection reaches, or an assigned
connection that cannot reach it, is refused (HTTP 409 on the run, 422 when saving); AI Flow does not fall back to
another account. A connection assigned to repositories cannot be removed until they are reassigned.

Remote branch cleanup is `POST /v1/github/delete-branch` (run, fence and the ticket's own branch). Command Center
deletes the branch with the connection assigned to the run's repository and audits the connection used.

Command Center performs pull request, comment, merge, branch, release and checks operations with the
connection assigned to the repository. For a GitHub App connection a node receives only the short lived
installation token limited to that repository (`POST /v1/github/token`). For an access-token connection, the same
endpoint returns a one-hour relay capability scoped to that repository, run, node and fencing lease. Git clone
and fetch traffic goes through Command Center, which uses the stored access token upstream. A second capability
can push only the run's branch; Command Center refuses other repositories, stale runs and other branches. The
stored access token is never sent to a node. Workers keep the relay capability in the Git process environment and
do not write it into the remote URL or Git configuration.

Clean-checkout probes use the same repository selection. App connections return a read-only installation token;
access-token connections return a one-hour read-only relay capability bound to the node, project and repository.
The node clones through the relay URL, and the relay refuses push requests and repositories no longer configured
for that probe.

### Audit

Every GitHub write in `write_audit` (target `github`, and `aiflow-control` for control repository commits)
carries `connection` in its payload: `default` or the connection id. This includes worker branch pushes through
the relay and version release publication. Token and key values never appear in the UI, logs, Jira, events or
audit rows.

### API routes served through a connection

These control API routes resolve the GitHub connection of the repository they act on, or list connections, and
refuse with HTTP 409 when none reaches it (`github_gateway(ctx, repo)`):

- Connections: `GET /v1/integrations/github`, `GET /v1/integrations/github/connections`,
  `POST /v1/integrations/github/connections/token`, `PUT /v1/integrations/github/connections/{connection_id}/token`,
  `POST /v1/integrations/github/connections/{connection_id}/test`,
  `DELETE /v1/integrations/github/connections/{connection_id}`, `GET /v1/integrations/github/manifest/start`,
  `GET /v1/integrations/github/setup`, `GET /v1/integrations/github/installation-settings`,
  `POST /v1/integrations/github/imports`, `POST /v1/integrations/github/imports/{pending_id}/connect`.
- Projects and repositories: `GET /v1/projects/catalog`, `POST /v1/projects`, `POST /v1/projects/preflight`,
  `POST /v1/projects/{key}/pause`, `DELETE /v1/projects/{key}`, `GET /v1/projects/{key}/repositories`,
  `PUT /v1/projects/{key}/repositories`.
- GitHub operations: `POST /v1/github/pr`, `POST /v1/github/comment`, `POST /v1/github/merge`,
  `POST /v1/github/update-branch`, `GET /v1/github/checks`, `GET /v1/github/git/{git_path:path}`,
  `POST /v1/github/git/{git_path:path}`, `POST /v1/nodes/{name}/check-probes/token`,
  `POST /v1/runs/{run_id}/result`, `POST /v1/version-releases/claim`,
  `POST /v1/version-releases/{version_id}/complete`, `GET /v1/version-releases/candidates`.
- Control repository commits (accounts, nodes, users, knowledge and review decisions, through the connection of
  the control repository): `DELETE /v1/accounts/{account_id}`, `PATCH /v1/accounts/{account_id}`,
  `POST /v1/accounts/{account_id}/sign-in`, `POST /v1/accounts/{account_id}/sign-in/complete`,
  `POST /v1/machines/{name}/accounts`, `DELETE /v1/nodes/{name}`, `POST /v1/nodes/join`,
  `POST /v1/nodes/{name}/operations`, `POST /v1/console/users`, `PUT /v1/console/users/{email}`,
  `PATCH /v1/console/users/{email}/disabled`, `POST /v1/knowledge/resources`,
  `POST /v1/knowledge/resources/{kind}/{resource_id}/restore`,
  `POST /v1/knowledge/proposals/{proposal_id}/approve`, `GET /v1/reviews`, `POST /v1/reviews/{key}/answer`,
  `POST /v1/reviews/{key}/approve`, `POST /v1/reviews/{key}/ask-ai`, `POST /v1/reviews/{key}/reject`,
  `POST /v1/reviews/{key}/rework`, `POST /v1/reviews/{key}/skip-gate`, `GET /v1/deploy/ci-state`.
