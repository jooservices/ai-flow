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
    features = client.get("/console-assets/console-features.js")

    assert 'src="/console-assets/console-features.js"' in page.text
    assert script.status_code == 200
    assert features.status_code == 200
    assert "function licensingNotice(state, escapeHtml)" in features.text
    assert "escapeHtml(new Date(state.over_limit_since)" in features.text


def test_console_loads_provider_routing_from_its_feature_asset():
    app = FastAPI()
    mount_ui(app, None)
    client = TestClient(app)

    page = client.get("/machines")
    script = client.get("/console-assets/console-features.js")

    assert page.status_code == 200
    assert 'src="/console-assets/console-features.js"' in page.text
    assert script.status_code == 200
    assert "AIFlowProviderRouting" in script.text
    assert "function render()" in script.text
    assert "MutationObserver" in script.text


def test_console_loads_flow_doctor_panel_from_its_ui_asset():
    app = FastAPI()
    mount_ui(app, None)
    client = TestClient(app)

    page = client.get("/dashboard")
    script = client.get("/console-assets/flow-doctor.js")

    assert page.status_code == 200
    assert 'src="/console-assets/flow-doctor.js"' in page.text
    assert script.status_code == 200
    assert "function flowDoctorPanel(d, esc, when)" in script.text
