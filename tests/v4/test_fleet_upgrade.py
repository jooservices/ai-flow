"""Fleet upgrades (TESTFLOW-73): canary and waves, halt and rollback, version skew policy, forward-only migrations
and the pre-upgrade check of the control plane. docs/v4/fleet-upgrade.md."""
import hashlib
import shutil
from pathlib import Path

import pytest

from conftest import add_ticket, join_node
from aiflow.v4.control import api as control_api
from aiflow.v4.control import db as control_db
from aiflow.v4.control import rollout
from aiflow.v4.control.loops import fleet_rollout, watchdog
from aiflow.v4.node import agent

OLD, NEW = "v4.2.0", "v4.3.0"
REPO = Path(__file__).resolve().parents[2]


def join(client, name):
    r = join_node(client, name)
    assert r.status_code == 200, r.text
    return {"Authorization": f"Bearer {r.json()['node_token']}"}


def beat(client, name, headers, version):
    r = client.post(f"/v1/nodes/{name}/heartbeat", json={"slots_total": 2, "release_version": version,
                                                         "release_protocol": 1}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()["desired"]["release"]


@pytest.fixture
def fleet(client, ctx, tmp_path, monkeypatch):
    """Four signed-release nodes on OLD; CURRENT names OLD. Soak 0 s so a reported release passes at once."""
    releases = tmp_path / "releases"
    releases.mkdir()
    (releases / "CURRENT").write_text(OLD + "\n")
    monkeypatch.setattr(control_api, "RELEASES", releases)
    ctx.settings.rollout_batch_size = 2
    ctx.settings.rollout_soak_s = 0
    ctx.settings.rollout_install_timeout_s = 600
    ctx.settings.rollout_canary_node = ""
    auth = {name: join(client, name) for name in ("cc", "node1", "node2", "node3")}
    for name, h in auth.items():
        assert beat(client, name, h, OLD) == OLD

    class Fleet:
        headers = auth
        version = {name: OLD for name in auth}

        def release(self, version):
            (releases / "CURRENT").write_text(version + "\n")

        def beat_all(self):
            return {name: beat(client, name, h, self.version[name]) for name, h in auth.items()}

        def install(self, *names, version=NEW):
            for name in names:
                self.version[name] = version
                beat(client, name, auth[name], version)

    return Fleet()


def member_status(ctx):
    r = rollout.latest(ctx.db)
    return {m["node"]: (m["wave"], m["status"]) for m in rollout.members(ctx.db, r["id"])}


# --- AC1, AC2: canary first, then waves of the configured size --------------------------------------------------
def test_new_release_goes_to_one_canary_then_waves(client, ctx, fleet):
    fleet.release(NEW)
    assert set(fleet.beat_all().values()) == {OLD}  # nobody is a member before the rollout picked its canary
    fleet_rollout(ctx)
    r = rollout.latest(ctx.db)
    assert (r["target"], r["previous"], r["status"]) == (NEW, OLD, "canary")
    desired = fleet.beat_all()
    canary = [n for n, v in desired.items() if v == NEW]
    assert canary == ["cc"]  # exactly one node; the first by name
    assert all(v == OLD for n, v in desired.items() if n != "cc")

    # the canary has not installed yet: the others keep the old release, however often the loop runs
    fleet_rollout(ctx)
    assert [n for n, v in fleet.beat_all().items() if v == NEW] == ["cc"]
    fleet.install("cc")
    fleet_rollout(ctx)  # reported and soaked: healthy, wave 1 starts with two nodes
    assert member_status(ctx)["cc"] == (0, "healthy")
    assert rollout.latest(ctx.db)["status"] == "rolling"
    desired = fleet.beat_all()
    assert desired == {"cc": NEW, "node1": NEW, "node2": NEW, "node3": OLD}

    # the next wave waits for every node of this one
    fleet.install("node1")
    fleet_rollout(ctx)
    assert fleet.beat_all()["node3"] == OLD
    assert member_status(ctx)["node2"] == (1, "assigned")
    fleet.install("node2")
    fleet_rollout(ctx)
    assert fleet.beat_all()["node3"] == NEW
    fleet.install("node3")
    fleet_rollout(ctx)
    r = rollout.latest(ctx.db)
    assert r["status"] == "completed" and r["wave"] == 2
    assert set(fleet.beat_all().values()) == {NEW}


def test_soak_time_is_part_of_the_health_check_and_batch_size_is_configurable(client, ctx, fleet):
    ctx.settings.rollout_batch_size = 3
    ctx.settings.rollout_soak_s = 3600
    ctx.settings.rollout_canary_node = "node2"
    fleet.release(NEW)
    fleet_rollout(ctx)
    assert [n for n, v in fleet.beat_all().items() if v == NEW] == ["node2"]  # the configured canary
    fleet.install("node2")
    fleet_rollout(ctx)
    assert member_status(ctx)["node2"] == (0, "assigned")  # reported, but not healthy for long enough
    ctx.db.run("UPDATE fleet_rollout_nodes SET upgraded_at=now() - interval '2 hours'")
    fleet_rollout(ctx)
    assert fleet.beat_all() == {"cc": NEW, "node1": NEW, "node2": NEW, "node3": NEW}  # one wave of three


def test_a_node_already_on_the_target_joins_the_wave_instead_of_being_sent_back(client, ctx, fleet):
    fleet.release(NEW)
    # the control host's own agent was installed by aiflow4-deploy before CURRENT changed
    assert beat(client, "node3", fleet.headers["node3"], NEW) == NEW
    fleet.version["node3"] = NEW
    ctx.settings.rollout_soak_s = 3600
    fleet_rollout(ctx)
    assert member_status(ctx) == {"node3": (0, "assigned")}  # it is the canary, nobody else got NEW
    assert fleet.beat_all() == {"cc": OLD, "node1": OLD, "node2": OLD, "node3": NEW}


# --- AC3, AC4: a failed health check halts the rollout and rolls the members back --------------------------------
def _to_wave_one(ctx, fleet):
    fleet.release(NEW)
    fleet_rollout(ctx)
    fleet.install("cc")
    fleet_rollout(ctx)
    assert fleet.beat_all() == {"cc": NEW, "node1": NEW, "node2": NEW, "node3": OLD}


def test_install_timeout_halts_the_rollout_and_rolls_back(client, ctx, fleet):
    _to_wave_one(ctx, fleet)
    fleet.install("node1")
    ctx.db.run("UPDATE fleet_rollout_nodes SET assigned_at=now() - interval '1 hour' WHERE node='node2'")
    fleet_rollout(ctx)
    r = rollout.latest(ctx.db)
    assert (r["status"], r["failed_node"], r["rollback"]) == ("halted", "node2", "pending")
    assert "did not report v4.3.0 within 600 s" in r["reason"]
    # nobody receives the new release any more; the upgraded nodes are told to go back
    assert set(fleet.beat_all().values()) == {OLD}
    incident = ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind='fleet_rollout' AND subject=%s", (NEW,))
    assert incident["severity"] == "critical"
    assert "node node2" in incident["title"] and "v4.3.0" in incident["title"]
    assert "previous v4.2.0" in incident["detail"] and "Rollback result: pending" in incident["detail"]

    fleet.install("cc", "node1", version=OLD)  # the agents reinstall the previous signed release
    fleet_rollout(ctx)
    r = rollout.latest(ctx.db)
    assert r["rollback"] == "succeeded"
    assert set(s for _, s in member_status(ctx).values()) == {"rolled_back"}
    done = ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind='fleet_rollback' AND subject=%s", (NEW,))
    assert done["title"] == "rollback of v4.3.0 succeeded"
    assert "Failed node: node2" in done["detail"] and "node1: rolled_back (to v4.2.0)" in done["detail"]
    assert "Rollback result: succeeded" in ctx.db.one(
        "SELECT detail FROM incidents WHERE kind='fleet_rollout' AND subject=%s", (NEW,))["detail"]
    # the halted release stays off the fleet until CURRENT names another one
    fleet_rollout(ctx)
    assert set(fleet.beat_all().values()) == {OLD}


def test_lost_heartbeat_in_the_canary_halts_and_a_stuck_rollback_is_reported_failed(client, ctx, fleet):
    fleet.release(NEW)
    fleet_rollout(ctx)
    fleet.install("cc")
    ctx.settings.rollout_soak_s = 3600
    fleet_rollout(ctx)
    ctx.db.run("UPDATE nodes SET last_heartbeat=now() - interval '10 minutes' WHERE name='cc'")
    fleet_rollout(ctx)
    r = rollout.latest(ctx.db)
    assert r["status"] == "halted" and r["failed_node"] == "cc" and "lost its heartbeat" in r["reason"]
    assert all(beat(client, n, fleet.headers[n], OLD) == OLD for n in ("node1", "node2", "node3"))
    # the canary never comes back on the previous release
    ctx.db.run("UPDATE fleet_rollout_nodes SET rollback_at=now() - interval '1 hour'")
    fleet_rollout(ctx)
    assert rollout.latest(ctx.db)["rollback"] == "failed"
    failed = ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind='fleet_rollback' AND subject=%s", (NEW,))
    assert failed["severity"] == "critical" and failed["title"] == "rollback of v4.3.0 FAILED"
    assert "cc: rollback_failed" in failed["detail"]
    # it comes back later and takes the previous release: the rollback is complete after all
    fleet.install("cc", version=OLD)
    fleet_rollout(ctx)
    assert rollout.latest(ctx.db)["rollback"] == "succeeded"
    assert not ctx.db.one("SELECT 1 FROM incidents WHERE kind='fleet_rollback' AND closed_at IS NULL")


def test_rejected_install_halts_the_rollout(client, ctx, fleet):
    _to_wave_one(ctx, fleet)
    r = client.post("/v1/nodes/node1/release-failures", headers=fleet.headers["node1"],
                    json={"release_version": NEW, "detail": "installer: release signature verification failed"})
    assert r.status_code == 200
    r = rollout.latest(ctx.db)
    assert r["status"] == "halted" and r["failed_node"] == "node1" and "rejected the release" in r["reason"]
    assert set(fleet.beat_all().values()) == {OLD}
    fleet.install("cc", version=OLD)
    fleet_rollout(ctx)  # node1 and node2 never left OLD
    assert rollout.latest(ctx.db)["rollback"] == "succeeded"


def test_a_node_that_switches_back_after_installing_fails_its_health_check(client, ctx, fleet):
    _to_wave_one(ctx, fleet)
    ctx.settings.rollout_soak_s = 3600
    fleet.install("node1")
    fleet_rollout(ctx)
    fleet.install("node1", version=OLD)  # the installer's own health check switched the symlink back
    fleet_rollout(ctx)
    r = rollout.latest(ctx.db)
    assert r["failed_node"] == "node1" and "switched back to v4.2.0" in r["reason"]


# --- AC5, AC9: a node of a wave fails while a ticket runs on it: rollback, requeue, no ticket lost -------------
def test_failed_upgrade_mid_rollout_requeues_running_tickets_and_loses_none(client, ctx, fleet):
    for key in ("TESTWP-1", "TESTWP-2"):
        add_ticket(ctx.db, key)
    # an earlier dev run left a branch on TESTWP-1
    ctx.db.run("INSERT INTO runs (id, ticket_key, project, step, account_id, node, fence, status, outcome, branch,"
               " started_at, ended_at) VALUES (gen_random_uuid(),'TESTWP-1','TESTWP','dev','codex-01','node1',0,"
               "'finished','done','TESTWP-1-fleet', now() - interval '2 hours', now() - interval '2 hours')")
    on_node1 = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "codex-01"},
                           headers=fleet.headers["node1"]).json()
    on_cc = client.post("/v1/work/claim", json={"ticket": "TESTWP-2", "account": "claude-01"},
                        headers=fleet.headers["cc"]).json()
    assert on_node1["claimed"] and on_cc["claimed"]
    for key, run, node in (("TESTWP-1", on_node1, "node1"), ("TESTWP-2", on_cc, "cc")):
        assert client.post(f"/v1/jira/{key}/transition", json={"run_id": run["run_id"], "fence": run["fence"],
                                                                 "to": "In Progress"},
                           headers=fleet.headers[node]).json()["status"] == "In Progress"
    in_progress = {t["key"] for t in ctx.db.all("SELECT key FROM tickets WHERE status='In Progress'")}
    assert in_progress == {"TESTWP-1", "TESTWP-2"}

    _to_wave_one(ctx, fleet)  # cc passed as canary, node1 and node2 are told to install NEW
    # node1's installer restarts its workers and the agent never comes back: the run loses its lease
    ctx.leases.r.delete(f"{ctx.leases.p}lease:TESTWP-1")
    ctx.db.run("UPDATE nodes SET last_heartbeat=now() - interval '10 minutes' WHERE name='node1'")
    fleet.install("node2")
    watchdog(ctx)
    fleet_rollout(ctx)

    r = rollout.latest(ctx.db)
    assert (r["status"], r["failed_node"]) == ("halted", "node1")
    assert set(beat(client, n, fleet.headers[n], fleet.version[n]) for n in ("cc", "node2", "node3")) == {OLD}
    fleet.install("cc", "node2", version=OLD)
    ctx.db.run("UPDATE nodes SET status='online', last_heartbeat=now() WHERE name='node1'")
    assert client.post("/v1/nodes/node1/heartbeat", json={}, headers=fleet.headers["node1"]).status_code == 200
    fleet_rollout(ctx)
    assert rollout.latest(ctx.db)["rollback"] == "succeeded"
    assert ctx.db.one("SELECT title FROM incidents WHERE kind='fleet_rollback'")["title"] == "rollback of v4.3.0 succeeded"

    # the ticket on the failed node is back in To Do, the other keeps running: none lost
    assert ctx.jira.status_of["TESTWP-1"] == "To Do"
    assert ctx.db.one("SELECT status FROM runs WHERE id=%s", (on_node1["run_id"],))["status"] == "lost"
    assert any(k == "TESTWP-1" and "keeps its branch" in text for k, text in ctx.jira.comments)
    for key in in_progress:
        status = ctx.db.one("SELECT status FROM tickets WHERE key=%s", (key,))["status"]
        running = ctx.db.one("SELECT id, fence FROM runs WHERE ticket_key=%s AND status='running'", (key,))
        valid = running and ctx.leases.holder(key) == f"{running['id']}:{running['fence']}"
        assert (status == "In Progress" and valid) or status == "To Do", key
    assert ctx.db.one("SELECT status FROM tickets WHERE key='TESTWP-2'")["status"] == "In Progress"
    # the next run continues the branch (once the usual retry backoff after a lost run has passed)
    ctx.db.run("UPDATE runs SET ended_at=now() - interval '1 hour' WHERE id=%s", (on_node1["run_id"],))
    again = client.post("/v1/work/claim", json={"ticket": "TESTWP-1", "account": "codex-01"},
                        headers=fleet.headers["node1"]).json()
    assert again["claimed"] and again["branch"] == "TESTWP-1-fleet", again


