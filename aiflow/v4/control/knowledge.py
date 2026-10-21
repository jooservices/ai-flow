"""Versioned knowledge, study case, skill, agent, guide and policy resources."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

KINDS = ("study_case", "knowledge", "skill", "agent", "guide", "policy")
FOLDERS = {"study_case": "study-cases", "knowledge": "knowledge", "skill": "skills",
           "agent": "agents", "guide": "guides"}
ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,78}[a-z0-9])?$")
PROJECT_RE = re.compile(r"^[A-Z][A-Z0-9_-]{0,31}$")


class Invalid(ValueError):
    """A resource has an invalid type, identity or body."""


def validate(kind: str, resource_id: str, title: str, content: str, actor: str) -> tuple[str, str, str, str, str]:
    kind = kind.strip()
    resource_id = resource_id.strip()
    title, actor = title.strip(), actor.strip()
    if kind not in KINDS:
        raise Invalid("unknown resource type")
    if kind == "policy":
        if resource_id != "default" and not PROJECT_RE.fullmatch(resource_id):
            raise Invalid("policy id must be 'default' or an uppercase project key")
        try:
            parsed = yaml.safe_load(content)
        except yaml.YAMLError as e:
            raise Invalid("policy content must be valid YAML") from e
        if not isinstance(parsed, dict):
            raise Invalid("policy content must be a YAML mapping")
    elif not ID_RE.fullmatch(resource_id):
        raise Invalid("id must be a lowercase slug")
    if not title or len(title) > 200:
        raise Invalid("title is required and must be at most 200 characters")
    if not content.strip() or len(content) > 100_000:
        raise Invalid("content is required and must be at most 100000 characters")
    if not actor or len(actor) > 200:
        raise Invalid("editor name is required and must be at most 200 characters")
    return kind, resource_id, title, content, actor


def path_for(kind: str, resource_id: str) -> str:
    if kind == "policy":
        return "policy/default.yaml" if resource_id == "default" else f"policy/projects/{resource_id}.yaml"
    if kind == "skill":
        return f"skills/{resource_id}/SKILL.md"
    return f"{FOLDERS[kind]}/{resource_id}.md"


def _frontmatter(title: str, resource_id: str, archived: bool, kind: str) -> str:
    meta: dict[str, Any] = {"id": resource_id, "title": title, "archived": archived}
    if kind == "skill":
        meta = {"name": resource_id, "description": title, "archived": archived}
    return "---\n" + yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).rstrip() + "\n---\n"


def file_content(kind: str, resource_id: str, title: str, content: str, archived: bool) -> str:
    if kind == "policy":
        header = f"# aiflow4-title: {title.replace(chr(10), ' ')[:200]}\n"
        if archived:
            header += "# aiflow4-archived: true\n"
        return header + content.rstrip() + "\n"
    return _frontmatter(title, resource_id, archived, kind) + content.rstrip() + "\n"


def _parse_markdown(raw: str, resource_id: str, kind: str) -> tuple[str, str, bool]:
    title = resource_id.replace("-", " ").replace("_", " ").title()
    archived = False
    content = raw
    if raw.startswith("---\n"):
        _marker, rest = raw.split("\n---\n", 1) if "\n---\n" in raw else ("", raw)
        if rest != raw:
            try:
                meta = yaml.safe_load(_marker[4:]) or {}
            except yaml.YAMLError:
                meta = {}
            title = str(meta.get("title") or meta.get("description") or title)
            archived = bool(meta.get("archived", False))
            content = rest
    return title, content.lstrip("\n").rstrip(), archived


def import_local(db, root: str | Path) -> None:
    """Index existing managed files once so the UI starts with the checked-out control repo contents."""
    root = Path(root)
    files: list[tuple[str, str, str]] = []
    default = root / "policy/default.yaml"
    if default.is_file():
        files.append(("policy", "default", str(default.relative_to(root))))
    for p in sorted((root / "policy/projects").glob("*.yaml")) if (root / "policy/projects").is_dir() else []:
        files.append(("policy", p.stem, str(p.relative_to(root))))
    patterns = (("study_case", "study-cases/*.md"), ("knowledge", "knowledge/*.md"),
                ("skill", "skills/*/SKILL.md"), ("agent", "agents/*.md"), ("guide", "guides/*.md"))
    for kind, pattern in patterns:
        for p in sorted(root.glob(pattern)):
            rid = p.parent.name if kind == "skill" else p.stem
            files.append((kind, rid, str(p.relative_to(root))))
    for kind, rid, rel in files:
        if db.one("SELECT 1 FROM managed_resources WHERE resource_type=%s AND resource_id=%s", (kind, rid)):
            continue
        path = root / rel
        raw = path.read_text(encoding="utf-8")
        if kind == "policy":
            archived = "# aiflow4-archived: true" in raw.splitlines()[:3]
            content = "\n".join(line for line in raw.splitlines() if not line.startswith("# aiflow4-"))
            try:
                title_line = next((line for line in raw.splitlines() if line.startswith("# aiflow4-title: ")), "")
                title = title_line.partition(": ")[2] or ("Default policy" if rid == "default" else f"{rid} policy")
            except StopIteration:
                title = "Default policy" if rid == "default" else f"{rid} policy"
            content = content.strip()
        else:
            title, content, archived = _parse_markdown(raw, rid, kind)
        db.run("INSERT INTO managed_resources (resource_type,resource_id,title,content,archived,path,version,updated_by)"
               " VALUES (%s,%s,%s,%s,%s,%s,1,'aiflow-control') ON CONFLICT DO NOTHING",
               (kind, rid, title, content, archived, rel))
        db.run("INSERT INTO managed_resource_history (resource_type,resource_id,version,title,content,archived,actor,action)"
               " VALUES (%s,%s,1,%s,%s,%s,'aiflow-control','import') ON CONFLICT DO NOTHING",
               (kind, rid, title, content, archived))


def list_resources(db, include_archived: bool = True) -> list[dict]:
    where = "" if include_archived else " WHERE archived=false"
    return db.all("SELECT resource_type AS type,resource_id AS id,title,content,archived,path,version,updated_by,"
                  "updated_at,commit_sha FROM managed_resources" + where + " ORDER BY resource_type,resource_id")


def get_resource(db, kind: str, resource_id: str) -> dict | None:
    return db.one("SELECT resource_type AS type,resource_id AS id,title,content,archived,path,version,updated_by,"
                  "updated_at,commit_sha FROM managed_resources WHERE resource_type=%s AND resource_id=%s",
                  (kind, resource_id))


def history(db, kind: str, resource_id: str) -> list[dict]:
    return db.all("SELECT version,title,content,archived,actor,action,created_at,commit_sha,pr_number,pr_url,approved_by"
                  " FROM managed_resource_history WHERE resource_type=%s AND resource_id=%s ORDER BY version DESC",
                  (kind, resource_id))


def proposals(db, kind: str = "", resource_id: str = "") -> list[dict]:
    filters = " AND resource_type=%s AND resource_id=%s" if kind and resource_id else ""
    params = (kind, resource_id) if filters else ()
    return db.all("SELECT id AS proposal_id,resource_type AS type,resource_id AS id,title,content,archived,actor,action,branch,"
                  "pr_number,pr_url,status,created_at,base_title,base_content,base_archived,base_version "
                  "FROM managed_resource_proposals WHERE status='pending'" + filters + " ORDER BY created_at DESC", params)


def save_current(db, *, kind: str, resource_id: str, title: str, content: str, archived: bool, actor: str,
                 action: str, path: str, commit_sha: str = "", approved_by: str = "") -> dict:
    previous = get_resource(db, kind, resource_id)
    version = int(previous["version"]) + 1 if previous else 1
    db.run("INSERT INTO managed_resources (resource_type,resource_id,title,content,archived,path,version,updated_by,commit_sha)"
           " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (resource_type,resource_id) DO UPDATE SET"
           " title=EXCLUDED.title,content=EXCLUDED.content,archived=EXCLUDED.archived,path=EXCLUDED.path,"
           " version=EXCLUDED.version,updated_by=EXCLUDED.updated_by,updated_at=now(),commit_sha=EXCLUDED.commit_sha",
           (kind, resource_id, title, content, archived, path, version, actor, commit_sha))
    db.run("INSERT INTO managed_resource_history (resource_type,resource_id,version,title,content,archived,actor,action,"
           "commit_sha,approved_by) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
           (kind, resource_id, version, title, content, archived, actor, action, commit_sha, approved_by))
    return get_resource(db, kind, resource_id) or {}
