# Licence

**Assumes:** You are signed in to an installed AI Flow v4 control plane as an Administrator, the
roles that hold `licensing.manage`, and an administrator has configured the licence issuer.

Open **Licence** from the console menu. The page shows the installation ID, the current plan, the expiry date, the
resource limits and how much of each limit is used. It also holds the usage report.

## Activate or renew

- **Online:** paste your licence key and select **Activate**. The key is sent only to the licence service and is not
  saved. The field is cleared after you submit.
- **Offline (closed network):** choose the signed licence file and select the offline activation button. The
  signature is checked on the control machine and nothing is sent over the network.

A wrong key or a file with a bad signature is rejected with a message and changes nothing.

## Plan limits

Each plan sets limits for machines, AI accounts, projects and users. You can edit the values for a plan and save
them. When a new machine, AI account, project or user would exceed a limit, the console names the limit and tells
you what to do. Work that is already running is never stopped by a limit.

## Expired or invalid licence

A warning banner shows on every screen for a 14-day grace period; AI Flow keeps working. After that it becomes
read-only. No data is deleted in any state. In read-only mode this screen still works, and a valid key restores
normal operation.

## Licence notice

The notice at the top of this screen (no licence required, or usage above a plan limit with its 14-day grace
period) is drawn by a small script that the console loads next to the page. Its text and behaviour are unchanged.

## Usage report

The report lists tickets finished, AI tokens and cost per project and month. Select **Export spreadsheet** to
download it as a CSV file.

Administrators can find the issuer settings in `docs/v4/licensing.md`.
