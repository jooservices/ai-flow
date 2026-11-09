"""AC1/AC2 of JAIF-595 (node side): CPU use split into AI tools, tests, browsers and databases."""
from __future__ import annotations

from pathlib import Path

import pytest

from aiflow.v4.node import agent, cpu_breakdown as cpu


@pytest.mark.parametrize("name, command, group", [
    ("claude", "", "ai_tools"),
    ("codex", "", "ai_tools"),
    ("node", "node /usr/lib/node_modules/@openai/codex/bin/codex --full-auto", "ai_tools"),
    ("node", "node /usr/bin/claude -p run pytest and jest and report", "ai_tools"),
    ("pytest", "", "tests"),
    ("python3", "python3 -m pytest tests/v4 -q", "tests"),
    ("python3.12", "/srv/.venv/bin/python3.12 -m unittest discover", "tests"),
    ("node", "node node_modules/.bin/vitest run", "tests"),
    ("php", "php vendor/bin/phpunit --testdox", "tests"),
    ("python3", "python3 -m aiflow.v4.node.agent", None),
    ("chrome", "", "browsers"),
    ("chrome-headless-", "", "browsers"),
    ("headless_shell", "", "browsers"),
    ("firefox", "", "browsers"),
    ("postgres", "", "databases"),
    ("redis-server", "", "databases"),
    ("mariadbd", "", "databases"),
    ("bash", "bash -lc echo hello", None),
    ("sshd", "", None),
])
def test_a_process_belongs_to_the_group_of_its_name_and_command(name, command, group):
    assert cpu.classify(name, command) == group


def write_process(root: Path, pid: int, name: str, jiffies: int, command: str = "") -> None:
    folder = root / str(pid)
    folder.mkdir(parents=True, exist_ok=True)
    # pid (name) state ppid pgrp session tty tpgid flags minflt cminflt majflt cmajflt utime stime ...
    fields = ["S", "1", "1", "1", "0", "0", "0", "0", "0", "0", "0", str(jiffies - jiffies // 2), str(jiffies // 2)]
    (folder / "stat").write_text(f"{pid} ({name}) {' '.join(fields)} 0 0 0\n")
    (folder / "cmdline").write_bytes(command.replace(" ", "\0").encode())


def test_the_jiffies_of_every_process_are_summed_by_group(tmp_path):
    write_process(tmp_path, 10, "claude", 100)
    write_process(tmp_path, 11, "codex", 50)
    write_process(tmp_path, 12, "python3", 400, "python3 -m pytest tests")
    write_process(tmp_path, 13, "chrome", 70)
    write_process(tmp_path, 14, "postgres", 300)
    write_process(tmp_path, 15, "sshd", 999)
    (tmp_path / "self").mkdir()
    (tmp_path / "meminfo").write_text("")
    assert cpu.sample_jiffies(tmp_path) == {"ai_tools": 150, "tests": 400, "browsers": 70, "databases": 300}


def test_a_process_with_a_space_or_bracket_in_its_name_is_read_correctly(tmp_path):
    write_process(tmp_path, 20, "Web Content (x)", 10)
    assert cpu._read_process(tmp_path / "20") == ("Web Content (x)", 10)
    (tmp_path / "21").mkdir()
    (tmp_path / "21" / "stat").write_text("garbage")
    assert cpu._read_process(tmp_path / "21") is None
    assert cpu._read_process(tmp_path / "22") is None


def test_shares_are_percent_of_the_whole_machine():
    before = {"ai_tools": 0, "tests": 0, "browsers": 0, "databases": 0}
    after = {"ai_tools": 25, "tests": 100, "browsers": 0, "databases": 50}
    # 4 CPUs, 100 ticks a second, 1 second: capacity 400 jiffies
    assert cpu.shares(before, after, 1.0, 4, 100) == {
        "ai_tools": 6.2, "tests": 25.0, "browsers": 0.0, "databases": 12.5,
    }
    assert cpu.shares(after, before, 1.0, 4, 100)["tests"] == 0.0  # ended process is never negative
    assert cpu.shares(before, {**before, "tests": 10**6}, 1.0, 4, 100)["tests"] == 100.0


def test_measure_samples_twice_and_reports_each_group(tmp_path):
    write_process(tmp_path, 12, "python3", 100, "python3 -m pytest tests")
    write_process(tmp_path, 14, "postgres", 100)

    def work_happens_meanwhile(seconds):
        write_process(tmp_path, 12, "python3", 200, "python3 -m pytest tests")

    measured = cpu.measure(tmp_path, sample_s=0.01, sleep=work_happens_meanwhile)
    assert set(measured) == set(cpu.GROUPS)
    assert measured["tests"] > 0 and measured["databases"] == 0.0 and measured["ai_tools"] == 0.0


def test_the_live_machine_can_be_measured():
    assert set(cpu.measure(sample_s=0.01)) == set(cpu.GROUPS)


def test_node_metrics_include_the_four_group_breakdown(monkeypatch):
    """AC1: the heartbeat metrics field carries the CPU split beside the overall cpu_pct."""
    breakdown = {"ai_tools": 1.5, "tests": 40.0, "browsers": 5.0, "databases": 8.0}
    monkeypatch.setattr(agent.cpu_breakdown, "measure", lambda: breakdown)
    recorded, _ = agent.metrics(None)
    assert recorded["cpu_breakdown"] == breakdown
    assert "cpu_pct" in recorded and "load1" in recorded and "cpus" in recorded
