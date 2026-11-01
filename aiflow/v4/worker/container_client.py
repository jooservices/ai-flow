"""Client shims and trusted broker launch construction."""
from __future__ import annotations

import json
import os
import socket
import sys
from pathlib import Path
from typing import Any, Callable


def broker_invocation(worktree: Path, engine_home: str, module_file: str,
                      within: Callable) -> tuple[list[str], str]:
    """Build an isolated launch for the installed broker, outside the project checkout."""
    root = worktree.resolve(strict=True)
    installed_file = Path(module_file).resolve(strict=True)
    if within(installed_file, root):
        raise RuntimeError("safe-container-runtime broker code must be installed outside the project worktree")
    trusted_root = installed_file.parents[3]
    home = Path(engine_home).resolve(strict=True)
    if within(home, root):
        raise RuntimeError("safe-container-runtime needs a trusted engine working directory")
    bootstrap = (
        "import importlib, os, pathlib, sys; "
        f"worktree=pathlib.Path({str(root)!r}); expected=pathlib.Path({str(installed_file)!r}); "
        "definitely_under=lambda value: (lambda path: path == worktree or worktree in path.parents)("
        "pathlib.Path(value or os.getcwd()).resolve()); "
        "sys.path[:]=[entry for entry in sys.path if not definitely_under(entry)]; "
        f"sys.path.insert(0, {str(trusted_root)!r}); "
        "module=importlib.import_module('aiflow.v4.worker.containers'); "
        "assert pathlib.Path(module.__file__).resolve() == expected; "
        "raise SystemExit(module._broker_main())"
    )
    return [sys.executable, "-I", "-c", bootstrap, "--broker"], str(home)


def proc_identity(pid: int) -> int | None:
    """Return Linux's process start tick so PID reuse cannot keep an orphan broker alive."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().split(")", 1)[1].split()
        return int(fields[19])
    except (OSError, ValueError, IndexError):
        return None


def is_child_of(pid: int, root_pid: int | None) -> bool:
    if not root_pid:
        return False
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        if pid == root_pid:
            return True
        seen.add(pid)
        try:
            status = Path(f"/proc/{pid}/stat").read_text()
            tail = status[status.rfind(")") + 2:].split()
            pid = int(tail[1])
        except (OSError, ValueError, IndexError):
            return False
    return False


def send(socket_path: str, payload: dict[str, Any]) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(1800)
    try:
        sock.connect(socket_path)
        sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())
        with sock.makefile("rb") as stream:
            return json.loads(stream.readline(16 * 1024 * 1024))
    finally:
        sock.close()


def client_main(send_request: Callable) -> int:
    """Entry point for the run-local `docker` and `docker-compose` shims."""
    socket_path = os.environ.get("AIFLOW4_CONTAINER_SOCKET", "")
    token = os.environ.get("AIFLOW4_CONTAINER_TOKEN", "")
    if not socket_path or not token:
        print("safe container runtime is not active for this run", file=sys.stderr)
        return 125
    argv = sys.argv[1:]
    if Path(sys.argv[0]).name == "docker-compose":
        argv = ["compose", *argv]
    try:
        response = send_request(socket_path, {"token": token, "argv": argv, "cwd": os.getcwd()})
    except (OSError, ValueError, TimeoutError):
        print("safe container runtime broker is unavailable", file=sys.stderr)
        return 125
    if not response.get("ok"):
        print(str(response.get("error") or "container command denied"), file=sys.stderr)
        return int(response.get("code", 125))
    sys.stdout.write(str(response.get("stdout") or ""))
    sys.stderr.write(str(response.get("stderr") or ""))
    return int(response.get("code", 125))
