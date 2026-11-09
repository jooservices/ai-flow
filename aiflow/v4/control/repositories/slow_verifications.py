"""Read tickets waiting in verification longer than a threshold (JAIF-444 / JAIF-271 AC7)."""
from __future__ import annotations

WAITING_SQL = """
SELECT t.key, t.project, t.summary, coalesce(d.ended_at, t.jira_updated, t.synced_at) AS since
FROM tickets t LEFT JOIN LATERAL (SELECT ended_at FROM runs WHERE ticket_key=t.key AND step='dev'
  AND outcome='done' ORDER BY started_at DESC LIMIT 1) d ON true
WHERE t.status='In Verify' AND lower(coalesce(t.type,'')) <> 'epic'
  AND NOT ('aiflow-hold' = ANY(t.labels))
  AND NOT EXISTS (SELECT 1 FROM runs r WHERE r.ticket_key=t.key AND r.status='running')
  AND coalesce(d.ended_at, t.jira_updated, t.synced_at)
      < coalesce(%s::timestamptz, now()) - %s * interval '1 minute'
ORDER BY since
"""


def waiting_verifications(db, minutes: int, now=None) -> list[dict]:
    """Tickets in verification with no running work for longer than `minutes`."""
    return db.all(WAITING_SQL, (now, minutes))


def database_now(db):
    """The database clock, the reference of the waiting time."""
    return db.one("SELECT now() AS t")["t"]
