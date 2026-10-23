"""Documentation site: build the guides in `docs/guide/` and the API reference into a static site, and check in CI
that a screen or API change comes with a documentation change.

    python3 -m aiflow.v4.core.docsite build --out build/docs-site [--version vX.Y.Z]
    python3 -m aiflow.v4.core.docsite check --base origin/develop

The site is plain HTML written with the standard library only (no site generator dependency). The release
packaging (`deploy/v4/ci/package-release.sh`) puts it into the release tarball as `docs-site/`; control serves it
at `/help/` and the console Help links open `/help/screens/<view>.html`. The API reference page is generated from
the mounted FastAPI control app at build time and is never written by hand. The runbook set is checked
against the incident kinds the code raises (`incident_kinds()`).
"""
from __future__ import annotations

import argparse
import ast
import html
import inspect
import os
import re
import subprocess
import sys
import tempfile
import typing
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[3]
GUIDE = ROOT / "docs" / "guide"
CONTROL = ROOT / "aiflow" / "v4" / "control"
UI = CONTROL / "ui" / "console.html"
SITE_ENV = "AIFLOW4_DOCS_SITE"
API_PAGE = "reference/api.md"
SAFE_LINK_SCHEMES = frozenset({"http", "https", "mailto"})

# Console screens (`id="view-<name>"` in console.html) and the page that explains each one.
SCREEN_PAGE = "screens/{view}.md"

# JavaScript renderers run outside the static <section> ranges. Keep this map beside the check so a renderer edit
# requires the page for the screen it renders. `None` means the renderer is shared by every screen.
SCREEN_RENDERER_VIEWS: dict[str, tuple[str, ...] | None] = {
    "renderGitHubIntegrations": ("integrations",),
    "renderLive": None,
    "renderDashboard": ("cc",),
    "renderCC": ("cc", "machines"),
    "renderNodeJoin": ("cc",),
    "renderSA": ("sa",),
    "renderDHead": ("work", "delivery"),
    "renderProjectPicker": ("work", "delivery"),
    "renderProjectPickerOptions": ("work", "delivery"),
    "renderDBody": ("work", "delivery"),
    "renderReviews": ("review",),
    "renderKnowledge": ("knowledge",),
    "renderLearningKnowledge": ("knowledge",),
    "renderMachines": ("machines",),
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
    "renderBackup": ("backup",),
    "renderSystemCheck": ("system-check",),
    "renderNotifications": ("notifications",),
    "renderProjects": ("projects",),
    "renderUsers": ("users",),
    "renderLicensing": ("licensing",),
    "renderLicenseNotice": ("licensing",),
    "renderCommandSearchResults": None,
    "renderRoleHealthSettings": ("machines",),
    "roleHealthPanel": ("machines",),
    "render": None,
    "helpUrl": None,
    "hintSeen": None,
    "renderHelp": None,
    "showView": None,
}

# These Help elements are shared by every screen and live outside the screen sections/renderers. A change to one
# therefore requires the corresponding Help page to be reviewed for every screen. Individual hint strings below
# `HINTS` are mapped to their own screen instead.
SCREEN_SHARED_UI_PATTERNS = (
    re.compile(r"^\s*\.firstuse\{"),
    re.compile(r'\bid="(?:help-link|firstuse)"'),
    re.compile(r"\bhint-dismiss\b"),
)

# A change to one of these needs a documentation change in the same pull request.
SCREEN_PATHS = ("aiflow/v4/control/ui/",)
DOC_PATHS = ("docs/guide/", "docs/v4/")


# --- what the code raises ---------------------------------------------------------------------------------------
def incident_kinds(source_root: Path = ROOT / "aiflow" / "v4") -> set[str]:
    """Incident kinds raised anywhere in v4 code by `open_incident(ctx, kind, ...)`."""
    kinds: set[str] = set()
    for path in sorted(source_root.rglob("*.py")):
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
    return re.findall(r'<section class="view" id="view-([a-z-]+)"', page.read_text(encoding="utf-8"))


