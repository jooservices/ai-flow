# System check

**Assumes:** You are signed in to the AI Flow v4 console as an Owner or Super Administrator.

Open **System check** from the console menu. The page lists every condition AI Flow needs, per machine and per
project: tools and versions, test services, permissions, network access, disk space, credentials and repository
settings. Each condition shows one result:

- **Ready**: nothing to do.
- **Fixed automatically**: the machine repaired it by itself (installed a tool, started a service or freed space).
- **Needs an action**: follow the one-sentence action shown next to the condition.
- **Not possible on this machine**: the machine cannot meet it; the project is not started there.

Select **Run now** to repeat the check. It also runs after a machine joins, after a project is added, after an
upgrade and every hour. The Projects screen names the failed condition when a project is held back on a machine.
Credential conditions only say present or missing, valid or invalid; no secret is shown. The result is included in
the support bundle.