# --- AC6: version skew policy -----------------------------------------------------------------------------------
def test_skew_policy_allows_one_minor_behind_and_nothing_ahead():
    assert rollout.skew("v4.3.0", "v4.3.7")["compatible"]
    assert rollout.skew("v4.3.0", "v4.2.0")["compatible"]
    behind = rollout.skew("v4.3.0", "v4.1.9")
    assert not behind["compatible"] and behind["state"] == "behind" and "2 minors behind" in behind["reason"]
    assert rollout.skew("v4.3.0", "v4.1.9", max_minor_lag=2)["compatible"]
    assert rollout.skew("v4.3.0", "v4.4.0")["state"] == "ahead"
    assert rollout.skew("v5.0.0", "v4.9.0")["state"] == "major"
    assert rollout.skew("v4.3.0", "")["state"] == "unknown"
    assert rollout.skew("4.0.0a1", "v1.0.0")["state"] == "off"


def test_incompatible_node_gets_no_work_and_is_flagged(client, ctx, fleet):
    ctx.settings.control_version = NEW
    add_ticket(ctx.db, "TESTWP-5")
    fleet.install("cc", version="v4.1.0")
    claim = client.post("/v1/work/claim", json={"ticket": "TESTWP-5", "account": "claude-01"}, headers=fleet.headers["cc"])
    assert claim.status_code == 409 and "version skew" in claim.json()["reason"]
    cands = client.get("/v1/work/candidates", params={"account": "claude-01"}, headers=fleet.headers["cc"]).json()
    assert cands["tickets"] == [] and "2 minors behind" in cands["reason"]
    watchdog(ctx)
    incident = ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind='version_skew' AND closed_at IS NULL")
    assert incident["title"] == "node cc is incompatible with the control plane" and "v4.1.0" in incident["detail"]
    from aiflow.v4.control.state import snapshot
    nodes = {n["name"]: n for n in snapshot(ctx)["nodes"]}
    assert nodes["cc"]["version_policy"]["compatible"] is False
    assert nodes["node1"]["version_policy"]["compatible"] is True
    # newer than the control plane is incompatible too
    fleet.install("cc", version="v4.4.0")
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-5", "account": "claude-01"},
                       headers=fleet.headers["cc"]).status_code == 409
    # back inside the policy: work again, the incident closes
    fleet.install("cc", version=OLD)
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-5", "account": "claude-01"},
                       headers=fleet.headers["cc"]).json()["claimed"]
    watchdog(ctx)
    assert not ctx.db.one("SELECT 1 FROM incidents WHERE kind='version_skew' AND closed_at IS NULL")


