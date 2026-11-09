from __future__ import annotations

import shlex
import sys
import threading
from contextlib import contextmanager
from types import SimpleNamespace

from aiflow.v4.control import load_policy
from aiflow.v4.control.repositories import nodes as node_repository
from aiflow.v4.worker import gate_step, post_merge, suite_slots, version_release


def test_test_slot_limit_defaults_to_two_and_rejects_invalid_files(tmp_path):
    assert suite_slots.slot_limit(tmp_path) == 2
    (tmp_path / "limit").write_text("0")
    assert suite_slots.slot_limit(tmp_path) == 2
    (tmp_path / "limit").write_text("33")
    assert suite_slots.slot_limit(tmp_path) == 2
    (tmp_path / "limit").write_text("3")
    assert suite_slots.slot_limit(tmp_path) == 3


def test_control_suite_limit_defaults_and_rejects_invalid_values():
    assert load_policy.configured_test_suite_slots(None) == 2
    assert load_policy.configured_test_suite_slots(4) == 4
    assert load_policy.configured_test_suite_slots(True) == 2
    assert load_policy.configured_test_suite_slots(33) == 2


def test_node_repository_reads_machine_load_settings_by_name():
    class FakeDb:
        def one(self, query, params):
            assert "test_suite_slots" in query and "removed_at IS NULL" in query
            assert params == ("node-test",)
            return {"metrics": {}, "test_suite_slots": 4}

    assert node_repository.machine_load_settings(FakeDb(), "node-test") == {
        "metrics": {}, "test_suite_slots": 4}


def test_node_repository_saves_machine_load_settings_by_name():
    class FakeDb:
        def run(self, query, params):
            assert "UPDATE nodes SET test_suite_slots=%s" in query
            assert params[0] == 4 and params[2] == "node-test"

    node_repository.save_machine_load_settings(FakeDb(), "node-test", {"cpu_load_per_cpu": 0.9}, 4)


def test_node_repository_reads_heartbeat_settings_by_name():
    class FakeDb:
        def one(self, query, params):
            assert "SELECT projects,dynamic,test_suite_slots" in query
            assert params == ("node-test",)
            return {"projects": ["TESTWP"], "dynamic": False, "test_suite_slots": 4}

    assert node_repository.heartbeat_settings(FakeDb(), "node-test") == {
        "projects": ["TESTWP"], "dynamic": False, "test_suite_slots": 4}


def test_test_slot_waits_until_an_active_suite_releases_its_slot(tmp_path):
    (tmp_path / "limit").write_text("1")
    first_entered, release_first = threading.Event(), threading.Event()
    second_started, second_entered = threading.Event(), threading.Event()

    def first_suite():
        with suite_slots.suite_slot(tmp_path):
            first_entered.set()
            assert release_first.wait(2)

    def second_suite():
        second_started.set()
        with suite_slots.suite_slot(tmp_path):
            second_entered.set()

    first = threading.Thread(target=first_suite)
    second = threading.Thread(target=second_suite)
    first.start()
    assert first_entered.wait(1)
    second.start()
    assert second_started.wait(1)
    assert not second_entered.wait(0.15)
    release_first.set()
    assert second_entered.wait(1)
    first.join(timeout=1)
    second.join(timeout=1)
    assert not first.is_alive() and not second.is_alive()