def role_names() -> list[str]:
    from ..control.console_auth import ROLE_PERMISSIONS
    return sorted(ROLE_PERMISSIONS)


def _mounted_control_app():
    """Create the control app with its UI routes mounted for API discovery and reference generation."""
    from unittest.mock import MagicMock

    from ..control.api import create_app
    from ..control.ui import mount_ui

    app = create_app(MagicMock())
    mount_ui(app, MagicMock())
    return app


# --- API reference, generated from the FastAPI app --------------------------------------------------------------
def api_reference_markdown(version: str = "") -> str:
    """Markdown for every route of the control API, from the app's OpenAPI schema."""
    from .. import VERSION

    schema = _mounted_control_app().openapi()
    out = ["# API reference", "",
           f"**Assumes:** you know HTTP and JSON, you have a role or a token that may call the endpoint, and you use "
           f"control API version {version or VERSION}. This page is generated from the code in the mounted FastAPI control app "
           "when "
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
            params = [f"`{p['name']}` ({p['in']}, {_schema_type(p.get('schema') or {}, schema['components']['schemas'])}"
                      f"{', required' if p.get('required') else ', optional'})"
                      for p in op.get("parameters", [])]
            if params:
                out += ["Parameters: " + ", ".join(params), ""]
            request = op.get("requestBody") or {}
            request_content = request.get("content") or {}
            for content_type, media in sorted(request_content.items()):
                body = media.get("schema") or {}
                out += [f"Request body ({content_type}{', required' if request.get('required') else ', optional'}):",
                        *_schema_markdown(body, schema["components"]["schemas"]), ""]
            for code, response in sorted(op.get("responses", {}).items()):
                response = _resolve_openapi_ref(response, schema)
                out += [f"Response `{code}`: {response.get('description', '').strip() or 'No description.'}"]
                for content_type, media in sorted((response.get("content") or {}).items()):
                    out += [f"\nSchema ({content_type}):", *_schema_markdown(
                        media.get("schema") or {}, schema["components"]["schemas"])]
                out.append("")
    return "\n".join(out) + "\n"


def _resolve_openapi_ref(value: dict, schema: dict) -> dict:
    """Resolve an OpenAPI component reference without mutating the source schema."""
    result = value
    seen = set()
    while isinstance(result, dict) and result.get("$ref"):
        ref = result["$ref"]
        if ref in seen or not ref.startswith("#/components/"):
            return result
        seen.add(ref)
        node = schema
        try:
            for part in ref.removeprefix("#/").split("/"):
                node = node[part]
        except (KeyError, TypeError):
            return result
        result = node
    return result


def _schema_field_shape(value: dict, components: dict) -> dict:
    """Return the concrete shape of a nullable or union field for recursive field listing."""
    value = _resolve_openapi_ref(value, {"components": {"schemas": components}})
    if not isinstance(value, dict):
        return {}
    branches = value.get("anyOf") or value.get("oneOf") or []
    if branches:
        resolved = [_resolve_openapi_ref(branch, {"components": {"schemas": components}})
                    for branch in branches]
        return next((branch for branch in resolved
                     if isinstance(branch, dict) and (branch.get("properties") or
                                                     branch.get("type") in ("object", "array"))),
                    resolved[0] if isinstance(resolved[0], dict) else {})
    return value


def _schema_type(value: dict, components: dict, seen: frozenset[str] = frozenset()) -> str:
    """Readable JSON type for an OpenAPI schema, including references, arrays and unions."""
    if not isinstance(value, dict):
        return "any"
    ref = value.get("$ref")
    if ref:
        name = ref.rsplit("/", 1)[-1]
        if name in seen:
            return name
        return _schema_type(components.get(name, {}), components, seen | {name})
    for union in ("anyOf", "oneOf"):
        if value.get(union):
            return " or ".join(_schema_type(item, components, seen) for item in value[union])
    kind = value.get("type")
    if isinstance(kind, list):
        return " or ".join(map(str, kind))
    if kind == "array":
        return f"array of {_schema_type(value.get('items') or {}, components, seen)}"
    if kind:
        suffix = f" ({value['format']})" if value.get("format") else ""
        return str(kind) + suffix
    if value.get("properties"):
        return "object"
    if value.get("enum"):
        return "string"
    return "any"


def _schema_markdown(value: dict, components: dict, prefix: str = "", required: bool = True,
                     depth: int = 0, seen: frozenset[str] = frozenset()) -> list[str]:
    """List the schema's fields with JSON types and required status; recurse through nested objects."""
    value = _resolve_openapi_ref(value, {"components": {"schemas": components}})
    ref_name = value.get("$ref", "").rsplit("/", 1)[-1] if isinstance(value, dict) else ""
    if ref_name:
        if ref_name in seen:
            return [f"- `{prefix or 'value'}`: {ref_name} (recursive)" ]
        seen = seen | {ref_name}
    if not isinstance(value, dict):
        return [f"- `{prefix or 'value'}`: any" ]
    properties = value.get("properties") or {}
    if not properties:
        return [f"- `{prefix or 'value'}`: {_schema_type(value, components, seen)}"
                f"{' (required)' if required else ' (optional)'}"]
    rows = []
    required_names = set(value.get("required") or ())
    for name, field in sorted(properties.items()):
        path = f"{prefix}.{name}" if prefix else name
        is_required = name in required_names
        description = " ".join(str(field.get("description", "")).split())
        suffix = f" — {description}" if description else ""
        rows.append(f"- `{path}`: {_schema_type(field, components, seen)} "
                    f"({'required' if is_required else 'optional'}){suffix}")
        nested = _schema_field_shape(field, components)
        if depth < 8 and isinstance(nested, dict):
            if nested.get("type") == "array":
                nested = nested.get("items") or {}
            if nested.get("properties") or nested.get("$ref"):
                rows.extend(_schema_markdown(nested, components,
                                             path + "[]" if _schema_field_shape(field, components).get("type") == "array"
                                             else path,
                                             True, depth + 1, seen))
    return rows


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
        raw_target = m.group(2)
        target = _decode_link_target(raw_target)
        if not _safe_link_target(raw_target):
            return m.group(1)
        parsed = urlsplit(target)
        if not parsed.scheme and not target.startswith(("#", "/")):
            target = re.sub(r"\.md(?=($|[?#]))", ".html", target)
        return f'<a href="{html.escape(target)}">{m.group(1)}</a>'

    text = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, text)
    return re.sub(r"\0(\d+)\0", lambda m: codes[int(m.group(1))], text)


