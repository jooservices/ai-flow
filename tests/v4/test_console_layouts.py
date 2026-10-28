"""Per-person console layouts (docs/v4/console-layouts.md): API, role defaults, stale layouts, the block list of the
console page, the layout functions of the script and a real browser run."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from aiflow.v4.control import layouts
from aiflow.v4.control.console_auth import ROLE_PERMISSIONS, issue_session

SECRET = "test-console-layout-secret-with-at-least-32-bytes"
PAGE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"
BOOT = "/* ---------- boot ---------- */"
STUBS = """
var document = {addEventListener: function () {}, documentElement: {setAttribute: function () {},
  classList: {add: function () {}, remove: function () {}}},
  getElementById: function () { return null; }, querySelector: function () { return null; },
  querySelectorAll: function () { return []; }};
var window = {matchMedia: function () { return {matches: false}; }, addEventListener: function () {}};
var location = {pathname: '/cc'};
var localStorage = {getItem: function () { return null; }, setItem: function () {}};
var fetch = function () {};
var setInterval = function () {};
"""


def sign_in(client, ctx, role: str, person: str):
    ctx.settings.console_session_secret = SECRET
    client.cookies.set("aiflow4_session", issue_session(person, role, 2_000_000_000, SECRET))


def blocks_of(response) -> list[dict]:
    assert response.status_code == 200, response.text
    return response.json()["blocks"]


def ids(blocks: list[dict]) -> list[str]:
    return [b["id"] for b in blocks]


# --- the page list and the console markup agree -------------------------------------------------------------------
def test_every_block_of_the_registry_is_marked_in_the_console_and_the_other_way_round():
    html = PAGE.read_text()
    doctor_asset = (PAGE.parent / "flow-doctor.js").read_text()
    marked = set(re.findall(r'data-block="([a-z-]+)"', html + doctor_asset))
    registered = {block for _, blocks in layouts.PAGES.values() for block, _, _ in blocks}
    # Work board reuses the Delivery board markup; it has no layout of its own.
    assert marked == registered
    for page, (_, blocks) in layouts.PAGES.items():
        assert len({b[0] for b in blocks}) == len(blocks), page
        assert all(b[2] in layouts.SIZES for b in blocks)


def test_the_pages_in_scope_are_the_ones_the_ticket_names():
    assert set(layouts.PAGES) == {"dashboard", "review", "machines", "knowledge", "delivery", "users"}


# --- role defaults ---------------------------------------------------------------------------------------------
def test_every_console_role_has_a_default_layout_for_every_page():
    for role in ROLE_PERMISSIONS:
        for page in layouts.PAGES:
            blocks = layouts.default_layout(page, role)
            assert sorted(ids(blocks)) == sorted(layouts.page_ids(page))
            assert not all(b["hidden"] for b in blocks)


def test_role_defaults_differ_by_role_and_cover_the_roles_of_the_ticket():
    owner = layouts.default_layout("dashboard", "owner")
    lead = layouts.default_layout("dashboard", "lead")
    owner_ids = ids(owner)
    assert owner_ids[:3] == ["tokens", "quota", "usage"]
    assert ids(lead)[:3] == ["kpis", "flow", "waiting"]  # the board first
    assert layouts.default_layout("dashboard", "super_admin") == owner
    assert layouts.profile_for("product_owner") == "business"
    assert layouts.profile_for("delivery") == "lead"
    assert layouts.profile_for("ba") == layouts.profile_for("business") == "business"
    assert layouts.profile_for("team_lead") == "lead"
    assert layouts.profile_for("reviewer") == "reviewer"
    for role in ("team_lead", "business", "sa", "human_reviewer"):
        assert layouts.profile_for(role)
    assert ids(layouts.default_layout("dashboard", "human_reviewer"))[0] == "waiting"
    assert layouts.default_layout("dashboard", "unknown-role") == layouts.default_layout("dashboard", "nobody")


@pytest.mark.parametrize("role", ["business", "reviewer"])
def test_dashboard_quota_is_visible_by_default_and_showable_without_machines_access(role):
    default = layouts.default_layout("dashboard", role)
    quota = next(block for block in default if block["id"] == "quota")
    assert quota["hidden"] is False

    # Even if this person hides the safe quota summary later, layout mode must offer it in Show.
    hidden_quota = [dict(block, hidden=block["id"] == "quota") for block in default]
    out = run_js(f"""
