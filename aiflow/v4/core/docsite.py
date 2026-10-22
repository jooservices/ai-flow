"""Documentation site: build the guides in `docs/guide/` and the API reference into a static site, and check in CI
that a screen or API change comes with a documentation change.

    python3 -m aiflow.v4.core.docsite build --out build/docs-site [--version vX.Y.Z]
    python3 -m aiflow.v4.core.docsite check --base origin/develop

The site is plain HTML written with the standard library only (no site generator dependency). The release
packaging (`deploy/v4/ci/package-release.sh`) puts it into the release tarball as `docs-site/`; control serves it
at `/help/` and the console Help links open `/help/screens/<view>.html`. The API reference page is generated from
the FastAPI routes of `control/api.py` at build time and is never written by hand. The runbook set is checked
against the incident kinds the code raises (`incident_kinds()`).
"""
from __future__ import annotations

import argparse
import ast
import html
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
GUIDE = ROOT / "docs" / "guide"
CONTROL = ROOT / "aiflow" / "v4" / "control"
UI = CONTROL / "ui" / "console.html"
SITE_ENV = "AIFLOW4_DOCS_SITE"
API_PAGE = "reference/api.md"

# Console screens (`id="view-<name>"` in console.html) and the page that explains each one.
SCREEN_PAGE = "screens/{view}.md"

# JavaScript renderers run outside the static <section> ranges. Keep this map beside the check so a renderer edit
# requires the page for the screen it renders. `None` means the renderer is shared by every screen.
SCREEN_RENDERER_VIEWS: dict[str, tuple[str, ...] | None] = {
    "renderGitHubIntegrations": ("integrations",),
    "renderLive": None,
    "renderCC": ("cc",),
    "renderNodeJoin": ("cc",),
    "renderSA": ("sa",),
    "renderDHead": ("delivery",),
    "renderDBody": ("delivery",),
    "renderReviews": ("review",),
    "renderKnowledge": ("knowledge",),
    "renderJiraIntegrations": ("integrations",),
    "renderIntegrations": ("integrations",),
    "renderLead": ("lead",),
    "renderBHead": ("business",),
    "renderBBody": ("business",),
    "renderBForm": ("business",),
    "renderBCR": ("business",),
    "renderBusiness": ("business",),
    "renderStakeholderHead": ("stakeholder",),
    "renderStakeholderBody": ("stakeholder",),
    "renderStakeholder": ("stakeholder",),
    "renderDrawer": None,
    "renderFooter": None,
    "renderProjects": ("projects",),
    "render": None,
    "renderHelp": None,
}

# A change to one of these needs a documentation change in the same pull request.
SCREEN_PATHS = ("aiflow/v4/control/ui/",)
DOC_PATHS = ("docs/guide/", "docs/v4/")


# --- what the code raises ---------------------------------------------------------------------------------------
def incident_kinds(control: Path = CONTROL) -> set[str]:
    """Incident kinds in the code: the literal or module constant given to `open_incident(ctx, kind, ...)`."""
    kinds: set[str] = set()
    for path in sorted(control.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        consts: dict[str, str] = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                names = target.elts if isinstance(target, ast.Tuple) else [target]
                values = node.value.elts if isinstance(node.value, ast.Tuple) else [node.value]
                if len(names) == len(values):
                    consts.update({n.id: v.value for n, v in zip(names, values) if isinstance(n, ast.Name)
                                   and isinstance(v, ast.Constant) and isinstance(v.value, str)})
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "id", "") == "open_incident"
                    and len(node.args) >= 2):
                continue
            arg = node.args[1]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                kinds.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in consts:
                kinds.add(consts[arg.id])
    return kinds


def console_views(page: Path = UI) -> list[str]:
    """Screens of the console: the `view-*` sections of console.html."""
    return re.findall(r'<section class="view" id="view-([a-z]+)"', page.read_text(encoding="utf-8"))


def role_names() -> list[str]:
    from ..control.console_auth import ROLE_PERMISSIONS
    return sorted(ROLE_PERMISSIONS)


