from __future__ import annotations

import shlex
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

from aiflow.v4.control import load_policy
from aiflow.v4.control.features.fleet.bodies import MachineLoadLimitsBody
from aiflow.v4.control.features.fleet.events import MachineLoadLimitsSaved
from aiflow.v4.control.repositories import nodes as node_repository
from aiflow.v4.control.load_policy_api import MachineLoadLimitsBody as LoadPolicyMachineLoadLimitsBody
from aiflow.v4.worker import gate_step, post_merge, process_checks, suite_slots, version_release


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


def test_machine_load_limits_body_is_fleet_owned_and_keeps_slot_validation():
    assert LoadPolicyMachineLoadLimitsBody is MachineLoadLimitsBody
    body = MachineLoadLimitsBody(cpu_load_per_cpu=1, memory_used_pct=80, disk_free_pct=10,
                                 test_suite_slots=32)
    assert body.test_suite_slots == 32
    with pytest.raises(ValueError):
        MachineLoadLimitsBody(cpu_load_per_cpu=1, memory_used_pct=80, disk_free_pct=10, test_suite_slots=33)
    with pytest.raises(ValueError):
        MachineLoadLimitsBody(cpu_load_per_cpu=1, memory_used_pct=80, disk_free_pct=10, unexpected=True)


def test_legacy_machine_load_event_keeps_its_existing_audit_shape():
    event = MachineLoadLimitsSaved(node="node-test", actor="owner", machine={"cpu_load_per_cpu": 0.8},
                                   host=None, host_cpu_load_per_cpu=None)

    assert event.audit().payload == {"machine": {"cpu_load_per_cpu": 0.8}, "host": None,
                                     "host_cpu_load_per_cpu": None}


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


def test_test_slot_contention_is_shared_across_processes(tmp_path):
    (tmp_path / "limit").write_text("1")
    first_entered, release_first, second_entered = (tmp_path / name for name in
                                                     ("first-entered", "release-first", "second-entered"))
    first_code = """
import sys, time
from pathlib import Path
from aiflow.v4.worker.suite_slots import suite_slot

directory, entered, release = map(Path, sys.argv[1:])
with suite_slot(directory):
    entered.touch()
    while not release.exists():
        time.sleep(0.01)
"""
    second_code = """
import sys
from pathlib import Path
from aiflow.v4.worker.suite_slots import suite_slot

directory, entered = map(Path, sys.argv[1:])
with suite_slot(directory):
    entered.touch()
"""
    first = subprocess.Popen([sys.executable, "-c", first_code, str(tmp_path), str(first_entered),
                              str(release_first)], cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True)
    second = None
    try:
        deadline = time.monotonic() + 2
        while not first_entered.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert first_entered.is_file(), first.communicate(timeout=1)[1]

        second = subprocess.Popen([sys.executable, "-c", second_code, str(tmp_path), str(second_entered)],
                                  cwd=Path.cwd(), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(0.15)
        assert not second_entered.exists()

        release_first.touch()
        _, second_error = second.communicate(timeout=2)
        _, first_error = first.communicate(timeout=2)
        assert second.returncode == 0, second_error
        assert first.returncode == 0, first_error
        assert second_entered.is_file()
    finally:
        for process in (second, first):
            if process is not None and process.poll() is None:
                process.terminate()
                process.communicate(timeout=1)


@pytest.mark.parametrize("lock_name", ["acquire.lock", "slot-0.lock"])
def test_test_slot_fails_clearly_for_non_regular_lock_paths(tmp_path, lock_name):
    (tmp_path / lock_name).mkdir()

    with pytest.raises(RuntimeError, match="test suite slot lock path is not a regular file"):
        with suite_slots.suite_slot(tmp_path):
            pass


def test_test_slot_rejects_a_lock_that_changes_type_during_open(monkeypatch, tmp_path):
    import stat

    with monkeypatch.context() as scoped:
        scoped.setattr(suite_slots.os, "fstat", lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFDIR))
        with pytest.raises(RuntimeError, match="test suite slot lock path is not a regular file"):
            suite_slots._open_lock(tmp_path / "lock")


def test_test_slot_wait_can_be_cancelled_while_all_slots_are_occupied(tmp_path):
    (tmp_path / "limit").write_text("1")
    checks = iter((False, True))

    with suite_slots.suite_slot(tmp_path):
        with pytest.raises(suite_slots.SlotWaitCancelled, match="slot wait cancelled"):
            with suite_slots.suite_slot(tmp_path, cancel=lambda: next(checks)):
                pytest.fail("cancelled waiter acquired an occupied slot")