S.consoleUser = {json.dumps({"role": role, "permissions": sorted(ROLE_PERMISSIONS[role])})};
var hidden = layoutHidden({json.dumps(hidden_quota)}, 'cc');
console.log(JSON.stringify({{
  hidden: hidden.map(function (block) {{ return block.id; }}),
  show: layoutBarHtml(true, hidden, 'dashboard').indexOf('data-id=\"quota\"') >= 0
}}));""")
    assert out["show"] is True
    assert "quota" in out["hidden"]


def test_dashboard_has_one_page_wide_layout_grid_for_both_sections():
    html = PAGE.read_text()
    assert "data-layout-section=" not in html
    assert "root.querySelectorAll('[data-block]')" in html
    assert "Arrange all Dashboard blocks together" in html


# --- resolve: stale and new blocks -----------------------------------------------------------------------------
def test_a_removed_block_in_a_saved_layout_is_ignored_without_error():
    saved = [{"id": "gone", "size": "small", "hidden": False}, {"id": "quota", "size": "medium", "hidden": False},
             {"id": "quota", "size": "small", "hidden": True}, "junk", {"id": 5}]
    resolved = layouts.resolve("dashboard", "owner", saved)
    assert "gone" not in ids(resolved)
    assert ids(resolved)[0] == "quota" and resolved[0]["size"] == "medium" and resolved[0]["hidden"] is False
    assert sorted(ids(resolved)) == sorted(layouts.page_ids("dashboard"))


def test_a_block_added_later_is_appended_visible_even_when_the_role_hides_it_by_default():
    saved = [{"id": "kpis", "size": "full", "hidden": False}, {"id": "flow", "size": "small", "hidden": False}]
    resolved = layouts.resolve("dashboard", "lead", saved)  # the lead default hides "tokens" and "autonomy"
    assert ids(resolved)[:2] == ["kpis", "flow"]
    assert len(resolved) == len(layouts.page_ids("dashboard"))
    assert all(not b["hidden"] for b in resolved[2:])


def test_a_saved_layout_with_a_bad_size_falls_back_to_the_block_default():
    resolved = layouts.resolve("review", "owner", [{"id": "queue", "size": "huge", "hidden": "yes"}])
    assert resolved[0] == {"id": "queue", "size": "full", "hidden": False}


# --- API -----------------------------------------------------------------------------------------------------
def test_layout_api_needs_a_console_session(client, ctx):
    ctx.settings.console_session_secret = SECRET
    assert client.get("/v1/console/layouts").status_code == 401
    assert client.put("/v1/console/layouts/dashboard", json={"blocks": []}).status_code == 401
    assert client.delete("/v1/console/layouts/dashboard").status_code == 401


def test_first_read_returns_the_role_default_for_every_page(client, ctx):
    sign_in(client, ctx, "lead", "layout-lead@example.test")
    data = client.get("/v1/console/layouts").json()
    assert data["person"] == "layout-lead@example.test" and data["profile"] == "lead"
    assert set(data["pages"]) == set(layouts.PAGES) and data["sizes"] == list(layouts.SIZES)
    dashboard = data["pages"]["dashboard"]
    assert dashboard["customized"] is False
    assert [(b["id"], b["size"], b["hidden"]) for b in dashboard["blocks"]] == [
        (b["id"], b["size"], b["hidden"]) for b in layouts.default_layout("dashboard", "lead")]
    assert all(b["title"] for b in dashboard["blocks"])


def test_save_then_restore_in_another_browser(client, ctx):
    from fastapi.testclient import TestClient
    sign_in(client, ctx, "owner", "layout-owner@example.test")
    wanted = [{"id": "quota", "size": "large", "hidden": False}, {"id": "kpis", "size": "small", "hidden": True}]
    saved = client.put("/v1/console/layouts/dashboard", json={"blocks": wanted})
    assert saved.status_code == 200 and saved.json()["customized"] is True
    # A second browser (no shared cookie jar) signed in as the same person gets the same layout from the server.
    other = TestClient(client.app)
    other.cookies.set("aiflow4_session", issue_session("layout-owner@example.test", "owner", 2_000_000_000, SECRET))
    restored = other.get("/v1/console/layouts").json()["pages"]["dashboard"]
    assert restored["customized"] is True
    assert restored["blocks"][0]["id"] == "quota" and restored["blocks"][0]["size"] == "large"
    assert restored["blocks"][1] == {"id": "kpis", "title": "Summary cards", "size": "small", "hidden": True}
    assert len(restored["blocks"]) == len(layouts.page_ids("dashboard"))  # the unmentioned blocks follow
    row = ctx.db.one("SELECT blocks FROM console_layouts WHERE email=%s AND page='dashboard'",
                     ("layout-owner@example.test",))
    assert row["blocks"] == wanted


def test_layouts_are_kept_per_person_and_per_page(client, ctx):
    sign_in(client, ctx, "owner", "layout-a@example.test")
    client.put("/v1/console/layouts/review", json={"blocks": [{"id": "threads", "size": "medium", "hidden": False}]})
    pages = client.get("/v1/console/layouts").json()["pages"]
    assert pages["review"]["blocks"][0]["id"] == "threads" and pages["dashboard"]["customized"] is False
    sign_in(client, ctx, "owner", "layout-b@example.test")
    pages = client.get("/v1/console/layouts").json()["pages"]
    assert pages["review"]["customized"] is False and pages["review"]["blocks"][0]["id"] == "queue"


def test_reset_returns_the_page_to_the_role_default(client, ctx):
    sign_in(client, ctx, "lead", "layout-reset@example.test")
    client.put("/v1/console/layouts/dashboard", json={"blocks": [{"id": "autonomy", "size": "small", "hidden": False}]})
    assert client.get("/v1/console/layouts").json()["pages"]["dashboard"]["blocks"][0]["id"] == "autonomy"
    reset = client.delete("/v1/console/layouts/dashboard")
    assert reset.status_code == 200 and reset.json()["customized"] is False
    assert ids(reset.json()["blocks"]) == ids(layouts.default_layout("dashboard", "lead"))
    assert ctx.db.one("SELECT 1 AS x FROM console_layouts WHERE email=%s", ("layout-reset@example.test",)) is None
    assert client.delete("/v1/console/layouts/dashboard").status_code == 200  # resetting twice is harmless


def test_a_stale_stored_layout_is_read_without_error_and_new_blocks_are_added(client, ctx):
    sign_in(client, ctx, "owner", "layout-stale@example.test")
    old = [{"id": "removed-in-a-release", "size": "full", "hidden": False}, {"id": "users", "size": "small", "hidden": False}]
    ctx.db.run("INSERT INTO console_layouts (email, page, blocks) VALUES (%s, 'users', %s::jsonb)",
               ("layout-stale@example.test", json.dumps(old)))
    page = client.get("/v1/console/layouts").json()["pages"]["users"]
    assert ids(page["blocks"]) == ["users", "access"]
    assert page["blocks"][0]["size"] == "small" and page["blocks"][1]["hidden"] is False


def test_saving_ignores_unknown_blocks_and_refuses_unknown_pages_and_sizes(client, ctx):
    sign_in(client, ctx, "owner", "layout-valid@example.test")
    ok = client.put("/v1/console/layouts/machines", json={"blocks": [
        {"id": "no-such-block", "size": "full", "hidden": False}, {"id": "add-machine", "size": "medium", "hidden": False}]})
    assert ok.status_code == 200 and ids(ok.json()["blocks"]) == ["add-machine", "machines"]
    stored = ctx.db.one("SELECT blocks FROM console_layouts WHERE email=%s AND page='machines'",
                        ("layout-valid@example.test",))
    assert ids(stored["blocks"]) == ["add-machine"]
    assert client.put("/v1/console/layouts/nowhere", json={"blocks": []}).status_code == 404
    assert client.delete("/v1/console/layouts/nowhere").status_code == 404
    assert client.put("/v1/console/layouts/machines", json={"blocks": [
        {"id": "machines", "size": "huge", "hidden": False}]}).status_code == 400
    assert client.put("/v1/console/layouts/machines", json={"blocks": "all"}).status_code == 422
    too_many = [{"id": "machines", "size": "full", "hidden": False}] * (layouts.MAX_BLOCKS + 1)
    assert client.put("/v1/console/layouts/machines", json={"blocks": too_many}).status_code == 422


# --- the script's layout functions ------------------------------------------------------------------------------
def run_js(driver: str) -> object:
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is needed to run the console script")
    script = re.search(r"<script>\n(.*?)</script>", PAGE.read_text(), re.S).group(1)
    script = script[:script.index(BOOT)]
    result = subprocess.run([node, "-"], input=STUBS + script + driver + "\n})();\n", text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_script_orders_known_blocks_first_and_puts_a_newer_block_at_the_end():
    out = run_js("""
