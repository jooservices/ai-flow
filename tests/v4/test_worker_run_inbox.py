from __future__ import annotations

import subprocess
import threading
from types import SimpleNamespace

from aiflow.v4.adapters import RunResult, RunSpec
from aiflow.v4.worker.dev import Heartbeat, run_with_inbox


class FakeHeartbeat:
    def __init__(self, messages):
        self.messages = list(messages)
        self.lost = threading.Event()
        self.included = []
        self.checkpoints = 0

    def checkpoint(self):
        self.checkpoints += 1
        if self.messages:
            return [self.messages.pop(0)]
        return []

    def include(self, messages):
        self.included.extend(messages)


class FakeAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.specs = []

    def run(self, spec, on_progress=None, stop=None, on_start=None):
        self.specs.append(spec)
        if on_start:
            on_start()
        return self.responses.pop(0)


def test_worker_injects_guidance_only_at_a_provider_process_boundary():
    adapter = FakeAdapter([RunResult(final_text="First pass", tokens_in=10, duration_s=2),
                           RunResult(final_text="Updated pass", tokens_out=7, duration_s=3)])
    message = {"id": 4, "kind": "request", "author": "owner@example.test", "body": "Add a regression test."}
    heartbeat = FakeHeartbeat([message])
    spec = RunSpec(prompt="Original task", cwd="/tmp/worktree", account_home="/tmp/account", log_path="/tmp/run.log")

    result = run_with_inbox(adapter, spec, heartbeat)

    assert len(adapter.specs) == 2
    assert adapter.specs[0].prompt == "Original task"
    assert "Original task" in adapter.specs[1].prompt
    assert "Add a regression test." in adapter.specs[1].prompt
    assert adapter.specs[1].cwd == spec.cwd and adapter.specs[1].log_append is True
    assert heartbeat.included == [message]
    assert result.final_text == "Updated pass"
    assert result.tokens_in == 10 and result.tokens_out == 7
    assert result.duration_s == 5


def test_continuation_keeps_the_fenced_run_branch_and_uncommitted_worktree(tmp_path):
    worktree = tmp_path / "worktree"
    worktree.mkdir()

    def git(*args):
        result = subprocess.run(["git", *args], cwd=worktree, check=True, capture_output=True, text=True)
        return result.stdout.strip()

    git("init", "-b", "JAIF-357-inbox-checkpoint")
    git("config", "user.name", "Inbox test")
    git("config", "user.email", "inbox-test@example.test")
    (worktree / "tracked.txt").write_text("baseline\n")
    git("add", "tracked.txt")
    git("commit", "-m", "test: Create a checkpoint worktree")
    original_branch = git("branch", "--show-current")
    original_commit = git("rev-parse", "HEAD")
    (worktree / "owner-draft.txt").write_text("preserve this existing uncommitted edit\n")

    message = {"id": 9, "kind": "request", "author": "owner@example.test", "body": "Keep the draft file."}

    class InboxAPI:
        def __init__(self):
            self.calls = []
            self.status = "queued"

        def call(self, method, path, body):
            self.calls.append((method, path, body.copy()))
            if message["id"] in body["inbox_included"]:
                self.status = "included"
            elif message["id"] in body["inbox_received"]:
                self.status = "received"
            inbox = [] if self.status == "included" else [{**message, "status": self.status}]
            return {"inbox": inbox}

    class WorktreeAdapter:
        def __init__(self):
            self.specs = []

        def run(self, spec, on_progress=None, stop=None, on_start=None):
            self.specs.append(spec)
            assert git("branch", "--show-current") == original_branch
            assert (worktree / "owner-draft.txt").read_text() == "preserve this existing uncommitted edit\n"
            if len(self.specs) == 1:
                (worktree / "provider-turn.txt").write_text("uncommitted provider work\n")
                return RunResult(final_text="First pass", duration_s=1)
            assert (worktree / "provider-turn.txt").read_text() == "uncommitted provider work\n"
            if on_start:
                on_start()
            return RunResult(final_text="Finished with the owner guidance", duration_s=1)

    run = SimpleNamespace(run_id="same-run", fence=7, ticket={"key": "TEST-1"})
    api = InboxAPI()
    heartbeat = Heartbeat(api, run, every_s=30)
    adapter = WorktreeAdapter()
    spec = RunSpec(prompt="Original task", cwd=str(worktree), account_home=str(tmp_path / "account"))

    result = run_with_inbox(adapter, spec, heartbeat)

    assert len(adapter.specs) == 2 and all(item.cwd == str(worktree) for item in adapter.specs)
    assert adapter.specs[1].log_append is True
    assert git("branch", "--show-current") == original_branch
    assert git("rev-parse", "HEAD") == original_commit  # continuation does not commit or switch work
    status = git("status", "--porcelain")
    assert "?? owner-draft.txt" in status and "?? provider-turn.txt" in status
    assert len({path for _, path, _ in api.calls}) == 1
    assert {path for _, path, _ in api.calls} == {"/v1/runs/same-run/heartbeat"}
    assert all(body["fence"] == run.fence for _, _, body in api.calls)
    assert api.status == "included" and result.final_text == "Finished with the owner guidance"


