from fastapi import FastAPI
from fastapi.testclient import TestClient

from aiflow.v4.control.ui import mount_ui


def test_work_board_loads_the_run_inbox_asset():
    app = FastAPI()
    mount_ui(app, None)
    client = TestClient(app)

    page = client.get("/work")
    asset = client.get("/console-assets/run-inbox.js")

    assert page.status_code == 200 and "/console-assets/run-inbox.js" in page.text
    assert asset.status_code == 200 and "runInboxPanel" in asset.text
