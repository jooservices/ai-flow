# First-run setup

A new v4 control plane with no console user opens the setup wizard at `/login`. The first screen asks for the public
address and creates the first Administrator with an email and password. The browser signs in that Administrator
immediately. The password is stored only as a salted scrypt hash, and the public address is stored in PostgreSQL so
both API replicas use it for OAuth callbacks and generated links.

## Guided setup

After the Administrator is created, the wizard checks these steps:

1. Connect Jira in Integrations using OAuth or an API token.
2. Connect GitHub in Integrations by creating an App from GitHub's manifest flow or importing an existing App.
3. Add a machine in Machines and AI. The screen creates a one-use command that expires after five minutes. Run that
   command on the new machine; it is the only setup step that needs a terminal.
4. Add an AI account to the machine in Machines and AI. Its provider sign-in URL and code appear on that screen.
5. Add the first Jira project and its repositories in Projects.
6. Send a sample ticket to Intake.

The first machine is assigned to a project, so the project can be configured before the machine is joined even though
the wizard presents the machine step first. Every check reports what is ready or names the missing action in plain
words. Jira, GitHub, machine, account and project actions remain available in Integrations, Machines and AI, and
Projects after setup.

Any post-administrator step can be skipped. The wizard records the skip and leaves a setup panel on the Dashboard;
the owner can resume there later. The sample ticket step creates a Jira issue, moves it to Intake and confirms that
status. If Jira cannot create or confirm the issue, the screen says what failed and how to retry. A successfully
confirmed sample ticket completes and locks the wizard. The ticket's later pipeline progress is followed on the
normal ticket and Dashboard screens; setup does not wait for Done.

## Bootstrap and access

- `GET /login` serves the wizard while there is no console user, then serves the regular sign-in form.
- `GET /setup` resumes setup until a sample ticket is confirmed, then redirects to Dashboard.
- `GET /v1/setup/status` checks each step and returns its plain-language missing action.
- `POST /v1/setup/bootstrap` accepts the public address, Administrator email and password once. A short-lived
  same-site setup nonce protects this first-use request.
- `POST /v1/setup/steps/{step}` records a skip or resume for an optional step.
- `POST /v1/setup/sample-ticket` creates the sample and only completes setup after Jira confirms Intake.

An optional legacy installer `--admin-email` can still seed an Administrator for controlled CI or migration
workflows. A fresh install needs only `aiflow4-control-install --version vX.Y.Z --domain D`, then browser setup.

Tests: `tests/v4/test_setup_wizard.py`, `tests/v4/test_control_install.py`, and
`tests/v4/test_clean_install_acceptance.py`.