def test_test_slot_releases_a_slot_if_cancelled_after_acquisition(tmp_path):
    checks = iter((False, True))

    with pytest.raises(suite_slots.SlotWaitCancelled):
        with suite_slots.suite_slot(tmp_path, cancel=lambda: next(checks)):
            pytest.fail("cancelled waiter entered the slot")

    with suite_slots.suite_slot(tmp_path):
        pass


def test_gate_requests_a_slot_before_running_the_suite(monkeypatch, tmp_path):
    events = []
    outer_slot_directory = tmp_path / "outer-slots"
    real_suite_slot = suite_slots.suite_slot

    @contextmanager
    def slot(_directory=None):
        events.append("slot acquired")
        yield
        events.append("slot released")

    def run(context, steps, env, *, log=None):
        events.append("suite started")
        assert context.run_as == ("nice", "-n", "10", "setpriv", "--")
        assert [step.argv for step in steps] == [("true",)]
        nested_directory = Path(env["AIFLOW4_TEST_SLOT_DIR"])
        assert nested_directory.is_dir() and nested_directory != outer_slot_directory
        with real_suite_slot(nested_directory):
            assert (nested_directory / "acquire.lock").is_file()
        return gate_step.local_gate.GateResult()

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(gate_step.local_gate, "run", run)

    gate_step.run(tmp_path, {"local_gate": ["true"]}, "test",
                  env={"AIFLOW4_TEST_SLOT_DIR": str(outer_slot_directory)}, run_as=["setpriv", "--"])

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
    def slot(*, cancel=None):
        assert cancel is None
        events.append("slot acquired")
        yield
        events.append("slot released")

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    code = "import os; print(os.nice(0))"
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"

    exit_code, output = version_release._run_check(command, {}, tmp_path, [], {}, str(tmp_path), 10)

    assert exit_code == 0 and output.strip() == "10"
    assert events == ["slot acquired", "slot released"]


@pytest.mark.parametrize("lease_loss", [None, "while-waiting", "after-acquire"])
def test_post_merge_check_waits_for_a_slot_and_rechecks_lease(monkeypatch, tmp_path, lease_loss):
    events = []
    lost = threading.Event()

    @contextmanager
    def slot(*, cancel=None):
        assert callable(cancel)
        if lease_loss == "while-waiting":
            lost.set()
            raise suite_slots.SlotWaitCancelled("lost while waiting")
        events.append("slot acquired")
        if lease_loss == "after-acquire":
            lost.set()
        try:
            yield
        finally:
            events.append("slot released")

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
        if lease_loss:
            pytest.fail("project check started after lease loss")
        events.append(("process started", command))
        return FakeProcess()

    heartbeat = SimpleNamespace(lost=lost, stop=threading.Event(), ident=None, start=lambda: None)
    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(post_merge, "Heartbeat", lambda *_args: heartbeat)
    monkeypatch.setattr(post_merge, "Git", FakeGit)
    monkeypatch.setattr(post_merge, "project_env", lambda *_args: {})
    monkeypatch.setattr(post_merge.subprocess, "Popen", popen)
    command = "echo ok"
    project = {"key": "DEMO", "repo": "example/demo", "base": "main", "checks": [command],
               "post_merge_criteria": [{"check": command}]}
    claim = SimpleNamespace(project=project, ticket={"key": "DEMO-1"}, run_id="run-1", fence=1)

    result = post_merge.run_post_merge(FakeApi(), claim, str(tmp_path), 10, ["setpriv", "--"])

    if lease_loss:
        assert result["outcome"] == "lost"
        assert events == (["slot acquired", "slot released"] if lease_loss == "after-acquire" else [])
    else:
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
    def slot(*, cancel=None):
        events.append("slot")
        yield

    class Runtime:
        def __init__(self, spec, run_as, *, process_prefix=None):
            events.append(("runtime", spec.cwd, process_prefix))

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
    assert events == ["slot", ("runtime", str(tmp_path), suite_slots.nice_prefix([])), "bound"]