def test_nodes_inside_the_policy_keep_working_during_a_rollout(client, ctx, fleet):
    ctx.settings.control_version = NEW
    add_ticket(ctx.db, "TESTWP-6")
    _to_wave_one(ctx, fleet)
    # node1 is still on OLD (one minor behind) while its wave runs
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-6", "account": "codex-01"},
                       headers=fleet.headers["node1"]).json()["claimed"]


def test_auto_deploy_canary_may_run_ahead_of_the_control_plane(client, ctx, fleet):
    ctx.settings.control_version = OLD
    (control_api.RELEASES / "CANARY.json").write_text('{"node": "cc", "target": "v4.3.0"}')
    fleet.install("cc", version=NEW)
    add_ticket(ctx.db, "TESTWP-8")
    assert client.post("/v1/work/claim", json={"ticket": "TESTWP-8", "account": "claude-01"},
                       headers=fleet.headers["cc"]).json()["claimed"]


# --- AC7: the pre-upgrade check -----------------------------------------------------------------------------------
def _release_migrations(tmp_path) -> Path:
    new = tmp_path / "new-migrations"
    shutil.copytree(control_db.MIGRATIONS, new)
    return new


def test_upgrade_check_allows_added_migrations(ctx, tmp_path):
    new = _release_migrations(tmp_path)
    (new / "099_add_something.sql").write_text("CREATE TABLE IF NOT EXISTS something (id int);\n")
    result = rollout.check_upgrade(ctx.db, NEW, new, current=OLD)
    assert result["ok"] and result["new_migrations"] == ["099_add_something"]
    assert not ctx.db.one("SELECT to_regclass('public.something') AS t")["t"]  # the check never migrates


