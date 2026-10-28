from __future__ import annotations

import threading
from types import SimpleNamespace

from aiflow.v4.adapters import RunResult, RunSpec
from aiflow.v4.worker.dev import Heartbeat, run_with_inbox


class FakeHeartbeat:
    def __init__(self, messages):
        self.messages = list(messages)
        self.lost = threading.Event()
        self.included = []

    def checkpoint(self):
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


def test_worker_does_not_start_a_continuation_when_no_guidance_arrived():
    adapter = FakeAdapter([RunResult(final_text="Done")])
    result = run_with_inbox(adapter, RunSpec(prompt="Original", cwd="/tmp", account_home="/tmp"), FakeHeartbeat([]))
    assert result.final_text == "Done"
    assert len(adapter.specs) == 1


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
