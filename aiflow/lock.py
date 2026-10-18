"""Leases, counters and cooldowns in the shared Redis (through `docker exec redis-cli`).

A lease is `SET key owner NX EX ttl`; it is renewed by the owner as a heartbeat and disappears when the
supervisor dies, which is how an orphaned ticket is detected.
"""
from __future__ import annotations

import os
import subprocess

RENEW = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end"
RELEASE = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"


class Redis:
    def __init__(self, container: str, db: int, prefix: str, password: str) -> None:
        self.container, self.db, self.prefix, self.password = container, db, prefix, password

    def _run(self, *args: str) -> str:
        env = dict(os.environ, REDISCLI_AUTH=self.password)
        cmd = ["docker", "exec", "-e", "REDISCLI_AUTH", self.container, "redis-cli", "-n", str(self.db), "--no-auth-warning", *args]
        result = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"redis-cli failed: {result.stderr.strip()[:200]}")
        return result.stdout.strip()

    def key(self, name: str) -> str:
        return self.prefix + name

    def acquire(self, name: str, owner: str, ttl: int) -> bool:
        return self._run("SET", self.key(name), owner, "NX", "EX", str(ttl)) == "OK"

    def renew(self, name: str, owner: str, ttl: int) -> bool:
        return self._run("EVAL", RENEW, "1", self.key(name), owner, str(ttl)) == "1"

    def release(self, name: str, owner: str) -> bool:
        return self._run("EVAL", RELEASE, "1", self.key(name), owner) == "1"

    def get(self, name: str) -> str | None:
        value = self._run("GET", self.key(name))
        return value or None

    def set(self, name: str, value: str, ttl: int) -> None:
        self._run("SET", self.key(name), value, "EX", str(ttl))

    def incr(self, name: str, ttl: int) -> int:
        count = int(self._run("INCR", self.key(name)))
        self._run("EXPIRE", self.key(name), str(ttl))
        return count

    def delete(self, name: str) -> None:
        self._run("DEL", self.key(name))