def _decode_link_target(target: str) -> str:
    """Undo HTML escaping from `_inline` and decode entities supplied in the Markdown source."""
    return html.unescape(html.unescape(target)).strip()


def _safe_link_target(raw_target: str) -> bool:
    """Accept safe external schemes and same-site relative URLs, never active or network-path URLs."""
    target = _decode_link_target(raw_target)
    if not target or "\\" in target or any(ord(char) < 0x20 or ord(char) == 0x7F for char in target):
        return False
    try:
        parsed = urlsplit(target)
    except ValueError:
        return False
    if parsed.scheme:
        return parsed.scheme.lower() in SAFE_LINK_SCHEMES
    return not parsed.netloc and not target.startswith("//")


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


def collect_pages(guide: Path = GUIDE, with_api: bool = True, version: str = "") -> dict[str, str]:
    """Relative page path (`screens/cc.md`) -> Markdown source."""
    pages = {p.relative_to(guide).as_posix(): p.read_text(encoding="utf-8") for p in sorted(guide.rglob("*.md"))}
    if with_api:
        pages[API_PAGE] = api_reference_markdown(version)
    return pages


def check_links(pages: dict[str, str]) -> list[str]:
    """Unsafe targets and broken relative links between pages (unknown anchors are ignored)."""
    errors = []
    for name, source in pages.items():
        for target in re.findall(r"\]\(([^)\s]+)\)", re.sub(r"```.*?```", "", source, flags=re.S)):
            if not _safe_link_target(target):
                errors.append(f"{name}: unsafe link target {target}")
                continue
            target = _decode_link_target(target)
            parsed = urlsplit(target)
            if parsed.scheme or target.startswith("/") or not parsed.path:
                continue
            ref = os.path.normpath((Path(name).parent / parsed.path).as_posix())
            if ref not in pages:
                errors.append(f"{name}: broken link {target}")
    return errors


