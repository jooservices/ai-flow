"""SQL for the watchdog checks.

The statements are the ones the checks already ran. The check modules call them
in the same order, with the same parameters.
"""

NODES_HEARTBEAT = "SELECT name, status, last_heartbeat FROM nodes"
NODE_MARK_OFFLINE = "UPDATE nodes SET status='offline' WHERE name=%s"

NODES_TOOLS = "SELECT name, status, tools, projects, dynamic FROM nodes"
OPEN_NODE_TOOL_INCIDENTS = "SELECT subject FROM incidents WHERE kind='node_tools' AND closed_at IS NULL"

ONLINE_NODES_FOR_SERVICES = "SELECT name, status, metrics, projects, dynamic FROM nodes WHERE status='online'"
OPEN_NODE_SERVICE_INCIDENTS = "SELECT subject FROM incidents WHERE kind='node_services' AND closed_at IS NULL"

NODES_NAME_STATUS = "SELECT name, status FROM nodes"

ORPHAN_TICKETS = (
    "SELECT t.key FROM tickets t WHERE t.status = 'In Progress'"
    " AND lower(btrim(t.type)) <> 'epic'"
    " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = t.key AND r.status = 'running')"
    " AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key = t.key AND r.started_at > now() - interval '10 minutes')"
    " AND t.synced_at > now() - interval '5 minutes'"
)
TICKET_STATUS_TO_DO = "UPDATE tickets SET status='To Do' WHERE key=%s"
ORPHAN_AUDIT = (
    "INSERT INTO write_audit (target, action, ticket, actor, accepted, result) VALUES"
    " ('jira','transition',%s,'control',true,'To Do (orphan)')"
)

STUCK_STREAK_SELECT = "SELECT ticket_key, fails FROM streak"
LATEST_RUN_SUMMARY = "SELECT account_id, summary FROM runs WHERE ticket_key=%s ORDER BY started_at DESC LIMIT 1"
OPEN_STUCK_INCIDENTS = "SELECT subject FROM incidents WHERE kind='ticket_stuck' AND closed_at IS NULL"

RUNNING_RUNS = "SELECT * FROM runs WHERE status='running'"
ACCOUNT_IDLE_IF_NO_OTHER_RUN = (
    "UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s AND status='working'"
    " AND NOT EXISTS (SELECT 1 FROM runs WHERE account_id=%s AND status='running' AND id<>%s)"
)
LEASE_LOST_AUDIT = (
    "INSERT INTO write_audit (target, action, ticket, run_id, fence, actor, accepted, result)"
    " VALUES ('jira','transition',%s,%s,%s,'control',true,'To Do (lease lost)')"
)
OPEN_RUN_CLOCK_INCIDENTS = "SELECT subject FROM incidents WHERE kind=%s AND closed_at IS NULL"
RUNNING_TICKET = "SELECT 1 FROM runs WHERE ticket_key=%s AND status='running'"

COOLDOWN_ACCOUNTS = "SELECT id, status, cooldown_until FROM accounts WHERE status='cooldown'"
ACCOUNT_MARK_IDLE = "UPDATE accounts SET status='idle', updated_at=now() WHERE id=%s"
PROBLEM_ACCOUNTS = (
    "SELECT id, provider, status, cooldown_until, last_error FROM accounts"
    " WHERE status IN ('cooldown','signed_out','error')"
)
WORKING_ACCOUNTS = "SELECT id FROM accounts WHERE status IN ('idle','working')"

ONLINE_NODE_NAMES = "SELECT name FROM nodes WHERE status = 'online'"
READY_ACCOUNT_IDS = "SELECT id FROM accounts WHERE status IN ('idle','working','probing')"

QUOTA_WINDOW_ACCOUNTS = "SELECT id,provider,quota FROM accounts WHERE quota_source<>'' AND quota ? 'windows'"

QUOTA_FORECAST_ACCOUNTS = "SELECT id,provider,quota,quota_source,quota_observed_at FROM accounts ORDER BY id"
SAVE_QUOTA_FORECAST = "UPDATE accounts SET quota_at_risk=%s,quota_forecast=%s WHERE id=%s"