def test_version_release_timeout_kills_descendants_and_releases_the_slot(monkeypatch, tmp_path):
    started = time.monotonic()
    slot_directory = tmp_path / "slots"
    monkeypatch.setenv("AIFLOW4_TEST_SLOT_DIR", str(slot_directory))
    child_pid_path = tmp_path / "child.pid"
    command = f"sleep 30 & echo $! > {shlex.quote(str(child_pid_path))}; wait"

    exit_code, output = version_release._run_check(command, {}, tmp_path, [], {"PATH": "/usr/bin:/bin"},
                                                   str(tmp_path), 1)

    assert exit_code != 0 and "Check exceeded 1 seconds." in output
    assert time.monotonic() - started < 5
    child_pid = int(child_pid_path.read_text())
    child_stat = Path(f"/proc/{child_pid}/stat")
    deadline = time.monotonic() + 2
    child_stopped = False
    while time.monotonic() < deadline:
        try:
            process_state = child_stat.read_text().split(") ", 1)[1].split()[0]
        except FileNotFoundError:
            child_stopped = True
            break
        if process_state == "Z":
            child_stopped = True
            break
        time.sleep(0.01)
    assert child_stopped, "timed-out check left its child running"
    with suite_slots.suite_slot(slot_directory):
        pass


@pytest.mark.parametrize("worker_module", [post_merge, version_release])
@pytest.mark.parametrize(("returncode", "expected_code"), [(0, -9), (-9, -9)])
@pytest.mark.parametrize("kill_error", [False, True])
@pytest.mark.parametrize("has_stdout", [False, True])
def test_timeout_output_collection_is_bounded_after_kill(monkeypatch, worker_module, returncode, expected_code,
                                                        kill_error, has_stdout):
    class FakePipe:
        closed = False

        def close(self):
            self.closed = True

    class StuckProcess:
        pid = 123
        returncode = -9
        stdout = FakePipe() if has_stdout else None

        def __init__(self):
            self.timeouts = []

        def communicate(self, *, timeout):
            self.timeouts.append(timeout)
            raise subprocess.TimeoutExpired("check", timeout, output=b"partial")

        def wait(self, *, timeout):
            raise subprocess.TimeoutExpired("check", timeout)

    process = StuckProcess()
    process.returncode = returncode
    killed = []

    def kill_group(pid, sig):
        killed.append((pid, sig))
        if kill_error:
            raise ProcessLookupError

    monkeypatch.setattr(process_checks.os, "killpg", kill_group)

    output, exit_code = worker_module._communicate(process, 3)

    assert process.timeouts == [3, 1]
    assert (process.stdout is None or process.stdout.closed) and killed == [(123, process_checks.signal.SIGKILL)]
    assert exit_code == expected_code and output == "partial\nCheck exceeded 3 seconds."


def test_version_release_rechecks_lease_after_slot_acquisition(monkeypatch, tmp_path):
    from aiflow.v4.worker.dev import LeaseLost

    @contextmanager
    def slot(*, cancel=None):
        yield

    def unexpected_popen(*_args, **_kwargs):
        pytest.fail("project check started after lease loss")

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(version_release.subprocess, "Popen", unexpected_popen)

    with pytest.raises(LeaseLost):
        version_release._run_check("true", {}, tmp_path, [], {}, str(tmp_path), 10, cancel=lambda: True)


def test_version_release_maps_a_cancelled_slot_wait_to_lease_loss(monkeypatch, tmp_path):
    from aiflow.v4.worker.dev import LeaseLost

    def slot(*, cancel=None):
        raise suite_slots.SlotWaitCancelled("lost while waiting")

    monkeypatch.setattr(suite_slots, "suite_slot", slot)

    with pytest.raises(LeaseLost):
        version_release._run_check("true", {}, tmp_path, [], {}, str(tmp_path), 10, cancel=lambda: True)


def test_version_release_container_rechecks_lease_before_process_start(monkeypatch, tmp_path):
    import aiflow.v4.worker.containers as containers
    from aiflow.v4.worker.dev import LeaseLost

    @contextmanager
    def slot(*, cancel=None):
        yield

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    def unexpected_popen(*_args, **_kwargs):
        pytest.fail("container check started after lease loss")

    checks = iter((False, True))
    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(containers, "ContainerRuntime", Runtime)
    monkeypatch.setattr(version_release.subprocess, "Popen", unexpected_popen)

    with pytest.raises(LeaseLost):
        version_release._run_check("true", {"containers": True}, tmp_path, [], {}, str(tmp_path), 10,
                                   cancel=lambda: next(checks))