@pytest.mark.parametrize("change, reason", [
    ("edit", "was changed after it was applied"),
    ("delete", "is applied but v4.3.0 does not ship it"),
    ("badname", "is not named NNN_*.sql"),
])
def test_upgrade_check_blocks_edited_deleted_or_misnamed_migrations(ctx, tmp_path, change, reason):
    new = _release_migrations(tmp_path)
    if change == "edit":
        (new / "001_registry.sql").write_text((new / "001_registry.sql").read_text() + "\n-- edited\n")
    elif change == "delete":
        (new / "004_canary_flow_bugs.sql").unlink()
    else:
        (new / "add_column.sql").write_text("SELECT 1;\n")
    result = rollout.check_upgrade(ctx.db, NEW, new, current=OLD)
    assert not result["ok"] and any(reason in x for x in result["reasons"])


def test_upgrade_check_compares_with_the_running_release_when_no_checksum_was_recorded(ctx, tmp_path):
    ctx.db.run("UPDATE schema_migrations SET checksum=''")
    new = _release_migrations(tmp_path)
    (new / "002_memory.sql").write_text("-- rewritten\n")
    assert rollout.check_upgrade(ctx.db, NEW, new)["ok"]  # nothing to compare with
    result = rollout.check_upgrade(ctx.db, NEW, new, baseline=control_db.MIGRATIONS)
    assert not result["ok"] and "002_memory differs from the running release" in result["reasons"][0]


