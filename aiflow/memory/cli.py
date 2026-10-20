"""`bin/aiflow-mem`: search, read and propose memory from a terminal (an AI inside a ticket, or a person).

Environment: AIFLOW_MEMORY_URL, AIFLOW_MEMORY_TOKEN (the supervisor sets them for an AI; a client token cannot verify, promote or decay),
AIFLOW_MEMORY_NAMESPACE and AIFLOW_MEMORY_PROJECT as defaults."""
from __future__ import annotations

import argparse
import json
import os
import sys

from .client import Memory, Unavailable


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="aiflow-mem", description="AI memory: search before you work, propose what you learned.")
    sub = parser.add_subparsers(dest="command", required=True)
    s = sub.add_parser("search", help="find entries (verified and promoted)")
    s.add_argument("query", nargs="?", default="")
    s.add_argument("--kind", action="append", help="fact, case, lesson or context (repeatable)")
    s.add_argument("--limit", type=int, default=6)
    for name in ("search", "add"):
        p = s if name == "search" else sub.add_parser("add", help="propose an entry (stored as fresh until it is confirmed)")
        p.add_argument("--namespace", default=os.environ.get("AIFLOW_MEMORY_NAMESPACE", ""))
        p.add_argument("--project", default=os.environ.get("AIFLOW_MEMORY_PROJECT", ""))
        p.add_argument("--repo", default=os.environ.get("AIFLOW_MEMORY_REPO", ""))
    a = sub.choices["add"]
    a.add_argument("--kind", required=True, choices=["fact", "case", "lesson", "context"])
    a.add_argument("--scope", default="project", choices=["global", "namespace", "project", "ticket"])
    a.add_argument("--title", required=True)
    a.add_argument("--body", required=True)
    a.add_argument("--tags", default="")
    a.add_argument("--ticket", default=os.environ.get("AIFLOW_MEMORY_TICKET", ""))
    g = sub.add_parser("get", help="read one entry")
    g.add_argument("id")
    f = sub.add_parser("feedback", help="say whether an entry helped")
    f.add_argument("id")
    f.add_argument("--helpful", action="store_true")
    f.add_argument("--unhelpful", action="store_true")
    f.add_argument("--ticket", default=os.environ.get("AIFLOW_MEMORY_TICKET", ""))
    for name in ("verify", "promote", "decay"):
        p = sub.add_parser(name, help=f"{name} an entry (admin token)")
        p.add_argument("id")
        if name == "decay":
            p.add_argument("--reason", default="decayed by an admin")
    sub.add_parser("curate", help="run the curator (admin token)")
    sub.add_parser("stats", help="how many entries of each kind and status")
    sub.add_parser("flush", help="send the entries kept while the service was down")
    args = parser.parse_args(argv)
    memory = Memory(os.environ.get("AIFLOW_MEMORY_URL", ""), os.environ.get("AIFLOW_MEMORY_TOKEN", ""))
    try:
        if args.command == "search":
            for item in memory.call("POST", "/v1/search", dict(query=args.query, namespace=args.namespace, project=args.project, repo=args.repo,
                                                               kinds=args.kind, limit=args.limit, touch=False))["items"]:
                print(f"[{item['kind']}/{item['status']}] {item['title']}  (id {item['id']})\n    {' '.join(item['body'].split())[:300]}")
        elif args.command == "add":
            entry = dict(kind=args.kind, scope=args.scope, namespace=args.namespace, project=args.project, repo=args.repo, jira_key=args.ticket,
                         title=args.title, body=args.body, tags=[t for t in args.tags.split(",") if t], created_by=os.environ.get("AIFLOW_MEMORY_ACTOR", "cli"))
            print(json.dumps(memory.call("POST", "/v1/entries", entry)))
        elif args.command == "get":
            print(json.dumps(memory.get(args.id), indent=1))
        elif args.command == "feedback":
            print(json.dumps(memory.call("POST", f"/v1/entries/{args.id}/feedback", dict(jira_key=args.ticket, helpful=not args.unhelpful))))
        elif args.command in ("verify", "promote"):
            print(json.dumps(memory.call("POST", f"/v1/entries/{args.id}/{args.command}", {})))
        elif args.command == "decay":
            print(json.dumps(memory.call("POST", f"/v1/entries/{args.id}/decay", dict(reason=args.reason))))
        elif args.command == "curate":
            print(json.dumps(memory.call("POST", "/v1/curate", {})))
        elif args.command == "stats":
            print(json.dumps(memory.call("GET", "/v1/stats"), indent=1))
        else:
            print(f"{memory.flush()} entries sent")
    except Unavailable as error:
        print(f"memory service not available: {error}", file=sys.stderr)
        return 3
    except ValueError as error:
        print(f"refused: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
