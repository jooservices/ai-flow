from types import SimpleNamespace

import httpx

from aiflow.v4 import cli
from aiflow.v4.core.settings import Settings


def test_node_join_cli_sends_short_lived_token_without_name(tmp_path, monkeypatch, capsys):
    join_token = tmp_path / "join-token"
    join_token.write_text("short-lived-token")
    node_token = tmp_path / "state" / "node.token"
    def fake_settings(_cls):
        return SimpleNamespace(control_url="https://control.example", node_token_file=str(node_token))

    monkeypatch.setattr(Settings, "from_env", classmethod(fake_settings))
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        @staticmethod
        def json():
            return {"node": "node-from-token", "node_token": "long-lived-node-token"}

    def post(url, json, timeout):
        calls.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(httpx, "post", post)

    assert cli.main(["node", "join", "--join-token-file", str(join_token), "--ip", "192.0.2.44"]) == 0

    assert calls == [("https://control.example/v1/nodes/join",
                      {"join_token": "short-lived-token", "ip": "192.0.2.44"}, 30)]
    assert node_token.read_text() == "long-lived-node-token"
    assert oct(node_token.stat().st_mode & 0o777) == "0o600"
    assert "joined node-from-token" in capsys.readouterr().out
