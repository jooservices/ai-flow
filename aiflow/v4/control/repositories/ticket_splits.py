"""Persistence for ticket split plans and their local Jira ticket mirrors."""
from __future__ import annotations

from typing import Any

from ..db import J


def ticket(connection, key: str) -> dict | None:
    return connection.execute(
        "SELECT key,project,status,type,priority,labels,summary,description,fields FROM tickets WHERE key=%s",
        (key,)).fetchone()


def plan(connection, key: str) -> dict | None:
    return connection.execute("SELECT plan,estimate,child_keys,status FROM ticket_split_plans WHERE parent_key=%s",
                              (key,)).fetchone()


def save_plan(connection, parent: dict, estimate: dict, target_minutes: int, plan_data: list[dict]) -> dict:
    connection.execute("INSERT INTO ticket_split_plans (parent_key,project,estimate,target_minutes,plan)"
                       " VALUES (%s,%s,%s,%s,%s) ON CONFLICT (parent_key) DO NOTHING",
                       (parent["key"], parent["project"], J(estimate), target_minutes, J(plan_data)))
    return plan(connection, parent["key"]) or {}


def save_child(connection, key: str, parent: dict, marker: str, item: dict, blocked_by: list[str]) -> None:
    connection.execute("INSERT INTO tickets (key,project,status,type,priority,labels,summary,description,blocked_by,synced_at)"
                       " VALUES (%s,%s,'To Do','Story',%s,%s,%s,%s,%s,now()) ON CONFLICT (key) DO UPDATE SET"
                       " status='To Do',type='Story',labels=EXCLUDED.labels,summary=EXCLUDED.summary,"
                       " description=EXCLUDED.description,blocked_by=EXCLUDED.blocked_by,synced_at=now()",
                       (key, parent["project"], parent["priority"], [marker], item["summary"],
                        item["description"], blocked_by))
    connection.execute("UPDATE ticket_split_plans SET child_keys=array_append(child_keys,%s),updated_at=now()"
                       " WHERE parent_key=%s AND NOT (%s=ANY(child_keys))", (key, parent["key"], key))


def offer_child(connection, key: str, status: str) -> None:
    connection.execute("UPDATE tickets SET status=%s,labels=CASE WHEN 'aiflow-ready'=ANY(labels) THEN labels"
                       " ELSE array_append(labels,'aiflow-ready') END,synced_at=now() WHERE key=%s", (status, key))


def complete(connection, parent: dict, children: list[str], parent_status: str, parent_label: str) -> None:
    connection.execute("UPDATE tickets SET status=%s,labels=array_remove(array_append(labels,%s),'aiflow-ready'),"
                        "synced_at=now() WHERE key=%s", (parent_status, parent_label, parent["key"]))
    connection.execute("UPDATE ticket_split_plans SET child_keys=%s,status='complete',updated_at=now()"
                       " WHERE parent_key=%s", (children, parent["key"]))