# --- API reference, generated from the FastAPI app --------------------------------------------------------------
def api_reference_markdown() -> str:
    """Markdown for every route of the control API, from the app's OpenAPI schema."""
    from unittest.mock import MagicMock

    from .. import VERSION
    from ..control.api import create_app

    schema = create_app(MagicMock()).openapi()
    out = ["# API reference", "",
           f"**Assumes:** you know HTTP and JSON, you have a role or a token that may call the endpoint, and you use "
           f"control API version {VERSION}. This page is generated from the code of `aiflow/v4/control/api.py` when "
           "the documentation site is built; do not edit it by hand.", "",
           "Console routes need a signed-in session when console login is on. Node, worker, reviewer and owner "
           "routes need their bearer token (see [Security](../admin/security.md)). Examples never contain a real "
           "token.", ""]
    groups: dict[str, list[tuple[str, str, dict]]] = {}
    for path, item in schema.get("paths", {}).items():
        for method, op in item.items():
            if method not in ("get", "post", "put", "patch", "delete"):
                continue
            parts = [p for p in path.split("/") if p]
            group = parts[1] if len(parts) > 1 and parts[0] == "v1" else (parts[0] if parts else "root")
            groups.setdefault(group, []).append((method.upper(), path, op))
    for group in sorted(groups):
        out += [f"## {group}", ""]
        for method, path, op in sorted(groups[group], key=lambda x: (x[1], x[0])):
            out += [f"### `{method} {path}`", ""]
            text = " ".join((op.get("description") or op.get("summary") or "").split())
            if text:
                out += [text, ""]
            params = [f"`{p['name']}` ({p['in']}{', required' if p.get('required') else ''})"
                      for p in op.get("parameters", [])]
            if params:
                out += ["Parameters: " + ", ".join(params), ""]
            body = (((op.get("requestBody") or {}).get("content") or {}).get("application/json") or {}).get("schema") or {}
            if body.get("$ref"):
                name = body["$ref"].rsplit("/", 1)[-1]
                fields = ", ".join(f"`{k}`" for k in (schema["components"]["schemas"].get(name, {}).get("properties") or {}))
                out += [f"Body: `{name}`" + (f" with {fields}" if fields else ""), ""]
            out += ["Responses: " + ", ".join(f"`{c}`" for c in sorted(op.get("responses", {}))), ""]
    return "\n".join(out) + "\n"


# --- Markdown subset -> HTML -------------------------------------------------------------------------------------
def _inline(text: str, page: str) -> str:
    text = html.escape(text, quote=False)
    codes: list[str] = []

    def keep(m):
        codes.append("<code>" + m.group(1) + "</code>")
        return f"\0{len(codes) - 1}\0"

    text = re.sub(r"`([^`]+)`", keep, text)
    text = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])", r"<em>\1</em>", text)

    def link(m):
        target = m.group(2)
        if not re.match(r"[a-z]+:|#|/", target):
            target = re.sub(r"\.md(#|$)", r".html\1", target)
        return f'<a href="{html.escape(target)}">{m.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    return re.sub(r"\0(\d+)\0", lambda m: codes[int(m.group(1))], text)