def test_gate_requests_a_slot_before_running_the_suite(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def slot(_directory=None):
        events.append("slot acquired")
        yield
        events.append("slot released")

    def run(context, steps, _env, *, log=None):
        events.append("suite started")
        assert context.run_as == ("nice", "-n", "10", "setpriv", "--")
        assert [step.argv for step in steps] == [("true",)]
        return gate_step.local_gate.GateResult()

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(gate_step.local_gate, "run", run)

    gate_step.run(tmp_path, {"local_gate": ["true"]}, "test", run_as=["setpriv", "--"])

    assert events == ["slot acquired", "suite started", "slot released"]


def test_gate_suite_processes_run_with_a_lower_nice_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFLOW4_TEST_SLOT_DIR", str(tmp_path / "slots"))
    code = "import os; raise SystemExit(0 if os.nice(0) == 10 else 1)"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    result = gate_step.run(tmp_path, {"local_gate": [command]}, "test", run_as=[])

    assert result.exit_code == 0


def test_version_release_check_process_runs_with_a_lower_nice_priority(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFLOW4_TEST_SLOT_DIR", str(tmp_path / "slots"))
    events = []

    @contextmanager
    def slot():
        events.append("slot acquired")
        yield
        events.append("slot released")

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    code = "import os; print(os.nice(0))"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    exit_code, output = version_release._run_check(command, {}, tmp_path, [], {}, str(tmp_path), 10)

    assert exit_code == 0 and output.strip() == "10"
    assert events == ["slot acquired", "slot released"]


def test_post_merge_check_waits_for_a_slot_and_runs_at_lower_priority(monkeypatch, tmp_path):
    events = []

    @contextmanager
    def slot():
        events.append("slot acquired")
        yield
        events.append("slot released")

    class FakeHeartbeat:
        ident = None

        def __init__(self, *_args):
            self.lost = threading.Event()
            self.stop = threading.Event()

        def start(self):
            pass

    class FakeGit:
        def __init__(self, *_args, **_kwargs):
            pass

        def mirror(self, *_args):
            return "mirror"

        def remote_branch_exists(self, *_args):
            return True

        def detached(self, *_args):
            return "head"

    class FakeProcess:
        pid = 123
        returncode = 0

        def communicate(self, **_kwargs):
            events.append("process collected")
            return "ok\n", None

    class FakeApi:
        def call(self, _method, path, _body):
            if path == "/v1/github/token":
                return {"token": "token"}
            return {"results": [{"status": "passed"}]}

    def popen(command, **_kwargs):
        events.append(("process started", command))
        return FakeProcess()

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(post_merge, "Heartbeat", FakeHeartbeat)
    monkeypatch.setattr(post_merge, "Git", FakeGit)
    monkeypatch.setattr(post_merge, "project_env", lambda *_args: {})
    monkeypatch.setattr(post_merge.subprocess, "Popen", popen)
    command = "echo ok"
    project = {"key": "DEMO", "repo": "example/demo", "base": "main", "checks": [command],
               "post_merge_criteria": [{"check": command}]}
    claim = SimpleNamespace(project=project, ticket={"key": "DEMO-1"}, run_id="run-1", fence=1)

    result = post_merge.run_post_merge(FakeApi(), claim, str(tmp_path), 10, ["setpriv", "--"])

    assert result["outcome"] == "done"
    assert events == [
        "slot acquired",
        ("process started", ["nice", "-n", "10", "setpriv", "--", "bash", "-lc", command]),
        "process collected",
        "slot released",
    ]


def test_version_release_check_inside_a_container_runtime_holds_a_slot(monkeypatch, tmp_path):
    import aiflow.v4.worker.containers as containers

    events = []

    @contextmanager
    def slot():
        events.append("slot")
        yield

    class Runtime:
        def __init__(self, spec, run_as):
            events.append(("runtime", spec.cwd))

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

        def bind_process(self, pid):
            events.append("bound")

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(containers, "ContainerRuntime", Runtime)

    exit_code, output = version_release._run_check("echo contained", {"containers": True}, tmp_path, [], {},
                                                   str(tmp_path), 10)

    assert exit_code == 0 and output.strip() == "contained"
    assert events == ["slot", ("runtime", str(tmp_path)), "bound"]


def test_version_release_check_that_exceeds_its_timeout_is_killed_and_reported(monkeypatch, tmp_path):
    monkeypatch.setenv("AIFLOW4_TEST_SLOT_DIR", str(tmp_path / "slots"))

    exit_code, output = version_release._run_check("sleep 30", {}, tmp_path, [], {"PATH": "/usr/bin:/bin"},
                                                   str(tmp_path), 1)

    assert exit_code != 0 and "Check exceeded 1 seconds." in output


def test_node_agent_reports_a_missing_worker_account_when_publishing_the_slot_limit(monkeypatch, tmp_path):
    import pytest
    from aiflow.v4.node import agent

    def missing(_name):
        raise KeyError("aiflow-worker")

    monkeypatch.setattr(agent.pwd, "getpwnam", missing)

    with pytest.raises(OSError, match="aiflow-worker account is missing"):
        agent._write_test_suite_slots(3, tmp_path / "slots")
