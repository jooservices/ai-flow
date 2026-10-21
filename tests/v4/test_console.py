from pathlib import Path

PAGE = Path(__file__).resolve().parents[2] / "aiflow" / "v4" / "control" / "ui" / "console.html"


def test_node_drawer_shows_registration_and_last_seen():
    text = PAGE.read_text()
    assert "fact('IP'" in text
    assert "fact('Joined'" in text
    assert "fact('Last heartbeat'" in text
    assert "fact('Hostname'" in text
    assert "last seen " in text
    assert "n.last_heartbeat" in text