def render_markdown(source: str, page: str = "") -> tuple[str, str]:
    """Return (title, html) for a page written with headings, paragraphs, lists, tables, quotes and code."""
    lines = source.splitlines()
    out: list[str] = []
    title = ""
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            code = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                code.append(lines[i])
                i += 1
            out.append("<pre><code>" + html.escape("\n".join(code)) + "</code></pre>")
            i += 1
        elif m := re.match(r"(#{1,4})\s+(.*)", line):
            level, text = len(m.group(1)), m.group(2).strip()
            title = title or text
            slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
            out.append(f'<h{level} id="{slug}">{_inline(text, page)}</h{level}>')
            i += 1
        elif line.startswith("|") and i + 1 < len(lines) and re.match(r"\|[\s:|-]+\|?\s*$", lines[i + 1]):
            head = [c.strip() for c in line.strip().strip("|").split("|")]
            i += 2
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                i += 1
            table = "<table><thead><tr>" + "".join(f"<th>{_inline(c, page)}</th>" for c in head) + "</tr></thead><tbody>"
            table += "".join("<tr>" + "".join(f"<td>{_inline(c, page)}</td>" for c in r) + "</tr>" for r in rows)
            out.append(table + "</tbody></table>")
        elif re.match(r"\s*([-*]|\d+\.)\s+", line):
            ordered = bool(re.match(r"\s*\d+\.", line))
            items: list[str] = []
            while i < len(lines) and (re.match(r"\s*([-*]|\d+\.)\s+", lines[i]) or (items and lines[i].startswith("  ") and lines[i].strip())):
                if re.match(r"\s*([-*]|\d+\.)\s+", lines[i]):
                    items.append(re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i]))
                else:
                    items[-1] += " " + lines[i].strip()
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>" + "".join(f"<li>{_inline(t, page)}</li>" for t in items) + f"</{tag}>")
        elif line.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].startswith(">"):
                quote.append(lines[i].lstrip("> ").rstrip())
                i += 1
            out.append("<blockquote>" + _inline(" ".join(quote), page) + "</blockquote>")
        elif not line.strip():
            i += 1
        else:
            para = []
            while i < len(lines) and lines[i].strip() and not re.match(r"(#{1,4}\s|```|\||>|\s*([-*]|\d+\.)\s)", lines[i]):
                para.append(lines[i].strip())
                i += 1
            out.append("<p>" + _inline(" ".join(para), page) + "</p>")
    return title, "\n".join(out)


CSS = """body{font:16px/1.55 system-ui,sans-serif;margin:0;display:flex;min-height:100vh;color:#1b1f24}
nav{width:260px;flex:none;background:#f3f5f7;padding:18px;border-right:1px solid #d8dde2;box-sizing:border-box}
nav h2{font-size:12px;text-transform:uppercase;letter-spacing:.05em;color:#5a6672;margin:18px 0 6px}
nav a{display:block;padding:2px 0;color:#0b5cad;text-decoration:none;font-size:14px}
main{max-width:860px;padding:24px 36px;min-width:0}
pre{background:#f3f5f7;padding:12px;overflow:auto;border-radius:6px}code{font-family:ui-monospace,monospace;font-size:.92em}
table{border-collapse:collapse}td,th{border:1px solid #d8dde2;padding:5px 9px;text-align:left;vertical-align:top}
blockquote{border-left:4px solid #0b5cad;margin:12px 0;padding:2px 14px;background:#eef4fb}
@media(prefers-color-scheme:dark){body{background:#14181c;color:#e3e7eb}nav{background:#1c2227;border-color:#2c353d}
nav a,a{color:#6cb0ff}pre{background:#1c2227}td,th{border-color:#2c353d}blockquote{background:#1c2a38}}
@media(max-width:700px){body{display:block}nav{width:auto}main{padding:16px}}
"""
SECTIONS = [("", "Start"), ("user", "User guides"), ("screens", "Console screens"), ("admin", "Administrator guide"),
            ("runbooks", "Runbooks"), ("reference", "Reference")]


def collect_pages(guide: Path = GUIDE, with_api: bool = True) -> dict[str, str]:
    """Relative page path (`screens/cc.md`) -> Markdown source."""
    pages = {p.relative_to(guide).as_posix(): p.read_text(encoding="utf-8") for p in sorted(guide.rglob("*.md"))}
    if with_api:
        pages[API_PAGE] = api_reference_markdown()
    return pages


def check_links(pages: dict[str, str]) -> list[str]:
    """Broken relative links between pages (and unknown anchors are ignored)."""
    errors = []
    for name, source in pages.items():
        for target in re.findall(r"\]\(([^)\s]+)\)", re.sub(r"```.*?```", "", source, flags=re.S)):
            if re.match(r"[a-z]+:|#|/", target):
                continue
            ref = os.path.normpath((Path(name).parent / target.split("#")[0]).as_posix())
            if ref not in pages:
                errors.append(f"{name}: broken link {target}")
    return errors


