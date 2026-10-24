"""Acceptance checks for the end-user CLI inventory and UI principle."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_inventory_covers_requested_owner_and_user_operations():
    inventory = (ROOT / "docs/v4/no-cli.md").read_text(encoding="utf-8")

    for operation in (
        "Install the control plane",
        "Provision a VM and enroll a node",
        "Install or repair node tools",
        "Assign an AI account to a node",
        "Sign an AI account in",
        "Connect or disconnect Jira",
        "Connect GitHub",
        "Add, pause, resume, or remove a project",
        "Manage runtime secrets",
        "Upgrade or recover the control plane",
        "Pause or drain a node",
        "View operation and system logs",
    ):
        assert operation in inventory

    for ticket in ("TESTFLOW-54", "TESTFLOW-55", "TESTFLOW-70", "TESTFLOW-71", "TESTFLOW-76", "TESTFLOW-77", "TESTFLOW-81"):
        assert ticket in inventory


def test_uncovered_operations_are_explicit_lead_backlog_proposals():
    inventory = (ROOT / "docs/v4/no-cli.md").read_text(encoding="utf-8")

    assert inventory.count("Proposed – ticket to be created by the lead after merge") >= 3
    assert "Assign an AI account to a node" in inventory
    assert "UI available; see [Machines and AI management](machines-and-ai.md)" in inventory
    assert "Change an existing project's settings or node assignment" in inventory
    assert "Manage runtime secrets and environment settings" in inventory
    assert "Pause or drain a node" in inventory
    assert "[Host and service logs](log-viewer.md)" in inventory
    assert "A console viewer for host/service logs is Proposed" not in inventory
    assert "no invented TESTFLOW keys" in inventory


def test_agents_states_ui_principle_and_deployment_readme_links_inventory():
    agents = (ROOT / "AGENTS.md").read_text(encoding="utf-8")
    readme = (ROOT / "deploy/v4/README.md").read_text(encoding="utf-8")
    topics = readme.split("## Topics", maxsplit=1)[1].split("## ", maxsplit=1)[0]

    assert "Every new feature for end users must have a UI path." in agents
    assert "CLI is only for developers and CI." in agents
    assert "[No CLI for end users](../../docs/v4/no-cli.md)" in topics
