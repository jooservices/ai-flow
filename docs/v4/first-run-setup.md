# First-run setup

**Console screens:** `cc`

A new v4 control plane with no console user opens the setup wizard at `/login`. The first screen asks for the public
address, installation ownership capability, Administrator email and password. The browser signs in that
Administrator immediately. The capability is the install-scoped `AIFLOW4_SETUP_OWNERSHIP_CAPABILITY`; on an
interactive install, choose it during the install command's hidden prompt and enter the same value in the browser.
It is verified and consumed atomically with account creation, and is never returned by setup status. The password is stored only as a salted
scrypt hash, and the public address is stored in PostgreSQL so both API replicas use it for OAuth callbacks and
generated links.

## Guided setup

After the Administrator is created, the wizard checks these steps:

1. Connect Jira in Integrations using OAuth or an API token.
2. Connect GitHub in Integrations by creating an App from GitHub's manifest flow or importing an existing App.
3. Add the first Jira project and its repositories in Projects.
4. Add a machine in Machines and AI. The screen creates a one-use command that expires after five minutes. Run that
   command on the new machine; it is the only setup step that needs a terminal.
5. Add an AI account to the machine in Machines and AI. Its provider sign-in URL and code appear on that screen.
6. Send a sample ticket to Intake.

The join command needs a project to choose, so the project comes before the machine. While no project exists, the
machine step says so and its action opens Projects. Every check reports what is ready or names the missing action in plain
words. Jira, GitHub, machine, account and project actions remain available in Integrations, Machines and AI, and
Projects after setup.

The setup screen (`/setup`, or `/login` before any user exists) creates the Administrator. It is locked from that
moment: `/setup` redirects to the Dashboard. Every later step is shown in the
"Finish setting up AI Flow" panel on the Dashboard (`ui/setup-progress.js`), with its check, its missing action, a link
to Integrations, Machines and AI, or Projects, and a Skip or Resume button. The sample ticket step creates a Jira
issue, moves it to Intake and confirms that status. If Jira cannot create or confirm the issue, the panel says what
failed and how to retry. Sending the sample does not close setup while other steps are skipped or unfinished: the panel
stays, and those steps stay resumable, until every step is ready. The ticket's later pipeline progress is followed on
the normal ticket and Dashboard screens; setup does not wait for Done.

Jira's confirmation of Intake is stored (`sample_confirmed_at`), and the step is ready from that record, not from the
ticket's live status, so the Dashboard poll makes no Jira call and the flow can move the sample on freely. Once every
step has been ready, completion is permanent. A second send is refused with 409 and never moves the ticket again.
If Jira created the sample but did not confirm Intake, a retry keeps the project saved with that issue key even if a
different project is selected in the form.

## Bootstrap and access

- `GET /login` serves the wizard while there is no console user, then serves the regular sign-in form.
- `GET /setup` serves the bootstrap screen only while there is no Administrator, then redirects to the Dashboard.
- `GET /v1/setup/status` returns only the bootstrap token and lock state before an Administrator exists; after sign-in,
  it checks each step and returns its plain-language missing action.
- `POST /v1/setup/bootstrap` accepts the public address, Administrator email and password once. A short-lived
  same-site nonce protects against CSRF; the installation ownership capability authenticates the first owner and
  is atomically consumed in the transaction that creates the Administrator. Remotely, `/setup` and `/login` refuse
  to serve the form over plain HTTP, and the public address must be `https://`; plain `http://` is accepted only when
  the address itself is `localhost` or a loopback IP, whoever sends the request. Otherwise the session cookie is `Secure`.
  Remote setup submissions require HTTPS;
  direct loopback access is allowed for local development, and a TLS-terminating proxy must be listed in the trusted
  proxy settings.
- `POST /v1/setup/steps/{step}` records a skip or resume for an optional step.
- `POST /v1/setup/sample-ticket` creates the sample and confirms Intake in Jira; requests serialize across API
  replicas and re-read setup state before creating an issue. `completed` is true only when all steps are ready.

An optional legacy installer `--admin-email` can still seed an Administrator for controlled CI or migration
workflows. A fresh interactive install needs only `aiflow4-control-install --version vX.Y.Z --domain D`; choose
the hidden ownership capability during that command, then use it in browser setup.

Tests: `tests/v4/test_setup_wizard.py`, `tests/v4/test_control_install.py`, and
`tests/v4/test_clean_install_acceptance.py`.
