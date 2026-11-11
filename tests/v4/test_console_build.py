"""Characterization tests for the assembled console document."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from aiflow.v4.control.console_auth import issue_session
from aiflow.v4.control.ui import console_build
from aiflow.v4.control.ui import (
    CONSOLE_PAGE_PATHS,
    build_console_page,
    build_served_console_page,
    console,
    mount_console_pages,
    mount_notifications_page,
    mount_settings_page,
)

SECRET = "console-build-test-secret-at-least-32-bytes"
TAB_MARKER = '      <button class="tab" role="tab" id="tab-licensing"'
VIEW_MARKER = '  <section class="view" id="view-licensing"'
SECURITY_TAB_MARKER = '      <button class="tab" role="tab" id="tab-notifications"'
BOOT_MARKER = "/* ---------- boot ---------- */"
SCRIPT_MARKER = "})();\n</script>"
LICENSING_TAG = '<script src="/console-assets/licensing-notice.js"></script>'
SERVED_TAG = '<script src="/console-assets/console-features.js"></script>'
EXTENSION_NAMES = (
    "csrf.js",
    "layout.js",
    "security.js",
    "load-policy.js",
    "dashboard-timeouts.js",
    "dashboard-silent-runs.js",
    "dashboard_attention.js",
    "dashboard-yield.js",
    "dashboard-cpu.js",
    "system-check.js",
)
BASE_TEMPLATE = (
    TAB_MARKER + ">licensing</button>\n"
    + VIEW_MARKER + ">licensing</section>\n"
    + SECURITY_TAB_MARKER + ">notifications</button>\n"
    + BOOT_MARKER + "\n" + SCRIPT_MARKER + "\n"
    + LICENSING_TAG + "\n"
)


def _source_tree(tmp_path, monkeypatch, manifest="page.html", fragments=None, template=BASE_TEMPLATE):
    source_dir = tmp_path / "console"
    source_dir.mkdir()
    (source_dir / "manifest.txt").write_text(manifest, encoding="utf-8")
    files = {"page.html": template} if fragments is None else fragments
    for name, content in files.items():
        (source_dir / name).write_text(content, encoding="utf-8")
    monkeypatch.setattr(console_build, "CONSOLE_SOURCES", source_dir)
    monkeypatch.setattr(console_build, "HERE", tmp_path)
    return source_dir


def _write_extensions(root):
    for name in EXTENSION_NAMES:
        (root / name).write_text(name.removesuffix(".js").upper(), encoding="utf-8")


class AdminDB:
    def one(self, _sql, _params=()):
        return {
            "role": "administrator",
            "roles": ["administrator"],
            "project_keys": None,
            "disabled": False,
            "totp_enabled": True,
        }


def test_raw_builder_keeps_manifest_fragment_order_and_extension_order():
    from aiflow.v4.control.ui import HERE, console_source_names, console_template

    names = (HERE / "console" / "manifest.txt").read_text(encoding="utf-8").split()
    expected_template = "".join(
        (HERE / "console" / name).read_text(encoding="utf-8") for name in names
    )
    assert console_source_names() == names
    assert console_template() == expected_template

    page = build_console_page()
    ordered = (
        'id="tab-system-check"',
        'id="tab-security"',
        (HERE / "csrf.js").read_text(encoding="utf-8"),
        (HERE / "layout.js").read_text(encoding="utf-8"),
        (HERE / "security.js").read_text(encoding="utf-8"),
        (HERE / "load-policy.js").read_text(encoding="utf-8"),
        (HERE / "dashboard-timeouts.js").read_text(encoding="utf-8"),
        (HERE / "dashboard-silent-runs.js").read_text(encoding="utf-8"),
        (HERE / "dashboard_attention.js").read_text(encoding="utf-8"),
        (HERE / "dashboard-yield.js").read_text(encoding="utf-8"),
        (HERE / "dashboard-cpu.js").read_text(encoding="utf-8"),
        (HERE / "system-check.js").read_text(encoding="utf-8"),
    )
    positions = [page.index(part) for part in ordered]
    assert positions == sorted(positions)
    assert page.count("/* ---------- boot ---------- */") == 1


def test_all_console_page_routes_return_the_current_console_response():
    app = FastAPI()
    ctx = SimpleNamespace(
        settings=SimpleNamespace(console_session_secret=SECRET),
        db=AdminDB(),
    )
    mount_console_pages(app)
    mount_notifications_page(app, ctx)
    mount_settings_page(app, ctx)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(
        "aiflow4_session",
        issue_session("admin@example.test", "administrator", 2_500_000_000, SECRET),
    )
    expected = console()
    assert expected.body == build_console_page().replace(
        '<script src="/console-assets/licensing-notice.js"></script>',
        '<script src="/console-assets/console-features.js"></script>',
        1,
    ).encode("utf-8")
    assert expected.body == build_served_console_page().encode("utf-8")
    assert expected.headers["cache-control"] == "no-store"

    page_paths = tuple(path for path in CONSOLE_PAGE_PATHS if path not in {"/", "/help"})
    assert "/notifications" in page_paths and "/settings" in page_paths
    for path in page_paths:
        response = client.get(path)
        assert response.status_code == 200, path
        assert response.content == expected.body, path
        assert response.headers["content-type"].startswith("text/html"), path
        assert response.headers["cache-control"] == "no-store", path


def test_controlled_manifest_splits_whitespace_and_repeats_without_separators(tmp_path, monkeypatch):
    _source_tree(
        tmp_path,
        monkeypatch,
        manifest=" first.html\n\tsecond.html  first.html  empty.html ",
        fragments={"first.html": "A", "second.html": "B", "empty.html": ""},
    )
    assert console_build.console_source_names() == ["first.html", "second.html", "first.html", "empty.html"]
    assert console_build.console_template() == "ABA"
    (tmp_path / "console" / "first.html").write_text("C", encoding="utf-8")
    assert console_build.console_template() == "CBC"


@pytest.mark.parametrize(
    ("manifest", "fragments", "error"),
    [
        ("absent.html", {}, FileNotFoundError),
        ("page.html", {"page.html": b"\xff"}, UnicodeDecodeError),
    ],
)
def test_source_read_errors_propagate(tmp_path, monkeypatch, manifest, fragments, error):
    source_dir = tmp_path / "console"
    source_dir.mkdir()
    (source_dir / "manifest.txt").write_text(manifest, encoding="utf-8")
    for name, content in fragments.items():
        path = source_dir / name
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(console_build, "CONSOLE_SOURCES", source_dir)
    with pytest.raises(error) as caught:
        console_build.console_template()
    if error is FileNotFoundError:
        assert caught.value.filename == str(source_dir / "absent.html")
    else:
        assert (caught.value.encoding, caught.value.reason) == ("utf-8", "invalid start byte")


def test_unreadable_manifest_fragment_propagates_without_returning_a_page(tmp_path, monkeypatch):
    source_dir = _source_tree(tmp_path, monkeypatch)
    fragment = source_dir / "page.html"
    read_text = type(fragment).read_text

    def read_with_permission_error(path, *args, **kwargs):
        if path == fragment:
            raise PermissionError(13, "Permission denied", str(path))
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(fragment), "read_text", read_with_permission_error)
    result = None
    with pytest.raises(PermissionError) as caught:
        result = console_build.build_console_page()
    assert result is None
    assert (caught.value.errno, caught.value.strerror, caught.value.filename) == (
        13, "Permission denied", str(fragment)
    )


def test_unreadable_extension_propagates_without_returning_a_page(tmp_path, monkeypatch):
    _source_tree(tmp_path, monkeypatch)
    _write_extensions(tmp_path)
    extension = tmp_path / "security.js"
    read_text = type(extension).read_text

    def read_with_permission_error(path, *args, **kwargs):
        if path == extension:
            raise PermissionError(13, "Permission denied", str(path))
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(type(extension), "read_text", read_with_permission_error)
    result = None
    with pytest.raises(PermissionError) as caught:
        result = console_build.build_console_page()
    assert result is None
    assert (caught.value.errno, caught.value.strerror, caught.value.filename) == (
        13, "Permission denied", str(extension)
    )


def test_manifest_omission_does_not_change_the_assembled_page(tmp_path, monkeypatch):
    source_dir = _source_tree(tmp_path, monkeypatch, fragments={"page.html": BASE_TEMPLATE})
    _write_extensions(tmp_path)
    expected_template = console_build.console_template()
    expected_page = console_build.build_console_page()
    (source_dir / "unlisted.html").write_text("UNLISTED CONTENT", encoding="utf-8")

    assert console_build.console_source_names() == ["page.html"]
    assert console_build.console_template() == expected_template == BASE_TEMPLATE
    assert console_build.build_console_page() == expected_page
    assert "UNLISTED CONTENT" not in expected_page


def test_empty_manifest_is_empty_template_and_absent_manifest_propagates(tmp_path, monkeypatch):
    source_dir = _source_tree(tmp_path, monkeypatch, manifest="", fragments={})
    assert console_build.console_source_names() == []
    assert console_build.console_template() == ""
    (source_dir / "manifest.txt").unlink()
    with pytest.raises(FileNotFoundError) as caught:
        console_build.console_source_names()
    assert caught.value.filename == str(source_dir / "manifest.txt")


def test_invalid_utf8_manifest_propagates(tmp_path, monkeypatch):
    source_dir = tmp_path / "console"
    source_dir.mkdir()
    (source_dir / "manifest.txt").write_bytes(b"\xff")
    monkeypatch.setattr(console_build, "CONSOLE_SOURCES", source_dir)
    with pytest.raises(UnicodeDecodeError) as caught:
        console_build.console_source_names()
    assert (caught.value.encoding, caught.value.object, caught.value.start, caught.value.end,
            caught.value.reason) == ("utf-8", b"\xff", 0, 1, "invalid start byte")


@pytest.mark.parametrize("marker", [TAB_MARKER, VIEW_MARKER, SECURITY_TAB_MARKER, BOOT_MARKER, SCRIPT_MARKER])
def test_missing_insertion_markers_keep_the_existing_error(tmp_path, monkeypatch, marker):
    template = BASE_TEMPLATE.replace(marker, "", 1)
    _source_tree(tmp_path, monkeypatch, template=template)
    with pytest.raises(RuntimeError, match="^console template is missing a view insertion point$"):
        console_build.build_console_page()


def test_assembly_preserves_extension_order_duplicate_markers_and_first_only_replacements(tmp_path, monkeypatch):
    duplicate = BASE_TEMPLATE + BASE_TEMPLATE
    _source_tree(tmp_path, monkeypatch, template=duplicate)
    _write_extensions(tmp_path)

    raw = console_build.build_console_page()
    served = console_build.build_served_console_page()
    extensions = [name.removesuffix(".js").upper() for name in EXTENSION_NAMES]
    assert [raw.index(name) for name in extensions] == sorted(raw.index(name) for name in extensions)
    assert raw.count('id="tab-system-check"') == 1
    assert raw.count('id="view-system-check"') == 1
    assert raw.count('id="tab-security"') == 1
    assert raw.index('id="tab-system-check"') < raw.index('id="tab-licensing"')
    assert raw.index('id="tab-security"') < raw.index('id="tab-notifications"')
    assert raw.count(BOOT_MARKER) == 2
    assert raw.count(SCRIPT_MARKER) == 2
    assert raw.count(LICENSING_TAG) == 2
    assert served.count(SERVED_TAG) == 1
    assert served.count(LICENSING_TAG) == 1
    assert console_build.build_console_page() == raw
    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(lambda _index: console_build.build_console_page(), range(4))) == [raw] * 4
    (tmp_path / "security.js").write_text("SECURITY-UPDATED", encoding="utf-8")
    assert "SECURITY-UPDATED" in console_build.build_console_page()


@pytest.mark.parametrize("asset", ["csrf.js", "load-policy.js"])
def test_missing_extension_file_propagates(tmp_path, monkeypatch, asset):
    _source_tree(tmp_path, monkeypatch)
    _write_extensions(tmp_path)
    (tmp_path / asset).unlink()
    with pytest.raises(FileNotFoundError) as caught:
        console_build.build_console_page()
    assert caught.value.filename == str(tmp_path / asset)


def test_invalid_utf8_extension_propagates(tmp_path, monkeypatch):
    _source_tree(tmp_path, monkeypatch)
    _write_extensions(tmp_path)
    (tmp_path / "security.js").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError) as caught:
        console_build.build_console_page()
    assert (caught.value.encoding, caught.value.object, caught.value.start, caught.value.end,
            caught.value.reason) == ("utf-8", b"\xff", 0, 1, "invalid start byte")
