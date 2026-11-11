"""Persistence of the provider sign-in link of a managed AI account."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import timedelta
from typing import Any, Iterable, Iterator

SIGN_IN_LINK_LIFETIME = timedelta(minutes=15)


def active_sign_in_link(db: Any, account_id: str) -> dict | None:
    """The sign-in request of an account that is not idle, with the link and its expiry."""
    return db.one("SELECT node,provider,sign_in_status,sign_in_url,sign_in_code,sign_in_error,"
                  "sign_in_expires_at FROM managed_ai_accounts"
                  " WHERE id=%s AND removed_at IS NULL AND sign_in_status<>'idle'", (account_id,))


def sign_in_request(db: Any, account_id: str) -> dict | None:
    """The node and request an account sign-in is assigned to."""
    return db.one("SELECT node,sign_in_request_id,sign_in_status,sign_in_url,sign_in_expires_at FROM managed_ai_accounts"
                  " WHERE id=%s AND removed_at IS NULL", (account_id,))


def apply_sign_in_status(connection: Any, account_id: str, node: str, request_id: str, status: str,
                         url: str, code: str, error: str) -> bool:
    """Store a node's status report. A waiting report is refused when the stored link has expired; a new or legacy link
    (no expiry yet) starts a fresh lifetime, so a repeated report never keeps a link without an expiry."""
    row = connection.execute(
        "UPDATE managed_ai_accounts SET sign_in_status=%s,"
        "sign_in_url=CASE WHEN %s<>'' THEN %s ELSE sign_in_url END,"
        "sign_in_code=CASE WHEN %s<>'' THEN %s ELSE sign_in_code END,"
        "sign_in_expires_at=CASE WHEN %s<>'' AND (%s<>sign_in_url OR sign_in_expires_at IS NULL)"
        " THEN now()+%s ELSE sign_in_expires_at END,"
        "sign_in_error=%s,updated_at=now() WHERE id=%s AND node=%s AND sign_in_request_id=%s"
        " AND removed_at IS NULL"
        " AND NOT (%s='waiting' AND (%s='' OR %s=sign_in_url) AND sign_in_url<>'' AND sign_in_expires_at IS NOT NULL"
        " AND sign_in_expires_at<=now()) RETURNING id",
        (status, url, url, code, code, url, url, SIGN_IN_LINK_LIFETIME, error[:300], account_id, node, request_id,
         status, url, url)).fetchone()
    return bool(row)


@contextmanager
def transaction(db: Any) -> Iterator[Any]:
    """Open the same connection and transaction boundary used by the former route helpers."""
    with db.pool.connection() as connection, connection.transaction():
        yield connection


def managed_shared_accounts(db: Any) -> list[dict]:
    return db.all("SELECT id,provider,tier,roles FROM shared_accounts WHERE managed")


def ensure_account_row(db: Any, row: dict) -> None:
    db.run("INSERT INTO accounts (id,provider,node,tier,roles,status)"
           " VALUES (%s,%s,'',%s,%s,'signed_out') ON CONFLICT (id) DO NOTHING",
           (row["id"], row["provider"], row["tier"], row["roles"] or []))


def create_shared_account(db: Any, body: Any, actor: str) -> None:
    db.run("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit, created_by)"
           " VALUES (%s,%s,%s,%s,%s,%s,%s)",
           (body.id, body.provider, body.tier, body.roles, body.explicit_only, body.session_limit, actor))


def lock_account_sessions(connection: Any, account_id: str) -> None:
    connection.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", ("account-sessions:" + account_id,))


def running_session_count(connection: Any, account_id: str) -> int:
    row = connection.execute("SELECT count(*) AS n FROM runs WHERE account_id=%s AND status='running'",
                             (account_id,)).fetchone()
    return int(row["n"])


def save_session_limit(connection: Any, account: Any, session_limit: int, actor: str) -> None:
    connection.execute("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit,"
                       " managed, created_by) VALUES (%s,%s,%s,%s,%s,%s,false,%s)"
                       " ON CONFLICT (id) DO UPDATE SET session_limit=EXCLUDED.session_limit",
                       (account.id, account.provider, account.tier, account.roles, account.explicit_only,
                        session_limit, actor))


def node_exists(db: Any, node: str) -> bool:
    return bool(db.one("SELECT 1 AS x FROM nodes WHERE name=%s", (node,)))


def ensure_shared_account(db: Any, account: Any, actor: str) -> None:
    db.run("INSERT INTO shared_accounts (id, provider, tier, roles, explicit_only, session_limit, managed,"
           " created_by) VALUES (%s,%s,%s,%s,%s,%s,false,%s) ON CONFLICT (id) DO NOTHING",
           (account.id, account.provider, account.tier, account.roles, account.explicit_only,
            account.sessions, actor))


def enable_machine(connection: Any, account_id: str, node: str, actor: str) -> None:
    connection.execute("DELETE FROM account_machine_exclusions WHERE account_id=%s AND node=%s",
                       (account_id, node))
    connection.execute("INSERT INTO account_machines (account_id, node, enabled_by) VALUES (%s,%s,%s)",
                       (account_id, node, actor))


def retry_signin(db: Any, account_id: str, node: str) -> bool:
    return bool(db.run("UPDATE account_machines SET signin_attempt=signin_attempt + 1, signin_link='', signin_code='',"
                       " signin_expires_at=NULL WHERE account_id=%s AND node=%s AND status='pending'",
                       (account_id, node)))


def managed_account_exists(db: Any, account_id: str) -> bool:
    return bool(db.one("SELECT 1 AS x FROM managed_ai_accounts WHERE id=%s AND removed_at IS NULL", (account_id,)))


def detach_machine(db: Any, account_id: str, node: str, actor: str) -> None:
    with transaction(db) as connection:
        lock_account_sessions(connection, account_id)
        connection.execute("DELETE FROM account_machines WHERE account_id=%s AND node=%s", (account_id, node))
        connection.execute("INSERT INTO account_machine_exclusions (account_id,node,detached_by) VALUES (%s,%s,%s)"
                           " ON CONFLICT (account_id,node) DO UPDATE SET detached_by=EXCLUDED.detached_by,"
                           " detached_at=now()", (account_id, node, actor))


def delete_managed_account(db: Any, account_id: str) -> bool:
    with transaction(db) as connection:
        lock_account_sessions(connection, account_id)
        row = connection.execute("SELECT managed FROM shared_accounts WHERE id=%s FOR UPDATE",
                                 (account_id,)).fetchone()
        if not row or not row["managed"]:
            return False
        connection.execute("DELETE FROM account_machines WHERE account_id=%s", (account_id,))
        connection.execute("DELETE FROM account_machine_exclusions WHERE account_id=%s", (account_id,))
        connection.execute("DELETE FROM shared_accounts WHERE id=%s", (account_id,))
        connection.execute("DELETE FROM accounts WHERE id=%s", (account_id,))
    return True


def shared_account_managed(db: Any, account_id: str) -> dict | None:
    return db.one("SELECT managed FROM shared_accounts WHERE id=%s", (account_id,))


def save_signin_prompt(db: Any, account_id: str, node: str, link: str, code: str, expires_in_s: int) -> bool:
    return bool(db.run("UPDATE account_machines SET signin_link=%s, signin_code=%s,"
                       " signin_expires_at=now() + make_interval(secs => %s)"
                       " WHERE account_id=%s AND node=%s AND status='pending'",
                       (link, code, expires_in_s, account_id, node)))


def machine_signin(db: Any, account_id: str, node: str) -> dict | None:
    return db.one("SELECT status,signin_attempt FROM account_machines WHERE account_id=%s AND node=%s",
                  (account_id, node))


def complete_pending_signin(db: Any, account_id: str, node: str, attempt: int) -> bool:
    return bool(db.run("UPDATE account_machines SET status='signed_in', signed_in_at=now(), signin_link='',"
                       " signin_code='', signin_expires_at=NULL WHERE account_id=%s AND node=%s"
                       " AND status='pending' AND signin_attempt=%s", (account_id, node, attempt)))


def finish_signin(db: Any, account_id: str) -> None:
    db.run("UPDATE accounts SET last_probe_at=now(),status=CASE WHEN status='signed_out' THEN 'idle'"
           " ELSE status END,last_error=CASE WHEN status='signed_out' THEN '' ELSE last_error END,"
           "updated_at=now() WHERE id=%s", (account_id,))


def node_names(db: Any) -> list[dict]:
    return db.all("SELECT name FROM nodes")


class NodeRepository:
    """Named database queries for registered fleet nodes."""

    def __init__(self, db: Any) -> None:
        """Bind this repository to the control database."""
        self.db = db

    def active_node_exists(self, name: str) -> bool:
        """Return whether this exact name identifies a node that has not been removed."""
        return bool(self.db.one("SELECT name FROM nodes WHERE name=%s AND removed_at IS NULL", (name,)))


class FakeNodeRepository:
    """In-memory active-name lookup for contexts whose callers supply registered node names."""

    def __init__(self, active_names: Iterable[str] = ()) -> None:
        """Capture the node names this fake considers active."""
        self._active_names = frozenset(active_names)
        self.calls: list[str] = []

    def active_node_exists(self, name: str) -> bool:
        """Return whether this exact name was supplied as active."""
        self.calls.append(name)
        return name in self._active_names
