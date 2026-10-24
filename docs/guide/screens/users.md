# Users and roles

**Assumes:** you are signed in as an Administrator. Other roles cannot open this screen.

Users and roles (`/users`) lists the people who can sign in and lets an Administrator manage them without a terminal or a file edit.

## What you can do here

- Add a person with an email, a name and one or more roles. You can limit the person to selected projects or leave
  access set to all projects. The form keeps its entered values while you change the roles or project access. A
  temporary password is shown once after the person is created.
- Give or remove roles. A person with several roles sees the union of the views of those roles.
- Limit a person to chosen projects, or leave the person on all projects.
- Disable or enable a person.
- Use **View as** in the top bar to see exactly the views of a selected role without signing out.

Every change is recorded with your name and shown in the audit list on this screen. The last administrator cannot be removed or disabled. See the role table in `docs/v4/role-based-views.md`.

See the [security guide](../admin/security.md) for the recommendations on sign-in and access.
