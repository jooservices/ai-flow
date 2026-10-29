"""AI accounts: `managed_ai_accounts`, `accounts`, `shared_accounts` and `account_machines` queries."""
from __future__ import annotations


def _all(queryable, sql: str, params: tuple = ()) -> list[dict]:
    if hasattr(queryable, "all"):
        return queryable.all(sql, params)
    return list(queryable.execute(sql, params).fetchall())


def removed_ids(queryable) -> set[str]:
    return {row["id"] for row in _all(queryable, "SELECT id FROM managed_ai_accounts WHERE removed_at IS NOT NULL")}


def active_managed(queryable) -> list[dict]:
    return _all(queryable, "SELECT * FROM managed_ai_accounts WHERE removed_at IS NULL ORDER BY created_at,id")


def import_account(conn, account) -> None:
    """Insert one control-repo account. A row the owner already removed or changed on screen is never overwritten."""
    conn.execute(
        "INSERT INTO managed_ai_accounts (id,provider,node,tier,roles,parallel,explicit_only,paused)"
        " VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (id) DO NOTHING",
        (account.id, account.provider, account.node, account.tier, list(account.roles),
         min(8, max(1, int(account.parallel or 1))), account.explicit_only, account.paused))
    conn.execute("INSERT INTO accounts (id,provider,node,tier,roles) VALUES (%s,%s,%s,%s,%s)"
                 " ON CONFLICT (id) DO NOTHING",
                 (account.id, account.provider, account.node, account.tier, list(account.roles)))
    if account.session_limit:
        conn.execute("INSERT INTO shared_accounts (id,provider,tier,roles,explicit_only,session_limit,"
                     "managed,created_by) VALUES (%s,%s,%s,%s,%s,%s,false,'import')"
                     " ON CONFLICT (id) DO NOTHING",
                     (account.id, account.provider, account.tier, list(account.roles),
                      account.explicit_only, min(50, int(account.session_limit))))
    for extra in account.extra_machines:
        conn.execute("INSERT INTO account_machines (account_id,node,status,enabled_by)"
                     " VALUES (%s,%s,'signed_in','import') ON CONFLICT DO NOTHING", (account.id, extra))