var model = [{id: 'b'}, {id: 'gone'}, {id: 'a'}];
console.log(JSON.stringify({
  order: layoutOrder(model, ['a', 'new', 'b']),
  none: layoutOrder(null, ['x', 'y'])
}));""")
    assert out == {"order": ["b", "a", "new"], "none": ["x", "y"]}


def test_script_moves_a_block_among_the_shown_blocks_with_the_arrow_keys():
    out = run_js("""
var blocks = [{id: 'a'}, {id: 'h'}, {id: 'b'}, {id: 'c'}];
var shown = ['a', 'b', 'c'];
var down = layoutMove(blocks, 'a', 1, shown), edge = layoutMove(blocks, 'a', -1, shown);
var end = layoutMove(blocks, 'a', 'end', shown), start = layoutMove(blocks, 'c', 'start', shown);
console.log(JSON.stringify({
  down: down.blocks.map(function (b) { return b.id; }), downAt: down.position, downTotal: down.total,
  edge: edge.moved, edgeAt: edge.position,
  end: end.blocks.map(function (b) { return b.id; }), start: start.blocks.map(function (b) { return b.id; }),
  absent: layoutMove(blocks, 'zzz', 1, shown)
}));""")
    assert out["down"] == ["h", "b", "a", "c"] and out["downAt"] == 2 and out["downTotal"] == 3
    assert out["edge"] is False and out["edgeAt"] == 1
    assert out["end"] == ["h", "b", "c", "a"] and out["start"] == ["c", "a", "h", "b"]
    assert out["absent"] is None


def test_script_keeps_blocks_that_are_not_on_screen_in_place_when_the_screen_order_changes():
    out = run_js("""
