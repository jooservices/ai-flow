import importlib.util
import json
from pathlib import Path

O2_DIR = Path(__file__).resolve().parents[2] / "deploy" / "v4" / "o2"
DASHBOARDS = O2_DIR / "dashboards"
# deploy/ is not a package and the repo root is not on sys.path under `uv run pytest`: load the script by path
_spec = importlib.util.spec_from_file_location("aiflow4_o2_provision", O2_DIR / "provision.py")
provision_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(provision_module)
EXPECTED_PANELS = {
    "Runs per hour by outcome": "run.finished",
    "AI time per account": "run.finished",
    "Policy denials": "policy.decision",
    "Incidents opened": "incident.opened",
    "Node heartbeats": "node.heartbeat",
}


def test_dashboard_json_defines_the_expected_panels():
    files = sorted(DASHBOARDS.glob("*.json"))
    assert files
    dashboards = [json.loads(path.read_text(encoding="utf-8")) for path in files]
    panels = [panel for dashboard in dashboards for tab in dashboard["tabs"] for panel in tab["panels"]]
    by_title = {panel["title"]: panel for panel in panels}

    assert set(by_title) == set(EXPECTED_PANELS)
    for title, event_type in EXPECTED_PANELS.items():
        panel = by_title[title]
        query = panel["queries"][0]
        assert panel["queryType"] == "sql"
        assert query["fields"]["stream"] == "aiflow4_events"
        assert '"aiflow4_events"' in query["query"]
        assert f"type = '{event_type}'" in query["query"]
        assert query["fields"]["x"] and query["fields"]["y"]
        # OpenObserve reads the snake_case key; a camelCase one is silently ignored
        assert isinstance(panel["config"]["show_legends"], bool)
        assert "showLegends" not in panel["config"]


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeOpenObserve:
    def __init__(self):
        self.dashboards = []
        self.created = []
        self.updated = []

    def get(self, url, params=None):
        return FakeResponse({"dashboards": list(self.dashboards)})

    def post(self, url, params=None, json=None):
        dashboard_id = str(len(self.dashboards) + 1)
        self.created.append((url, params, json))
        self.dashboards.append({"dashboard_id": dashboard_id, "title": json["title"], "hash": f"hash-{dashboard_id}"})
        return FakeResponse({"v8": {"dashboardId": dashboard_id}})

    def put(self, url, params=None, json=None):
        self.updated.append((url, params, json))
        return FakeResponse({})

    def close(self):
        return None


def test_provision_creates_then_updates_dashboards_idempotently():
    client = FakeOpenObserve()

    created = provision_module.provision("http://openobserve/o2", "encoded-auth", client)
    updated = provision_module.provision("http://openobserve/o2", "encoded-auth", client)

    assert created == ["Created: AI Flow events and runs"]
    assert updated == ["Updated: AI Flow events and runs"]
    assert len(client.created) == 1
    assert len(client.updated) == 1
    url, params, payload = client.created[0]
    assert url == "http://openobserve/o2/api/default/dashboards"
    assert params == {"folder": "default"}
    assert payload["version"] == 8
    update_url, update_params, update_payload = client.updated[0]
    assert update_url.endswith("/1")
    assert update_params == {"folder": "default", "hash": "hash-1"}
    assert update_payload["dashboardId"] == "1"


def test_main_reads_openobserve_url_and_basic_auth_from_environment(monkeypatch):
    client = FakeOpenObserve()
    client_args = {}

    def fake_client(**kwargs):
        client_args.update(kwargs)
        return client

    monkeypatch.setenv("AIFLOW4_O2_URL", "http://openobserve/o2")
    monkeypatch.setenv("O2_BASIC", "encoded-auth")
    monkeypatch.setattr(provision_module.httpx, "Client", fake_client)

    provision_module.main()

    assert client_args["headers"] == {"Authorization": "Basic encoded-auth"}
    assert client.created[0][0] == "http://openobserve/o2/api/default/dashboards"