def build(out: Path, version: str = "", guide: Path = GUIDE, with_api: bool = True) -> list[str]:
    """Write the static site to `out`; returns the relative HTML paths. Raises ValueError for broken links."""
    pages = collect_pages(guide, with_api, version)
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
@lru_cache(maxsize=1)
def _control_function_registry():
    """Parse control functions and imports so route contracts include shared response helpers they call."""
    modules: dict[str, tuple[str, ast.Module]] = {}
    path_modules: dict[Path, str] = {}
    for path in sorted(CONTROL.rglob("*.py")):
        relative = path.relative_to(ROOT).with_suffix("")
        parts = list(relative.parts)
        if parts[-1] == "__init__":
            parts.pop()
        module = ".".join(parts)
        modules[module] = (path.relative_to(ROOT).as_posix(), ast.parse(path.read_text(encoding="utf-8")))
        path_modules[path.resolve()] = module
    functions = {module: {node.name: node for node in ast.walk(tree)
                          if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
                 for module, (_, tree) in modules.items()}

    def imported_targets(module: str, node: ast.AST) -> dict[str, tuple[str, str | None]]:
        targets = {}
        module_parts = module.split(".")
        is_package = Path(modules[module][0]).name == "__init__.py"
        package = module if is_package else ".".join(module_parts[:-1])
        if isinstance(node, ast.Import):
            for item in node.names:
                target = item.name
                alias = item.asname or item.name.split(".")[0]
                bound = target if item.asname else item.name.split(".")[0]
                if bound in modules:
                    targets[alias] = (bound, None)
            return targets
        if not isinstance(node, ast.ImportFrom):
            return targets
        base = package
        if node.level:
            pieces = base.split(".") if base else []
            if node.level > len(pieces) + 1:
                return targets
            base = ".".join(pieces[:len(pieces) - node.level + 1])
        target_module = ".".join(p for p in (base, node.module or "") if p)
        for item in node.names:
            alias = item.asname or item.name
            if node.module is None:
                candidate = ".".join(p for p in (target_module, item.name) if p)
                if candidate in modules:
                    targets[alias] = (candidate, None)
            elif target_module in modules:
                targets[alias] = (target_module, item.name)
        return targets

    module_imports = {module: {} for module in modules}
    for module, (_, tree) in modules.items():
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module_imports[module].update(imported_targets(module, node))
    return modules, functions, module_imports, imported_targets, path_modules


def _called_control_function_spans(endpoint, registry) -> list[tuple[str, tuple[int, int]]]:
    """Find control helpers called directly by an API endpoint."""
    modules, functions, module_imports, imported_targets, path_modules = registry
    endpoint_code = getattr(endpoint, "__code__", None)
    if endpoint_code is None:
        return []
    source_path = Path(endpoint_code.co_filename).resolve()
    module = path_modules.get(source_path)
    if module is None:
        return []
    start = endpoint_code.co_firstlineno
    endpoint_node = next((fn for fn in functions.get(module, {}).values()
                          if fn.name == endpoint.__name__
                          and min([fn.lineno, *(item.lineno for item in getattr(fn, "decorator_list", ()))])
                          <= start <= (fn.end_lineno or fn.lineno)), None)
    if endpoint_node is None:
        return []

    imports = dict(module_imports[module])
    for inner in ast.walk(endpoint_node):
        if isinstance(inner, (ast.Import, ast.ImportFrom)):
            imports.update(imported_targets(module, inner))
    calls = set()
    for inner in ast.walk(endpoint_node):
        if not isinstance(inner, ast.Call):
            continue
        func = inner.func
        if isinstance(func, ast.Name):
            if func.id in functions.get(module, {}):
                calls.add((module, func.id))
            elif func.id in imports and imports[func.id][1]:
                calls.add(imports[func.id])
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            target = imports.get(func.value.id)
            if target and target[1] is None:
                calls.add((target[0], func.attr))
    result = []
    for target_module, target_function in sorted(calls):
        target_node = functions.get(target_module, {}).get(target_function or "")
        if target_node is None or (target_module == module and target_function == endpoint.__name__):
            continue
        decorators = getattr(target_node, "decorator_list", ())
        first = min([target_node.lineno, *(item.lineno for item in decorators)])
        result.append((modules[target_module][0], (first, target_node.end_lineno or target_node.lineno)))
    return result


@lru_cache(maxsize=1)
def api_route_contract_spans() -> dict[str, dict[str, list[tuple[int, int]]]]:
    """Repo-relative API module -> method and route contract -> source spans of its handlers.
    Derived from the running app, so a new route module is watched without editing this file."""
    found: dict[str, dict[str, list[tuple[int, int]]]] = {}
    helper_registry = _control_function_registry()
    for route in _mounted_control_app().routes:
        endpoint = getattr(route, "endpoint", None)
        code = getattr(endpoint, "__code__", None)
        path = getattr(route, "path", "")
        if code is None or not path.startswith("/v1/"):
            continue
        file = Path(code.co_filename).resolve()
        if ROOT not in file.parents:
            continue
        try:
            source, first = inspect.getsourcelines(endpoint)
            span = (first, first + len(source) - 1)
        except (OSError, TypeError):
            span = (code.co_firstlineno, code.co_firstlineno)
        module = file.relative_to(ROOT).as_posix()
        schemas = _schema_spans(route)
        helpers = _called_control_function_spans(endpoint, helper_registry)
        for method in sorted(getattr(route, "methods", ())):
            if method in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
                contract = f"{method} {path}"
                found.setdefault(module, {}).setdefault(contract, []).append(span)
                # The request/response models are part of the route's contract: editing one changes the API.
                for schema_module, schema_span in schemas:
                    found.setdefault(schema_module, {}).setdefault(contract, []).append(schema_span)
                for helper_module, helper_span in helpers:
                    found.setdefault(helper_module, {}).setdefault(contract, []).append(helper_span)
    return found


def _schema_spans(route) -> list[tuple[str, tuple[int, int]]]:
    """(repo-relative module, source span) of each class declared in the repo that a route takes or returns:
    models in the handler signature and `response_model`."""
    classes: list[type] = []

    def collect(annotation, depth: int = 0) -> None:
        if depth > 4 or annotation is None:
            return
        if isinstance(annotation, type):
            classes.append(annotation)
        for arg in getattr(annotation, "__args__", ()) or ():
            collect(arg, depth + 1)

    try:
        hints = typing.get_type_hints(route.endpoint)  # resolves string annotations (`from __future__`)
    except Exception:  # noqa: BLE001 - unresolved forward reference: fall back to what is declared
        hints = {k: v for k, v in getattr(route.endpoint, "__annotations__", {}).items() if not isinstance(v, str)}
    for annotation in hints.values():
        collect(annotation)
    collect(getattr(route, "response_model", None))
    spans = []
    for cls in classes:
        if not cls.__module__.startswith("aiflow."):
            continue
        try:
            file = Path(inspect.getsourcefile(cls) or "").resolve()
            source, first = inspect.getsourcelines(cls)
        except (OSError, TypeError):
            continue
        if ROOT in file.parents:
            spans.append((file.relative_to(ROOT).as_posix(), (first, first + len(source) - 1)))
    # OpenAPI responses may use a schema constant rather than a Pydantic model. Track the source assignment
    # whenever the mounted route uses that exact object, so edits to the schema are attributed to its route.
    response_ids: set[int] = set()

    def collect_ids(value) -> None:
        if isinstance(value, dict):
            response_ids.add(id(value))
            for key, item in value.items():
                collect_ids(key)
                collect_ids(item)
        elif isinstance(value, (list, tuple)):
            response_ids.add(id(value))
            for item in value:
                collect_ids(item)

    collect_ids(getattr(route, "responses", {}))
    endpoint_globals = getattr(route.endpoint, "__globals__", {})
    source_file = Path(getattr(route.endpoint, "__code__").co_filename).resolve()
    if ROOT in source_file.parents and response_ids:
        try:
            tree = ast.parse(source_file.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            tree = None
        if tree is not None:
            for node in ast.walk(tree):
                if isinstance(node, ast.Assign):
                    names = [target.id for target in node.targets if isinstance(target, ast.Name)]
                elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    names = [node.target.id]
                else:
                    continue
                if any(id(endpoint_globals.get(name)) in response_ids for name in names):
                    spans.append((source_file.relative_to(ROOT).as_posix(),
                                  (node.lineno, node.end_lineno or node.lineno)))
    return spans


@lru_cache(maxsize=1)
def api_routes_by_module() -> dict[str, set[str]]:
    """Repo-relative file of every module that registers control API routes -> its route paths."""
    return {module: {contract.split(" ", 1)[1] for contract in contracts}
            for module, contracts in api_route_contract_spans().items()}


def _mentions(text: str, routes: set[str]) -> bool:
    """True when text names an exact route path, optionally with its HTTP method.

    Route parameters may use different names in prose (`{key}` or `<KEY>`), but every path segment and
    suffix must be present. A reference to `/v1/knowledge` does not name `/v1/knowledge/{kind}`.
    """
    for route in routes:
        match = re.match(r"^(GET|POST|PUT|PATCH|DELETE)\s+(.+)$", route)
        method, path = (match.group(1), match.group(2)) if match else (None, route)
        parts = re.split(r"(\{[^}]+\}|<[^>]+>)", path)
        escaped = "".join(r"[^/<>]+" if re.fullmatch(r"\{[^}]+\}|<[^>]+>", part)
                          else re.escape(part) for part in parts)
        path_pattern = rf"(?<![\w]){escaped}(?![\w/])"
        if method:
            pattern = rf"\b{method}\s+`?{path_pattern}`?"
        else:
            pattern = path_pattern
        if re.search(pattern, text):
            return True
    return False


def _literal_route_contract_spans(source: str) -> dict[str, list[tuple[int, int]]]:
    """Find literal FastAPI route decorators and their handler spans in source text."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found: dict[str, list[tuple[int, int]]] = {}
    methods = {"get": "GET", "post": "POST", "put": "PUT", "patch": "PATCH", "delete": "DELETE"}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            path = decorator.args[0].value if decorator.args and isinstance(decorator.args[0], ast.Constant) else None
            if not isinstance(path, str) or not path.startswith("/v1/"):
                continue
            method = methods.get(decorator.func.attr.lower())
            registered = [method] if method else []
            if decorator.func.attr.lower() == "api_route":
                methods_arg = next((kw.value for kw in decorator.keywords if kw.arg == "methods"), None)
                if isinstance(methods_arg, (ast.List, ast.Tuple)):
                    registered = [m.value.upper() for m in methods_arg.elts
                                  if isinstance(m, ast.Constant) and isinstance(m.value, str)
                                  and m.value.upper() in methods.values()]
            for verb in registered:
                start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
                found.setdefault(f"{verb} {path}", []).append((start, node.end_lineno or node.lineno))
    return found


def _diff_hunks(diff: str) -> list[tuple[int, int, int, int]]:
    """Return (old start, old count, new start, new count) line ranges from a unified diff."""
    return [(int(m.group(1)), int(m.group(2) or 1), int(m.group(3)), int(m.group(4) or 1))
            for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", diff, re.M)]


def changed_api_routes(base: str, changed: list[str]) -> dict[str, set[str]]:
    """Find the method/path contracts whose handler or registration changed between `base` and HEAD."""
    current = api_route_contract_spans()
    found: dict[str, set[str]] = {}
    for module in changed:
        # Literal decorators recover the base revision's route contracts without importing or executing it.
        old_source = subprocess.run(["git", "show", f"{base}:{module}"], cwd=ROOT, text=True,
                                    capture_output=True).stdout
        previous = _literal_route_contract_spans(old_source)
        now = current.get(module, {})
        if not now and not previous:
            continue
        diff = subprocess.run(["git", "diff", "-U0", f"{base}...HEAD", "--", module], cwd=ROOT,
                              text=True, capture_output=True, check=True).stdout
        hunks = _diff_hunks(diff)
        # A contract present at the base and gone now (even the module's last route) was removed: it needs docs.
        contracts: set[str] = set(previous) - set(now)
        for old_start, old_count, new_start, new_count in hunks:
            current_hits = {contract for contract, spans in now.items()
                            if new_count and any(start <= new_start + new_count - 1 and new_start <= end
                                                 for start, end in spans)}
            old_hits = {contract for contract, spans in previous.items()
                        if old_count and any(start <= old_start + old_count - 1 and old_start <= end
                                             for start, end in spans)}
            contracts.update(current_hits)
            for contract in old_hits:
                # A live route is the contract docs should describe. For a pure route deletion, retain the old
                # contract so the change still requires an explicit documentation update.
                if contract in now or not current_hits:
                    contracts.add(contract)
        found[module] = contracts
    return found


def _read_doc(name: str) -> str:
    try:
        return (ROOT / name).read_text(encoding="utf-8")
    except OSError:
        return ""


def documentation_gaps(changed: list[str], read=_read_doc, routes: dict[str, set[str]] | None = None,
                       views: set[str] | None = None,
                       changed_routes: dict[str, set[str]] | None = None) -> list[str]:
    """Reasons a change fails the check. A changed screen needs a change to a page of `docs/guide/screens/` (the
    page of each changed screen when `views` names them); a changed API route module needs changed documentation
    that names every changed method/path contract. When route-level diff data is unavailable, every registered
    route is required so an unrelated route cannot satisfy the check. Unrelated edits do not count."""
    files = [c.strip() for c in changed if c.strip()]
    docs = [f for f in files if f.startswith(DOC_PATHS) and f.endswith(".md")]
    gaps = []
    if any(f.startswith(SCREEN_PATHS) for f in files):
        pages = {f for f in docs if re.fullmatch(r"docs/guide/screens/[a-z-]+\.md", f)}
        wanted = {f"docs/guide/screens/{v}.md" for v in views or ()}
        if not (wanted <= pages if wanted else pages):
            gaps.append("a console screen changed (aiflow/v4/control/ui) without a change in "
                        + (", ".join(sorted(wanted - pages)) if wanted else "its page under docs/guide/screens"))
    api_files = [f for f in files if f.endswith(".py") and f.startswith("aiflow/v4/control/")]
    if api_files:
        routes = api_routes_by_module() if routes is None else routes
        changed_routes = {} if changed_routes is None else changed_routes
        texts = [read(f) for f in docs]
        for f in api_files:
            if f not in routes and not changed_routes.get(f):
                continue
            if f in changed_routes and not changed_routes[f]:
                # Route-level diffing succeeded and found no changed handler, registration or schema contract.
                continue
            expected = changed_routes.get(f, routes.get(f, set()))
            missing = {contract for contract in expected if not any(_mentions(text, {contract}) for text in texts)}
            if missing:
                gaps.append(f"the control API changed ({f}) without changed documentation naming "
                            + ", ".join(sorted(missing)))
            elif not expected:
                gaps.append(f"the control API changed ({f}) but its changed lines could not be matched to a "
                            "route contract; update the documentation check's route mapping")
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
    # Diff coordinates refer to HEAD. During a merge, the worktree/index can already contain upstream edits that
    # shift those coordinates, so map against the exact file version used by the diff.
    source = subprocess.run(["git", "show", f"HEAD:{rel}"], cwd=ROOT, text=True, capture_output=True, check=True).stdout
    lines = source.splitlines()
    all_views = set(console_views())
    hints_start = next((i for i, line in enumerate(lines) if line.startswith("var HINTS = {")), None)
    hints_end = next((i for i in range(hints_start + 1, len(lines)) if lines[i] == "};"), None) \
        if hints_start is not None else None
    spans = []
    for i, line in enumerate(lines):
        if m := re.search(r'<section class="view" id="view-([a-z-]+)"', line):
            end = next((j for j in range(i, len(lines)) if "</section>" in lines[j]), i)
            spans.append((i + 1, end + 1, m.group(1)))
    functions = _function_spans(lines)
    help_start = next((i for i, line in enumerate(lines) if line.startswith("/* Help: every screen")), None)
    help_end = functions.get("renderHelp", (0, 0))[1] - 1
    touched: set[str] = set()
    unmapped: set[int] = set()
    for m in re.finditer(r"^@@ -\S+ \+(\d+)(?:,(\d+))? @@", diff, re.M):
        first, count = int(m.group(1)), int(m.group(2) or 1)
        last = first + max(count, 1) - 1
        for line_no in range(max(first, 1), last + 1):
            line_views = {v for lo, hi, v in spans if lo <= line_no <= hi}
            changed_line = lines[line_no - 1] if line_no <= len(lines) else ""
            hint_view = None
            for name, (lo, hi) in functions.items():
                if lo <= line_no <= hi:
                    if name not in SCREEN_RENDERER_VIEWS:
                        line_views = set()
                        break
                    mapped = SCREEN_RENDERER_VIEWS[name]
                    line_views |= (all_views if mapped is None else set(mapped))
            # Keep a hint-text edit local to that screen. The declarations and shared Help plumbing affect every
            # screen, including edits to the top-bar link, first-use container and dismiss behavior.
            if hints_start is not None and hints_end is not None and hints_start <= line_no - 1 <= hints_end:
                hint = re.match(r"\s*([a-z]+):\s*'", changed_line)
                if hint:
                    hint_view = hint.group(1)
                    line_views.add(hint_view)
                else:
                    line_views |= all_views
            if (help_start is not None and help_start <= line_no - 1 <= help_end and hint_view is None):
                line_views |= all_views
            if any(pattern.search(changed_line) for pattern in SCREEN_SHARED_UI_PATTERNS):
                line_views |= all_views
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
        api_files = [f for f in files if f.endswith(".py") and f.startswith("aiflow/v4/control/")]
        changed_routes = changed_api_routes(args.base, api_files) if api_files else None
        if UI.relative_to(ROOT).as_posix() in files:
            try:
                views = changed_views(args.base)
            except ValueError as exc:
                mapping_error = str(exc)
        gaps = documentation_gaps(files, views=views, changed_routes=changed_routes)
        if mapping_error:
            gaps.append(mapping_error)
    for gap in gaps:
        print("documentation check failed: " + gap, file=sys.stderr)
    if not gaps:
        print("documentation check passed")
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