var model = [{id: 'a', size: 's'}, {id: 'editor', size: 'm'}, {id: 'b', size: 'l'}, {id: 'c', size: 'f'}];
var present = [{id: 'c', size: 'f'}, {id: 'a', size: 's'}, {id: 'b', size: 'l'}];
console.log(JSON.stringify(layoutMerge(model, present).map(function (b) { return b.id; })));""")
    assert out == ["c", "editor", "a", "b"]


def test_hidden_conditional_block_is_listed_even_when_it_is_not_rendered():
    out = run_js("""
var model = [
  {id: 'header', title: 'Knowledge and Skills', hidden: false},
  {id: 'editor', title: 'Create or edit a resource', hidden: true},
  {id: 'resources', title: 'Resources', hidden: false}
];
var rendered = [{id: 'header'}, {id: 'resources'}];
var hidden = layoutHidden(model);
console.log(JSON.stringify({
  rendered: rendered.map(function (b) { return b.id; }),
  hidden: hidden.map(function (b) { return b.id; }),
  show: layoutBarHtml(true, hidden, 'knowledge').indexOf('data-id="editor"') >= 0
}));""")
    assert out == {"rendered": ["header", "resources"], "hidden": ["editor"], "show": True}


def test_script_queues_reset_after_a_pending_layout_save():
    out = run_js("""
