from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiflow.v4.control.ui import mount_ui


def test_console_loads_licensing_notice_from_its_ui_asset():
    app = FastAPI()
    mount_ui(app, None)
    client = TestClient(app)

    page = client.get("/licensing")
    script = client.get("/console-assets/licensing-notice.js")

    assert page.status_code == 200
    assert 'src="/console-assets/licensing-notice.js"' in page.text
    assert script.status_code == 200
    assert "function licensingNotice(state, escapeHtml)" in script.text
    assert "escapeHtml(new Date(state.over_limit_since)" in script.text
