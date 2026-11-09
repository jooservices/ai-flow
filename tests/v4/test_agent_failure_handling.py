import logging
import threading

from aiflow.v4.node import agent


def test_heartbeat_worker_failure_is_logged_and_the_heartbeat_keeps_going(monkeypatch, caplog):
    state = agent._AgentLoopState(tools={}, tools_at=0)

    def failing_worker_state(*_args):
        raise RuntimeError("worker unit refused to start")

    for stage in ("_apply_toolchains", "_apply_services", "_apply_probes", "_apply_queued_work"):
        monkeypatch.setattr(agent, stage, lambda *_args: None)
    monkeypatch.setattr(agent, "sync_provider_sign_ins", lambda *_args: None)
    monkeypatch.setattr(agent, "_apply_worker_state", failing_worker_state)

    with caplog.at_level(logging.WARNING, logger="aiflow4.node"):
        assert agent.apply_heartbeat(object(), "node-test", {}, state, True, threading.Event(), 15) is True

    assert "heartbeat failed: worker unit refused to start" in caplog.text
