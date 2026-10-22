# Console roles and permissions

The console session endpoint returns the signed-in user's role and the permissions for that role. The console UI
shows management controls from this permission list, and the API checks the same role-to-permission map in
`aiflow.v4.control.console_auth.ROLE_PERMISSIONS` before accepting protected actions.

| Role | Console access |
|---|---|
| `super_admin` | Every permission (`*`): GitHub and Jira integrations, project add/pause/resume/remove, node installation/repair/join actions, and Knowledge resource edits and policy approvals. |
| `owner` | Explicit `console.manage`, `integrations.manage`, `projects.manage`, `nodes.manage`, and Knowledge management grants, retained for existing signed sessions. |
| `lead` | `console.read`, node operation history, and Knowledge resource/history read grants. Management controls are hidden and management API requests are refused. |

The `/v1/console/session` response includes a sorted `permissions` array. The UI checks those permissions rather
than inferring access from a role name, so adding or changing a role's grants only requires updating
`ROLE_PERMISSIONS`. API authorization remains authoritative even when a caller sends a request without using the
console UI. The existing owner bearer-token path remains available for policy approvals and Jira integration
actions where configured.
