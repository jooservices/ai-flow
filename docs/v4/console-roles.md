# Console roles and permissions

The console session endpoint returns the signed-in user's role and the permissions for that role. The console UI
shows management controls from this permission list, and the API checks the same role-to-permission map in
`aiflow.v4.control.console_auth.ROLE_PERMISSIONS` before accepting protected actions.

| Role | Console access |
|---|---|
| `super_admin` | Full management: GitHub and Jira integrations, project add/pause/resume/remove, node installation/repair/join actions, and Knowledge resource edits and policy approvals. |
| `owner` | The same management permissions as `super_admin`, retained for existing signed sessions. |
| `lead` | Read-only console access, node operation history, and Knowledge resources/history. Management controls are hidden and management API requests are refused. |

The `/v1/console/session` response includes a sorted `permissions` array. The UI checks those permissions rather
than inferring access from a role name, so adding or changing a role's grants only requires updating
`ROLE_PERMISSIONS`. API authorization remains authoritative even when a caller sends a request without using the
console UI. The existing owner bearer-token path remains available for policy approvals and Jira integration
actions where configured.