def test_upgrade_check_blocks_when_a_node_would_leave_the_skew_policy(client, ctx, fleet, tmp_path):
    fleet.install("node3", version="v4.1.0")
    new = _release_migrations(tmp_path)
    assert rollout.check_upgrade(ctx.db, "v4.2.0", new)["ok"]
    result = rollout.check_upgrade(ctx.db, NEW, new, current=OLD)
    assert not result["ok"]
    assert result["reasons"] == ["node node3 would be outside the version policy on v4.3.0: node runs v4.1.0,"
                                 " 2 minors behind the control plane's v4.3.0 (at most 1 allowed)"]


def test_blocked_upgrade_check_is_recorded_and_exits_9(ctx, tmp_path, monkeypatch, test_database_url, capsys):
    from aiflow.v4 import cli
    new = _release_migrations(tmp_path)
    (new / "003_sa_health.sql").unlink()
    monkeypatch.setenv("AIFLOW4_DATABASE_URL", test_database_url)
    assert cli.main(["upgrade-check", "--target", NEW, "--current", OLD, "--migrations", str(new)]) == 9
    assert '"ok":false' in capsys.readouterr().out
    audit = ctx.db.one("SELECT accepted, result FROM write_audit WHERE action='upgrade_check' ORDER BY id DESC LIMIT 1")
    assert audit["accepted"] is False and "003_sa_health" in audit["result"]
    incident = ctx.db.one("SELECT severity, title, detail FROM incidents WHERE kind='upgrade_blocked' AND closed_at IS NULL")
    assert incident["severity"] == "critical" and incident["title"] == "control upgrade to v4.3.0 blocked"
    assert "keeps running v4.2.0" in incident["detail"]
    assert cli.main(["upgrade-check", "--target", NEW, "--current", OLD]) == 0
    assert not ctx.db.one("SELECT 1 FROM incidents WHERE kind='upgrade_blocked' AND closed_at IS NULL")


