"""Split the v4 test files into balanced shards that CI runs on parallel runners (JAIF-431).

    python3 -m aiflow.v4.core.test_shards files --index 2 --total 4

Prints the test files of one shard, one per line. The scale proof and the browser journeys have their own CI jobs and
are in no shard. Files are assigned largest first to the lightest shard (size in bytes), so every runner and the
coverage test compute the same partition.
"""
from __future__ import annotations

import argparse
from pathlib import Path

TESTS = Path("tests/v4")
OWN_JOB = ("test_scale_proof.py", "test_console_browser.py", "test_log_viewer_browser.py")
SHARDS = 4


def sharded_files(root: Path = Path(".")) -> list[Path]:
    return sorted(p for p in (root / TESTS).rglob("test_*.py") if p.relative_to(root / TESTS).as_posix() not in OWN_JOB)


KEEP_APART: tuple[tuple[str, str], ...] = ()  # files that must not share a shard; empty since JAIF-437 was fixed


def partition(files: list[Path], total: int = SHARDS) -> list[list[Path]]:
    shards: list[list[Path]] = [[] for _ in range(total)]
    loads = [0] * total
    for path in sorted(files, key=lambda p: (-p.stat().st_size, p.as_posix())):
        lightest = loads.index(min(loads))
        shards[lightest].append(path)
        loads[lightest] += path.stat().st_size
    for first, second in KEEP_APART:
        _keep_apart(shards, loads, first, second)
    return [sorted(s) for s in shards]


def _keep_apart(shards: list[list[Path]], loads: list[int], first: str, second: str) -> None:
    home = {path.name: index for index, shard in enumerate(shards) for path in shard}
    if first not in home or second not in home or home[first] != home[second] or len(shards) < 2:
        return
    source = home[second]
    moved = next(path for path in shards[source] if path.name == second)
    target = min((i for i in range(len(shards)) if i != source), key=lambda i: loads[i])
    shards[source].remove(moved)
    shards[target].append(moved)
    loads[source] -= moved.stat().st_size
    loads[target] += moved.stat().st_size


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="test_shards", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", choices=["files"])
    parser.add_argument("--index", type=int, required=True, help="1-based shard number")
    parser.add_argument("--total", type=int, default=SHARDS)
    args = parser.parse_args(argv)
    if not 1 <= args.index <= args.total:
        parser.error("--index must be between 1 and --total")
    for path in partition(sharded_files(), args.total)[args.index - 1]:
        print(path.as_posix())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
