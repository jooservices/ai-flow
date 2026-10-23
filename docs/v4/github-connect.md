# Connect GitHub from the console

Owners connect GitHub from **Integrations** in the v4 console. The GitHub App is the control plane's only GitHub
credential: workers still ask the control API for a short lived installation token scoped to their project repo,
and the control API continues to fence and audit GitHub writes.

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
