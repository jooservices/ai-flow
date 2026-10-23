# Security

**Assumes:** you are signed in with console login on and are using the installed v4 release. Settings and the user
list need the Super Administrator role; everyone can manage their own two-factor sign-in.

Security (`/security`) holds two-factor sign-in, session rules and the audit log.

## What you can do here

- Set up two-factor sign-in with an authenticator app and receive recovery codes. Each recovery code works once.
  Store them somewhere safe; they are shown only when issued.
- Set how long a session lasts.
- See active sessions and sign one out. A forced sign-out ends that session and its next request is rejected.
- Read the audit log of changes to settings, people, roles and credentials. The audit log is read-only: it cannot
  be edited or deleted from the console.

See the [administrator security guide](../admin/security.md) for the rest of the hardening measures.