def build(out: Path, version: str = "", guide: Path = GUIDE, with_api: bool = True) -> list[str]:
    """Write the static site to `out`; returns the relative HTML paths. Raises ValueError for broken links."""
    pages = collect_pages(guide, with_api)
    errors = check_links(pages)
    if errors:
        raise ValueError("documentation site has broken links:\n" + "\n".join(errors))
    titles = {name: render_markdown(src, name)[0] or name for name, src in pages.items()}
    out.mkdir(parents=True, exist_ok=True)
    (out / "style.css").write_text(CSS, encoding="utf-8")
    written = []
    for name, source in pages.items():
        depth = name.count("/")
        up = "../" * depth
        nav = [f'<a href="{up}index.html"><strong>AI Flow documentation</strong></a>']
        for folder, label in SECTIONS:
            members = [n for n in pages if (n.rsplit("/", 1)[0] if "/" in n else "") == folder and n != "index.md"]
            if members:
                nav.append(f"<h2>{label}</h2>")
                nav += [f'<a href="{up}{n[:-3]}.html">{html.escape(titles[n])}</a>' for n in sorted(members)]
        _, body = render_markdown(source, name)
        footer = f"<hr><p><small>AI Flow {html.escape(version)} documentation</small></p>" if version else ""
        page = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
                f'content="width=device-width,initial-scale=1"><title>{html.escape(titles[name])} - AI Flow</title>'
                f'<link rel="stylesheet" href="{up}style.css"></head><body><nav>{"".join(nav)}</nav>'
                f"<main>{body}{footer}</main></body></html>")
        target = out / (name[:-3] + ".html")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(page, encoding="utf-8")
        written.append(target.relative_to(out).as_posix())
    return written


def site_dir() -> Path | None:
    """Where control finds the built site: `$AIFLOW4_DOCS_SITE`, the release's `docs-site/`, or (from a source
    checkout) a copy built once into a temporary directory. None when there is nothing to serve."""
    for candidate in (os.environ.get(SITE_ENV), ROOT / "docs-site"):
        if candidate and (Path(candidate) / "index.html").is_file():
            return Path(candidate)
    if GUIDE.is_dir():
        cache = Path(tempfile.gettempdir()) / f"aiflow4-docs-site-{os.getuid()}"
        if not (cache / "index.html").is_file():
            build(cache)
        return cache
    return None


# --- CI: documentation must follow the product ----------------------------------------------------------------------
def api_routes_by_module() -> dict[str, set[str]]:
    """Repo-relative file of every module that registers control API routes -> the route paths it registers.
    Derived from the routes of the running app, so a new route module is watched without editing this file."""
    from unittest.mock import MagicMock

    from ..control.api import create_app

    found: dict[str, set[str]] = {}
    for route in create_app(MagicMock()).routes:
        code = getattr(getattr(route, "endpoint", None), "__code__", None)
        path = getattr(route, "path", "")
        if code is None or not path.startswith("/v1/"):
            continue
        file = Path(code.co_filename).resolve()
        if ROOT in file.parents:
            found.setdefault(file.relative_to(ROOT).as_posix(), set()).add(path)
    return found


def _mentions(text: str, paths: set[str]) -> bool:
    """True when the text names one of the route paths (cut before the first `{parameter}`)."""
    return any(re.sub(r"/\{.*$", "", p) in text for p in paths)


