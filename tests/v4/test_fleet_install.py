from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import yaml

from conftest import join_node


ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy/v4/ansible"
CLOUD_INIT = ROOT / "deploy/v4/cloud-init"


def test_sample_inventory_carries_ten_nodes_and_all_fleet_settings():
    inventory = yaml.safe_load((ANSIBLE / "inventory.example.yml").read_text())
    all_vars = inventory["all"]["vars"]
    nodes = inventory["all"]["children"]["aiflow4_nodes"]["hosts"]

    assert len(nodes) == 10
    assert all_vars["aiflow4_node_server_installer_tag"] == "v1.1.0"
    for host, values in nodes.items():
        assert values["aiflow4_node_name"] == host
        assert values["aiflow4_node_role"] == "worker"
        assert values["aiflow4_node_project"] == "TESTWP"
        assert values["aiflow4_node_cpu_limit"] > 0
        assert values["aiflow4_node_memory_limit"]
        assert values["aiflow4_node_slots"] > 0
        assert values["aiflow4_node_si_environment"] == {"SI_PHP_VERSION": "8.3", "SI_PHP_MODE": "cli"}


def test_cloud_init_samples_call_the_one_use_ai_flow_join_bootstrap():
    paths = (
        CLOUD_INIT / "proxmox/user-data.yaml",
        CLOUD_INIT / "virtualbox/user-data.yaml",
        CLOUD_INIT / "cloud/user-data.yaml",
    )
    assert len(paths) == 3
    for path in paths:
        source = path.read_text()
        config = yaml.safe_load(source)
        assert source.startswith("#cloud-config")
        assert config["hostname"] == "aiflow-node-01"
        assert config["runcmd"][0] == ["test", "-r", "/run/secrets/aiflow4/join-token"]
        assert config["runcmd"][1][0:2] == ["curl", "-fsSL"]
        assert config["runcmd"][1][2].endswith("/v1/install.sh")
        assert config["runcmd"][2] == [
            "sh", "/run/aiflow4-install.sh", "--token-file", "/run/secrets/aiflow4/join-token"
        ]
        assert "join_token:" not in source


def test_ansible_roles_wrap_the_pinned_server_installer_and_ai_flow_bootstrap():
    node_tasks = yaml.safe_load((ANSIBLE / "roles/aiflow4_node/tasks/main.yml").read_text())
    control_tasks = yaml.safe_load((ANSIBLE / "roles/aiflow4_control/tasks/main.yml").read_text())
    node_source = (ANSIBLE / "roles/aiflow4_node/tasks/main.yml").read_text()
    control_source = (ANSIBLE / "roles/aiflow4_control/tasks/main.yml").read_text()
    node_defaults = (ANSIBLE / "roles/aiflow4_node/defaults/main.yml").read_text()
    control_defaults = (ANSIBLE / "roles/aiflow4_control/defaults/main.yml").read_text()
    bootstrap = (ROOT / "deploy/v4/bin/aiflow4-bootstrap").read_text()

    for source in (node_source, control_source):
        assert "server_installer_tag == 'v1.1.0'" in source
        assert "--preflight" in source
        assert "ansible.builtin.apt" not in source
        assert "ansible.builtin.package" not in source

    for defaults in (node_defaults, control_defaults):
        assert "server-installer-1.1.0/bin/server-installer" in defaults

    assert any("Prepare node OS through server-installer" == task["name"] for task in node_tasks)
    assert any("Prepare control host OS through server-installer" == task["name"] for task in control_tasks)
    assert "SI_PHP_VERSION" in node_source and "SI_PHP_MODE" in (ANSIBLE / "inventory.example.yml").read_text()
    assert "AIFLOW4_SERVER_INSTALLER_PREPARED" in node_source
    assert "--token-file" in bootstrap
    assert "SERVER_INSTALLER_TAG=v1.1.0" in bootstrap
    assert "aiflow-worker" in bootstrap and "npm install --global" in bootstrap


def test_node_role_skips_enrollment_when_installed_identity_matches_and_applies_limits():
    source = (ANSIBLE / "roles/aiflow4_node/tasks/main.yml").read_text()
    agent_limits = (ANSIBLE / "roles/aiflow4_node/templates/node-agent-limits.conf.j2").read_text()
    worker_limits = (ANSIBLE / "roles/aiflow4_node/templates/worker-limits.conf.j2").read_text()
    slice_limits = (ANSIBLE / "roles/aiflow4_node/templates/node-limits.slice.j2").read_text()

    assert "aiflow4_node_is_joined" in source
    assert "AIFLOW4_NODE_NAME" in source
    assert "when: not aiflow4_node_is_joined | bool" in source
    assert "AIFLOW4_SLOTS" in source
    assert "Slice=aiflow4-node.slice" in agent_limits
    assert "Slice=aiflow4-node.slice" in worker_limits
    assert "CPUQuota=" in slice_limits and "aiflow4_node_cpu_limit" in slice_limits
    assert "MemoryMax={{ aiflow4_node_memory_limit }}" in slice_limits


def test_one_inventory_joins_ten_nodes_concurrently_and_rejoin_keeps_one_record_per_node(client, ctx):
    inventory = yaml.safe_load((ANSIBLE / "inventory.example.yml").read_text())
    nodes = inventory["all"]["children"]["aiflow4_nodes"]["hosts"]
    names = [values["aiflow4_node_name"] for values in nodes.values()]

    def join_one(name):
        response = join_node(client, name, role="worker", project="TESTWP")
        assert response.status_code == 200, response.text
        return response.json()["node"]

    with ThreadPoolExecutor(max_workers=10) as pool:
        joined = list(pool.map(join_one, names))
    assert sorted(joined) == sorted(names)

    # Exercise the same inventory again; the API upserts each identity and never inserts a duplicate node.
    with ThreadPoolExecutor(max_workers=10) as pool:
        joined_again = list(pool.map(join_one, names))
    assert sorted(joined_again) == sorted(names)
    count = ctx.db.one("SELECT count(*) AS count FROM nodes WHERE name = ANY(%s)", (names,))["count"]
    assert count == 10


def test_fleet_docs_propose_server_installer_profiles_and_topics_link():
    docs = (ROOT / "docs/v4/fleet-install.md").read_text()
    deploy_readme = (ROOT / "deploy/v4/README.md").read_text()

    assert "`aiflow-control`" in docs and "`aiflow-node`" in docs
    assert "jooservices/server-installer" in docs
    assert "Creating either profile is a change for the server-installer owner/lead, not this repository." in docs
    assert "[Fleet installation](../../docs/v4/fleet-install.md)" in deploy_readme

