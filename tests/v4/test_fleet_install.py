import importlib.machinery
import importlib.util
import json
import shutil
import stat
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml

from conftest import join_node


ROOT = Path(__file__).resolve().parents[2]
ANSIBLE = ROOT / "deploy/v4/ansible"
CLOUD_INIT = ROOT / "deploy/v4/cloud-init"
BOOTSTRAP = ROOT / "deploy/v4/bin/aiflow4-bootstrap"
PROVIDERS = ("proxmox", "virtualbox", "cloud")


def ansible_tool(executable: str, package: str):
    """Return argv for an installed Ansible tool, or one already in the uv cache; None skips the test."""
    candidates = []
    if shutil.which(executable):
        candidates.append([executable])
    if shutil.which("uvx"):
        candidates.append(["uvx", "--offline", "--from", package, executable])
    for argv in candidates:
        try:
            probe = subprocess.run([*argv, "--version"], capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return argv
    return None


def test_playbook_passes_ansible_syntax_check():
    argv = ansible_tool("ansible-playbook", "ansible-core")
    if argv is None:
        pytest.skip("ansible-playbook is not installed")
    result = subprocess.run([*argv, "-i", "inventory.example.yml", "--syntax-check", "playbook.yml"],
                            cwd=ANSIBLE, capture_output=True, text=True, timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr


def test_playbook_and_roles_pass_ansible_lint():
    argv = ansible_tool("ansible-lint", "ansible-lint")
    if argv is None:
        pytest.skip("ansible-lint is not installed")
    result = subprocess.run([*argv, "--offline", "playbook.yml", "roles"],
                            cwd=ANSIBLE, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, result.stdout + result.stderr


def test_join_token_request_runs_on_the_runner_without_sudo():
    tasks = yaml.safe_load((ANSIBLE / "roles/aiflow4_node/tasks/main.yml").read_text())
    task = next(t for t in tasks if t["name"] == "Request one-use enrollment token from the control plane")
    assert task["delegate_to"] == "localhost"
    assert task["become"] is False
    assert task["no_log"] is True


def run_prepared_bootstrap(tmp_path, php_version):
    """Run the bootstrap up to the token redeem with stub curl/id/php and only the needed real tools on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    temp_dir = tmp_path / "tmp"
    temp_dir.mkdir()
    for tool in ("python3", "mktemp", "chmod", "cat", "rm"):
        (bin_dir / tool).symlink_to(shutil.which(tool))
    stubs = {
        "id": "#!/bin/sh\necho 0\n",
        "curl": ("#!/bin/sh\nout=; url=\n"
                 "while [ $# -gt 0 ]; do case \"$1\" in -o) out=$2; shift 2;; -H|--max-time|--data-binary) shift 2;;"
                 " -*) shift;; *) url=$1; shift;; esac; done\n"
                 "echo \"$url\" >> \"$CURL_LOG\"\n"
                 "case \"$url\" in */inspect) printf '%s' \"$GRANT\" > \"$out\";; *) exit 22;; esac\n"),
    }
    if php_version is not None:
        stubs["php"] = "#!/bin/sh\nprintf '%s' \"$PHP_STUB_VERSION\"\n"
    for name, body in stubs.items():
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    log = tmp_path / "curl.log"
    grant = {"name": "aiflow-node-01", "role": "worker", "project": "TESTWP", "stack": "php-8.3",
             "release": "v4.1.0"}
    env = {"PATH": str(bin_dir), "CURL_LOG": str(log), "GRANT": json.dumps(grant),
           "PHP_STUB_VERSION": php_version or "", "AIFLOW4_SERVER_INSTALLER_PREPARED": "1",
           "TMPDIR": str(temp_dir)}
    result = subprocess.run(["/bin/sh", str(BOOTSTRAP), "--token", "tok"], capture_output=True, text=True,
                            env=env, timeout=60)
    return result, log.read_text().splitlines() if log.exists() else []


@pytest.mark.parametrize(("php_version", "message"), [
    (None, "project TESTWP needs PHP 8.3, but php is not installed; run server-installer with SI_PHP_VERSION=8.3"),
    ("8.2", "project TESTWP needs PHP 8.3, but php is 8.2; run server-installer with SI_PHP_VERSION=8.3"),
])
def test_prepared_bootstrap_refuses_php_project_without_matching_php_before_redeeming(tmp_path, php_version,
                                                                                     message):
    result, calls = run_prepared_bootstrap(tmp_path, php_version)
    assert result.returncode == 1
    assert message in result.stderr
    # The token was only inspected, never redeemed, so the operator can fix the inventory and rerun.
    assert [c.rsplit("/v1/", 1)[1] for c in calls] == ["nodes/join-tokens/inspect"]


def test_prepared_bootstrap_with_matching_php_goes_on_to_redeem(tmp_path):
    result, calls = run_prepared_bootstrap(tmp_path, "8.3")
    assert "needs PHP" not in result.stderr
    assert [c.rsplit("/v1/", 1)[1] for c in calls] == ["nodes/join-tokens/inspect", "nodes/join"]


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


def load_renderer():
    path = ROOT / "deploy/v4/bin/aiflow4-cloud-init-user-data"
    loader = importlib.machinery.SourceFileLoader("aiflow4_cloud_init_user_data", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


def test_cloud_init_templates_deliver_a_per_vm_token_file_and_stop_when_it_is_missing():
    for provider in PROVIDERS:
        source = (CLOUD_INIT / provider / "user-data.yaml").read_text()
        config = yaml.safe_load(source)
        assert source.startswith("#cloud-config")
        assert config["hostname"] == "__AIFLOW4_NODE_NAME__"
        assert config["write_files"] == [{"path": "/run/aiflow4/join-token", "owner": "root:root",
                                          "permissions": "0600", "content": "__AIFLOW4_JOIN_TOKEN__"}]
        # A single `sh -e` entry: a missing token or failed download cannot fall through to later steps.
        assert len(config["runcmd"]) == 1
        argv = config["runcmd"][0]
        assert argv[:2] == ["sh", "-ec"]
        lines = argv[2].splitlines()
        assert lines[0].startswith("[ -s /run/aiflow4/join-token ] ||") and "exit 1" in lines[0]
        assert lines[1].startswith("curl -fsSL") and "__AIFLOW4_CONTROL_URL__/v1/install.sh" in lines[1]
        assert lines[2] == "sh /run/aiflow4/install.sh --token-file /run/aiflow4/join-token"
        assert "/run/secrets" not in source


def test_cloud_init_runcmd_exits_before_download_when_token_file_is_missing(tmp_path):
    config = yaml.safe_load((CLOUD_INIT / "proxmox/user-data.yaml").read_text())
    script = config["runcmd"][0][2].replace("/run/aiflow4", str(tmp_path))
    result = subprocess.run(["/bin/sh", "-ec", script], capture_output=True, text=True, env={"PATH": str(tmp_path)})
    assert result.returncode == 1
    assert "join token missing" in result.stderr


def test_user_data_renderer_mints_a_redeemable_token_into_a_private_per_vm_file(client, monkeypatch, tmp_path):
    renderer = load_renderer()
    monkeypatch.setenv("AIFLOW4_PROVISION_KEY", "join-secret")
    minted = []

    def mint(control_url, provision_key, name, role, project):
        response = client.post("/v1/nodes/join-tokens", json={"provision_key": provision_key, "name": name,
                                                               "role": role, "project": project})
        assert response.status_code == 200, response.text
        minted.append(response.json()["token"])
        return response.json()

    for provider in PROVIDERS:
        out = tmp_path / f"{provider}.yaml"
        assert renderer.main(["--provider", provider, "--name", f"vm-{provider}", "--project", "TESTWP",
                              "--control-url", "https://control.example.test/", "--out", str(out)],
                             mint=mint) == 0
        assert stat.S_IMODE(out.stat().st_mode) == 0o600
        rendered = out.read_text()
        assert "__AIFLOW4_" not in rendered
        config = yaml.safe_load(rendered)
        assert config["hostname"] == f"vm-{provider}"
        token = config["write_files"][0]["content"]
        assert token == minted[-1]
        assert "https://control.example.test/v1/install.sh" in config["runcmd"][0][2]
        inspect = client.post("/v1/nodes/join-tokens/inspect", json={"join_token": token})
        assert inspect.status_code != 403, inspect.text

    monkeypatch.delenv("AIFLOW4_PROVISION_KEY")
    assert renderer.main(["--provider", "cloud", "--name", "vm", "--project", "TESTWP", "--control-url",
                          "https://c", "--out", str(tmp_path / "none")], mint=mint) == 2
    assert not (tmp_path / "none").exists()
    with pytest.raises(ValueError):
        renderer.render("x", token="tok'en: injected-value-xx", name="vm", control_url="https://c")


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
        response = join_node(client, name, role="worker", project="TESTWP", heartbeat=False)
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