var calls = [], releaseSave;
fetch = function (url, options) {
  calls.push(options.method);
  if (options.method === 'PUT') return new Promise(function (resolve) { releaseSave = function () { resolve({ok: true}); }; });
  return Promise.resolve({ok: true, json: function () { return Promise.resolve({blocks: []}); }});
};
S.view = 'users';
S.layouts.pages.users = {blocks: [{id: 'users', title: 'Console users', size: 'small', hidden: false}]};
window.confirm = function () { return true; };
render = function () {};
layoutAnnounce = function () {};
toast = function () {};
layoutPersist('users', S.layouts.pages.users);
layoutAction('layout-reset', {getAttribute: function () { return null; }});
var reset = S.layouts.chain;
Promise.resolve().then(function () {
  var before = calls.slice();
  releaseSave();
  return reset.then(function () { return new Promise(function (resolve) { setTimeout(resolve, 0); }); }).then(function () {
    console.log(JSON.stringify({before: before, after: calls}));
  });
});""")
    assert out == {"before": ["PUT"], "after": ["PUT", "DELETE"]}


def test_every_layout_page_maps_to_a_known_view_and_container():
    out = run_js("console.log(JSON.stringify(LAYOUT_PAGES));")
    assert set(out.values()) == set(layouts.PAGES)


# --- a real browser ---------------------------------------------------------------------------------------------
def test_layout_mode_in_a_browser_drag_resize_hide_keyboard_reset_and_phone(ctx):
    try:
        from playwright.sync_api import Error, sync_playwright
    except ImportError:
        Error, sync_playwright = Exception, None
    import socket
    import threading
    import time
    import uvicorn

    from aiflow.v4.control import login
    from aiflow.v4.control.api import create_app
    from aiflow.v4.control.ui import mount_ui

    playwright = browser = None
    if sync_playwright is not None:
        try:
            playwright = sync_playwright().start()
        except Error as exc:
            pytest.skip(f"Playwright cannot start on this node: {exc}")
    try:
        if playwright is not None:
            try:
                browser = playwright.chromium.launch(headless=True)
            except Error as exc:
                pytest.skip(f"Playwright Chromium cannot launch on this node: {exc}")
        ctx.settings.console_login = True
        ctx.settings.console_session_secret = "browser-test-session-secret-at-least-32-bytes"
        ctx.settings.public_url = ""
        person = "layout-browser@example.test"
        password = login.seed_user(ctx.db, person, "owner", "Layout Browser")
        assert password
        app = create_app(ctx)
        mount_ui(app, ctx)
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind(("127.0.0.1", 0))
        server_socket.listen(128)
        base_url = f"http://127.0.0.1:{server_socket.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [server_socket]}, daemon=True)
        try:
            thread.start()
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started

            if browser is None:
                # This machine ships the Node Playwright CLI and Chromium, but not the optional Python binding.
                # Use that installed browser to cover the direct Dashboard redraw regression in this test run.
                import os

                cli = shutil.which("playwright")
                if not cli:
                    pytest.skip("Playwright is not installed")
                env = os.environ.copy()
                env.update({
                    "AIFLOW4_LAYOUT_BASE_URL": base_url,
                    "AIFLOW4_LAYOUT_EMAIL": person,
                    "AIFLOW4_LAYOUT_PASSWORD": password,
                    "AIFLOW4_LAYOUT_PLAYWRIGHT_MODULE": str(Path(cli).resolve().parent),
                })
                script = r"""