def _read_doc(name: str) -> str:
    try:
        return (ROOT / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def documentation_gaps(changed: list[str], read=_read_doc, routes: dict[str, set[str]] | None = None,
                       views: set[str] | None = None) -> list[str]:
    """Reasons a change fails the check. A changed screen needs a change to a page of `docs/guide/screens/` (the
    page of each changed screen when `views` names them); a changed API route module needs a changed page in
    `docs/guide/` or `docs/v4/` that names one of the routes that module registers. Unrelated edits do not count."""
    files = [c.strip() for c in changed if c.strip()]
    docs = [f for f in files if f.startswith(DOC_PATHS) and f.endswith(".md")]
    gaps = []
    if any(f.startswith(SCREEN_PATHS) for f in files):
        pages = {f for f in docs if re.fullmatch(r"docs/guide/screens/[a-z]+\.md", f)}
        wanted = {f"docs/guide/screens/{v}.md" for v in views or ()}
        if not (wanted <= pages if wanted else pages):
            gaps.append("a console screen changed (aiflow/v4/control/ui) without a change in "
                        + (", ".join(sorted(wanted - pages)) if wanted else "its page under docs/guide/screens"))
    api_files = [f for f in files if f.endswith(".py") and f.startswith("aiflow/v4/control/")]
    if api_files:
        routes = api_routes_by_module() if routes is None else routes
        texts = [read(f) for f in docs]
        for f in api_files:
            if f in routes and not any(_mentions(t, routes[f]) for t in texts):
                gaps.append(f"the control API changed ({f}) without a changed page in docs/guide or docs/v4 "
                            "that names one of its routes")
    return gaps


def _function_spans(lines: list[str]) -> dict[str, tuple[int, int]]:
    """Return 1-based line spans for top-level JavaScript function declarations in console.html."""
    spans = {}
    for start, line in enumerate(lines):
        match = re.match(r"function\s+([A-Za-z_$][\w$]*)\s*\([^\n]*", line)
        if not match:
            continue
        name = match.group(1)
        end = start if line.rstrip().endswith("}") else next(
            (row for row in range(start + 1, len(lines)) if lines[row] == "}"), None)
        if end is None:
            raise ValueError(f"could not find the end of JavaScript function {name} in the console")
        spans[name] = (start + 1, end + 1)
    return spans


def changed_views(base: str) -> set[str]:
    """Screens whose section or JavaScript renderer changed; fail when a UI hunk cannot be mapped."""
    rel = UI.relative_to(ROOT).as_posix()
    diff = subprocess.run(["git", "diff", "-U0", f"{base}...HEAD", "--", rel], cwd=ROOT, text=True,
                          capture_output=True, check=True).stdout
    lines = UI.read_text(encoding="utf-8").splitlines()
    spans = []
    for i, line in enumerate(lines):
        if m := re.search(r'<section class="view" id="view-([a-z]+)"', line):
            end = next((j for j in range(i, len(lines)) if "</section>" in lines[j]), i)
            spans.append((i + 1, end + 1, m.group(1)))
    functions = _function_spans(lines)
    touched: set[str] = set()
    unmapped: set[int] = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
        first, count = int(m.group(1)), int(m.group(2) or 1)
        last = first + max(count, 1) - 1
        for line_no in range(max(first, 1), last + 1):
            line_views = {v for lo, hi, v in spans if lo <= line_no <= hi}
            for name, (lo, hi) in functions.items():
                if lo <= line_no <= hi:
                    if name not in SCREEN_RENDERER_VIEWS:
                        line_views = set()
                        break
                    mapped = SCREEN_RENDERER_VIEWS[name]
                    line_views |= set(console_views() if mapped is None else mapped)
            if line_views:
                touched |= line_views
            else:
                unmapped.add(line_no)
    if unmapped:
        lines_text = ", ".join(str(n) for n in sorted(unmapped))
        raise ValueError("console UI change could not be mapped to a screen at line(s) " + lines_text
                         + "; add the renderer to SCREEN_RENDERER_VIEWS or keep screen content inside its view section")
    return touched


def changed_files(base: str) -> list[str]:
    result = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT, text=True,
                            capture_output=True, check=True)
    return result.stdout.splitlines()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="docsite", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="build the static documentation site")
    b.add_argument("--out", required=True)
    b.add_argument("--version", default="")
    c = sub.add_parser("check", help="fail when a screen or the API changed without a documentation change")
    c.add_argument("--base", default="origin/develop")
    c.add_argument("--files", nargs="*", help="changed files instead of git diff (tests)")
    args = parser.parse_args(argv)
    if args.cmd == "build":
        pages = build(Path(args.out), args.version)
        print(f"documentation site: {len(pages)} pages in {args.out}")
        return 0
    if args.files is not None:
        gaps = documentation_gaps(args.files)
    else:
        files = changed_files(args.base)
        mapping_error = None
        views = None
        if UI.relative_to(ROOT).as_posix() in files:
            try:
                views = changed_views(args.base)
            except ValueError as exc:
                mapping_error = str(exc)
        gaps = documentation_gaps(files, views=views)
        if mapping_error:
            gaps.append(mapping_error)
    for gap in gaps:
        print("documentation check failed: " + gap, file=sys.stderr)
    if not gaps:
        print("documentation check passed")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