@pytest.mark.parametrize("lose_during_runtime", [False, True])
def test_post_merge_container_check_holds_slot_and_rechecks_lease(monkeypatch, tmp_path, lose_during_runtime):
    import aiflow.v4.worker.containers as containers
    from aiflow.v4.worker.dev import LeaseLost

    events = []
    checks = iter((False, lose_during_runtime))
    runtime_prefixes = []

    @contextmanager
    def slot(*, cancel=None):
        events.append("slot")
        yield

    class Runtime:
        def __init__(self, *_args, process_prefix=None):
            runtime_prefixes.append(process_prefix)

        def __enter__(self):
            events.append("runtime")
            return self

        def __exit__(self, *_exc):
            return False

        def bind_process(self, _pid):
            events.append("bound")

    class Process:
        pid = 123
        returncode = 0

        def communicate(self, **_kwargs):
            events.append("collected")
            return "ok", None

    def popen(*_args, **_kwargs):
        events.append("started")
        return Process()

    monkeypatch.setattr(suite_slots, "suite_slot", slot)
    monkeypatch.setattr(containers, "ContainerRuntime", Runtime)
    monkeypatch.setattr(post_merge.subprocess, "Popen", popen)

    assert runtime_prefixes == []
    if lose_during_runtime:
        with pytest.raises(LeaseLost):
            post_merge._run_check("true", {"containers": True}, tmp_path, [], {"HOME": str(tmp_path)}, 10,
                                  cancel=lambda: next(checks))
        assert events == ["slot", "runtime"]
    else:
        output, exit_code = post_merge._run_check("true", {"containers": True}, tmp_path, [],
                                                  {"HOME": str(tmp_path)}, 10, cancel=lambda: next(checks))
        assert (output, exit_code) == ("ok", 0)
        assert events == ["slot", "runtime", "started", "bound", "collected"]
    assert runtime_prefixes == [suite_slots.nice_prefix([])]


def test_post_merge_timeout_kills_descendants_that_hold_stdout(tmp_path):
    proc = subprocess.Popen(["bash", "-c", "sleep 30 &"], cwd=tmp_path, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, start_new_session=True)
    started = time.monotonic()

    output, exit_code = post_merge._communicate(proc, 1)

    assert exit_code != 0 and "Check exceeded 1 seconds." in output
    assert time.monotonic() - started < 5


def test_node_agent_reports_a_missing_worker_account_when_publishing_the_slot_limit(monkeypatch, tmp_path):
    from aiflow.v4.node import agent

    def missing(_name):
        raise KeyError("aiflow-worker")

    monkeypatch.setattr(agent.pwd, "getpwnam", missing)

    with pytest.raises(OSError, match="aiflow-worker account is missing"):
        agent._write_test_suite_slots(3, tmp_path / "slots")


def test_node_agent_publishes_worker_group_lock_files_and_repairs_missing_ones(monkeypatch, tmp_path):
    from aiflow.v4.node import agent

    shared = tmp_path / "slots"
    chown_calls = []
    fchown_calls = []
    monkeypatch.setattr(agent.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_gid=12345))
    monkeypatch.setattr(agent.os, "chown", lambda path, uid, gid: chown_calls.append((Path(path), uid, gid)))
    monkeypatch.setattr(agent.os, "fchown", lambda descriptor, uid, gid: fchown_calls.append((descriptor, uid, gid)))

    agent._write_test_suite_slots(33, shared)

    assert (shared / "limit").read_text() == "2\n"
    lock_paths = [shared / "acquire.lock", *(shared / f"slot-{index}.lock" for index in range(32))]
    assert all(path.is_file() and path.stat().st_mode & 0o777 == 0o660 for path in lock_paths)
    assert len(fchown_calls) == 33 and all(uid == 0 and gid == 12345 for _, uid, gid in fchown_calls)
    assert (shared, 0, 12345) in chown_calls

    (shared / "slot-31.lock").unlink()
    agent._write_test_suite_slots(4, shared)
    assert (shared / "slot-31.lock").is_file() and (shared / "limit").read_text() == "4\n"


def test_node_agent_rejects_a_precreated_non_regular_lock_path(monkeypatch, tmp_path):
    from aiflow.v4.node import agent

    shared = tmp_path / "slots"
    shared.mkdir()
    (shared / "acquire.lock").mkdir()
    monkeypatch.setattr(agent.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_gid=12345))
    monkeypatch.setattr(agent.os, "chown", lambda *_args: None)

    with pytest.raises(OSError, match="cannot prepare test-suite lock file"):
        agent._write_test_suite_slots(2, shared)


def test_node_agent_rejects_a_lock_that_changes_type_during_open(monkeypatch, tmp_path):
    import stat
    from aiflow.v4.node import agent

    lock_path = tmp_path / "lock"
    lock_path.touch()
    monkeypatch.setattr(agent.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_gid=12345))

    with monkeypatch.context() as scoped:
        scoped.setattr(agent.os, "fstat", lambda _descriptor: SimpleNamespace(st_mode=stat.S_IFDIR))
        with pytest.raises(OSError, match="test-suite lock path is not a regular file"):
            agent._ensure_test_suite_lock(lock_path, 12345)