def test_worker_does_not_start_a_continuation_when_no_guidance_arrived():
    adapter = FakeAdapter([RunResult(final_text="Done")])
    result = run_with_inbox(adapter, RunSpec(prompt="Original", cwd="/tmp", account_home="/tmp"), FakeHeartbeat([]))
    assert result.final_text == "Done"
    assert len(adapter.specs) == 1


def test_continuations_share_the_original_run_time_budget():
    adapter = FakeAdapter([RunResult(final_text="First pass", duration_s=9),
                           RunResult(final_text="Updated pass", duration_s=5)])
    heartbeat = FakeHeartbeat([{"id": 1, "kind": "context", "author": "owner", "body": "Keep the change small."}])

    run_with_inbox(adapter, RunSpec(prompt="Original", cwd="/tmp", account_home="/tmp", timeout_s=30), heartbeat)

    assert len(adapter.specs) == 2 and adapter.specs[1].timeout_s == 21


def test_expired_run_budget_does_not_receive_guidance_for_an_unstarted_continuation():
    adapter = FakeAdapter([RunResult(final_text="First pass", duration_s=61)])
    heartbeat = FakeHeartbeat([{"id": 1, "kind": "request", "author": "owner", "body": "Fix one more test."}])

    run_with_inbox(adapter, RunSpec(prompt="Original", cwd="/tmp", account_home="/tmp", timeout_s=60), heartbeat)

    assert len(adapter.specs) == 1 and heartbeat.checkpoints == 0


def test_heartbeat_receipt_is_retryable_and_inclusion_happens_only_after_provider_start():
    class InboxAPI:
        def __init__(self):
            self.messages = [{"id": 1, "kind": "context", "author": "owner", "body": "Use the latest spec.",
                              "status": "queued"}]
            self.calls = []
            self.fail_once = True

        def call(self, method, path, body):
            self.calls.append(body)
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("temporary network failure")
            for message in self.messages:
                if message["id"] in body.get("inbox_received", []):
                    message["status"] = "received"
                if message["id"] in body.get("inbox_included", []):
                    message["status"] = "included"
            return {"inbox": [m for m in self.messages if m["status"] in ("queued", "received")]}

    api = InboxAPI()
    run = SimpleNamespace(run_id="run-1", fence=3, ticket={"key": "TEST-1"})
    heartbeat = Heartbeat(api, run, every_s=30)

    assert heartbeat.checkpoint() == []  # the failed poll does not discard the persisted message
    batch = heartbeat.checkpoint()
    assert [message["id"] for message in batch] == [1]
    assert api.messages[0]["status"] == "received"
    heartbeat.include(batch)
    assert api.messages[0]["status"] == "included"
    assert api.calls[-1]["inbox_included"] == [1]