const assert = require('node:assert/strict');
const { chromium } = require(process.env.AIFLOW4_LAYOUT_PLAYWRIGHT_MODULE);
(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage();
    const base = process.env.AIFLOW4_LAYOUT_BASE_URL;
    await page.goto(base + '/login?next=%2Fcc', { waitUntil: 'domcontentloaded' });
    await page.getByLabel('Email').fill(process.env.AIFLOW4_LAYOUT_EMAIL);
    await page.getByLabel('Password').fill(process.env.AIFLOW4_LAYOUT_PASSWORD);
    await page.getByRole('button', { name: 'Sign in' }).click();
    await page.waitForURL(base + '/cc');
    await page.goto(base + '/dashboard', { waitUntil: 'domcontentloaded' });
    await page.getByRole('button', { name: 'Customize layout' }).waitFor({ state: 'visible', timeout: 10000 });
    const grid = page.locator('#view-cc .blocks');
    await page.waitForFunction(() => document.querySelectorAll('#view-cc .blocks .block-cell').length >= 8);
    let order = await grid.locator('.block-cell').evaluateAll(cells => cells.map(cell => cell.dataset.blockId));
    assert.deepEqual(order.slice(0, 3), ['tokens', 'quota', 'usage']);
    await page.getByRole('button', { name: 'Customize layout' }).click();
    assert.match(await page.locator('#view-cc [data-layout-bar]').innerText(), /Arrange all Dashboard blocks together/);
    await page.locator('#view-cc [data-layout-handle][data-id="tokens"]').focus();
    await page.keyboard.press('End');
    order = await grid.locator('.block-cell').evaluateAll(cells => cells.map(cell => cell.dataset.blockId));
    assert.equal(order.at(-1), 'tokens');
    await page.locator("#view-cc .sort-btn[data-key='open']").click();
    await page.locator("#view-cc [data-act='layout-done']").waitFor({ state: 'visible' });
    assert.ok(await page.locator('#view-cc [data-layout-handle]').count() >= 8);
    order = await page.locator('#view-cc .blocks .block-cell').evaluateAll(cells => cells.map(cell => cell.dataset.blockId));
    assert.equal(order.at(-1), 'tokens');
    await page.locator("#view-cc [data-act='layout-done']").click();
    assert.equal(await page.locator('#view-cc [data-layout-handle]').count(), 0);
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.stack || String(error)); process.exitCode = 1; });
"""
                result = subprocess.run(["node", "-e", script], env=env, capture_output=True, text=True, timeout=60)
                assert result.returncode == 0, result.stderr
                return

            def sign_in_page(context):
                page = context.new_page()
                page.goto(base_url + "/login?next=%2Fcc", wait_until="domcontentloaded")
                page.get_by_label("Email").fill(person)
                page.get_by_label("Password").fill(password)
                page.get_by_role("button", name="Sign in").click()
                page.wait_for_url(base_url + "/cc")
                page.goto(base_url + "/users", wait_until="domcontentloaded")
                return page

            def order(page):
                return page.eval_on_selector_all("#view-users .block-cell", "e => e.map(c => c.dataset.blockId)")

            context = browser.new_context()
            page = sign_in_page(context)
            page.get_by_role("button", name="Customize layout").wait_for(state="visible", timeout=10000)
            assert order(page) == ["users", "access"]
            # AC1/AC2/AC3: layout mode shows handles, sizes and Hide; Done leaves it.
            page.get_by_role("button", name="Customize layout").click()
            assert page.locator("#view-users [data-layout-handle]").count() == 2
            page.get_by_role("button", name="Small width for Your console access").click()
            assert page.locator("#view-users [data-block-id='access']").get_attribute("data-size") == "small"
            # AC6: the arrow keys move the focused handle's block and the change is announced.
            page.locator("#view-users [data-layout-handle][data-id='access']").focus()
            page.keyboard.press("ArrowUp")
            assert order(page) == ["access", "users"]
            page.wait_for_function("document.getElementById('layout-live').textContent.includes('moved to position 1 of 2')")
            assert page.evaluate("document.activeElement.dataset.id") == "access"
            # AC2: dragging the handle moves the block and the other makes room.
            handle = page.locator("#view-users [data-layout-handle][data-id='access']")
            target = page.locator("#view-users [data-block-id='users']")
            hb, tb = handle.bounding_box(), target.bounding_box()
            page.mouse.move(hb["x"] + 8, hb["y"] + 8)
            page.mouse.down()
            page.mouse.move(tb["x"] + 40, tb["y"] + 60, steps=8)
            page.mouse.up()
            assert order(page) == ["users", "access"]
            # AC3: hide, list under hidden blocks, show again.
            page.get_by_role("button", name="Hide Console users").click()
            page.get_by_role("button", name="Show Console users").wait_for(state="visible")
            assert page.locator("#view-users [data-block-id='users']").is_hidden()
            page.get_by_role("button", name="Show Console users").click()
            assert page.locator("#view-users [data-block-id='users']").is_visible()
            page.get_by_role("button", name="Done").click()
            assert page.locator("#view-users [data-layout-handle]").count() == 0
            # AC4: the layout comes from the server in a new browser (no shared storage).
            other = sign_in_page(browser.new_context())
            other.get_by_role("button", name="Customize layout").wait_for(state="visible", timeout=10000)
            assert order(other) == ["users", "access"]
            assert other.locator("#view-users [data-block-id='access']").get_attribute("data-size") == "small"
            # AC5: reset returns to the role default.
            page.on("dialog", lambda dialog: dialog.accept())
            page.get_by_role("button", name="Customize layout").click()
            page.get_by_role("button", name="Reset this page").click()
            page.wait_for_function("document.querySelector(\"#view-users [data-block-id='access']\").dataset.size === 'medium'")
            assert order(page) == ["users", "access"]
            # AC7: one block per row on a phone; layout mode works with touch.
            phone = browser.new_context(viewport={"width": 390, "height": 800}, has_touch=True, is_mobile=True)
            small = sign_in_page(phone)
            small.get_by_role("button", name="Customize layout").wait_for(state="visible", timeout=10000)
            first, second = (small.locator(f"#view-users [data-block-id='{i}']").bounding_box() for i in ("users", "access"))
            assert second["y"] >= first["y"] + first["height"] - 1 and abs(first["x"] - second["x"]) < 2
            small.get_by_role("button", name="Customize layout").tap()
            assert small.locator("#view-users [data-layout-handle]").count() == 2

            # The owner sees cost and quota first on the shared Dashboard grid. Keyboard movement crosses the
            # former Work/AI section boundary; sorting redraws the content directly while layout mode stays active.
            page.goto(base_url + "/dashboard", wait_until="domcontentloaded")
            page.get_by_role("button", name="Customize layout").wait_for(state="visible", timeout=10000)
            dashboard_grid = page.locator("#view-cc .blocks")
            assert dashboard_grid.locator(".block-cell").evaluate_all("cells => cells.map(c => c.dataset.blockId)")[:3] == [
                "tokens", "quota", "usage"]
            page.get_by_role("button", name="Customize layout").click()
            assert "Arrange all Dashboard blocks together" in page.locator("#view-cc [data-layout-bar]").inner_text()
            page.locator("#view-cc [data-layout-handle][data-id='tokens']").focus()
            page.keyboard.press("End")
            before = dashboard_grid.locator(".block-cell").evaluate_all("cells => cells.map(c => c.dataset.blockId)")
            assert before[-1] == "tokens"
            page.locator("#view-cc .sort-btn[data-key='open']").click()
            assert page.locator("#view-cc [data-act='layout-done']").is_visible()
            assert page.locator("#view-cc [data-layout-handle]").count() >= 8
            after = page.locator("#view-cc .blocks .block-cell").evaluate_all(
                "cells => cells.map(c => c.dataset.blockId)")
            assert after == before
            page.locator("#view-cc [data-act='layout-done']").click()
            assert page.locator("#view-cc [data-layout-handle]").count() == 0
        finally:
            if browser is not None:
                browser.close()
            server.should_exit = True
            if thread.is_alive():
                thread.join(timeout=5)
            server_socket.close()
    finally:
        if playwright is not None:
            playwright.stop()