def test_deploy_runs_the_check_before_touching_the_control_plane():
    script = (REPO / "deploy/v4/bin/aiflow4-deploy").read_text()
    check = script.index("upgrade-check --target")
    assert check < script.index("docker compose -f") and check < script.index('"$RELEASES/CURRENT.tmp"')
    assert "exit 9" in script[check:script.index("docker compose -f")]
    auto = (REPO / "deploy/v4/bin/aiflow4-autodeploy").read_text()
    assert auto.count('[ "$RC" -ne 9 ] || upgrade_blocked') == 2


# --- AC8: forward-only migrations -------------------------------------------------------------------------------
def test_migrations_record_checksums_and_never_run_backwards(db, tmp_path):
    rows = {r["version"]: r["checksum"] for r in db.all("SELECT version, checksum FROM schema_migrations")}
    assert rows["001_registry"] == hashlib.sha256((control_db.MIGRATIONS / "001_registry.sql").read_bytes()).hexdigest()
    # the previous release ships fewer migrations; after a rollback it starts on the migrated schema
    previous = tmp_path / "previous"
    shutil.copytree(control_db.MIGRATIONS, previous)
    (previous / "007_fleet_rollouts.sql").unlink()
    assert db.migrate(previous) == []
    assert "007_fleet_rollouts" in {r["version"] for r in db.all("SELECT version FROM schema_migrations")}
    assert db.one("SELECT to_regclass('public.fleet_rollouts') AS t")["t"]
    # and its own queries keep working on the newer schema
    db.run("INSERT INTO nodes (name, release_version) VALUES ('old-agent', 'v4.2.0')")
    assert db.one("SELECT release_version FROM nodes WHERE name='old-agent'")["release_version"] == "v4.2.0"


def test_node_rollback_reinstalls_the_previous_release_through_the_verified_path(tmp_path, monkeypatch):
    tarball = b"previous release"
    manifest = {"version": OLD, "sha256": hashlib.sha256(tarball).hexdigest(),
                "tarball": f"/v1/releases/{OLD}.tar.gz", "signature": f"/v1/releases/{OLD}.tar.gz.sigstore.json"}

    class Response:
        status_code = 200

        def __init__(self, content=b"", payload=None):
            self.content, self.payload = content, payload

        def json(self):
            return self.payload

    class API:
        class c:
            @staticmethod
            def get(url, timeout):
                return {f"/v1/releases/{OLD}/manifest": Response(payload=manifest),
                        manifest["tarball"]: Response(tarball), manifest["signature"]: Response(b"{}")}[url]

        def call(self, *args, **kwargs):
            raise AssertionError("nothing to report")

    verified, commands = [], []
    monkeypatch.setattr(agent, "RELEASE_INCOMING_DIR", tmp_path)
    monkeypatch.setattr(agent, "installed_release", lambda: NEW)
    monkeypatch.setattr(agent, "verify_release_signature", lambda tar, bundle, v: verified.append(v) or (True, ""))
    monkeypatch.setattr(agent.subprocess, "run", lambda cmd, **kw: commands.append(cmd) or type("R", (), {
        "returncode": 0, "stderr": "", "stdout": ""})())
    agent._update_tried.clear()
    agent.self_update(API(), OLD, "node1")
    assert verified == [OLD]
    assert commands and commands[0][-4:] == [str(tmp_path / f"aiflow4-{OLD}.tar.gz"), OLD,
                                             str(tmp_path / f"aiflow4-{OLD}.tar.gz.sigstore.json"), manifest["sha256"]]
