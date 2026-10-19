"""Leases, counters and cooldowns in the shared Redis, over a plain TCP connection (RESP).

A lease is `SET key owner NX EX ttl`; it is renewed by the owner as a heartbeat and disappears when the
supervisor dies, which is how an orphaned ticket is detected. Every machine talks to the same Redis, so the
client reconnects and retries on network errors and raises `RedisError` when the server stays unreachable.
"""
from __future__ import annotations

import socket
import time

RENEW = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('expire',KEYS[1],ARGV[2]) else return 0 end"
RELEASE = "if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"
QUEUE_DEPLOY = """
if redis.call('exists', KEYS[4]) == 1 then
  if redis.call('get', KEYS[1]) == ARGV[1] then return 1 end
  return 2
end
redis.call('set', KEYS[4], ARGV[2], 'EX', ARGV[5])
if redis.call('exists', KEYS[1]) == 1 then
  local pending = redis.call('get', KEYS[2])
  if pending and pending ~= '' then pending = pending .. string.char(10) .. ARGV[3] else pending = ARGV[3] end
  redis.call('set', KEYS[2], pending, 'EX', ARGV[4])
  return 0
end
redis.call('set', KEYS[1], ARGV[1], 'EX', ARGV[4])
redis.call('set', KEYS[3], ARGV[3], 'EX', ARGV[4])
return 1
"""
FINISH_DEPLOY = """
local owner = redis.call('get', KEYS[1])
if owner ~= ARGV[1] then
  if not owner and redis.call('exists', KEYS[2]) == 0 and redis.call('exists', KEYS[3]) == 0 then return '' end
  return -1
end
local active = redis.call('get', KEYS[3])
if active and active ~= ARGV[3] then return active end
if active then redis.call('del', KEYS[3]) end
local pending = redis.call('get', KEYS[2])
if pending and pending ~= '' then
  redis.call('del', KEYS[2])
  redis.call('set', KEYS[3], pending, 'EX', ARGV[2])
  redis.call('expire', KEYS[1], ARGV[2])
  return pending
end
redis.call('del', KEYS[1])
return ''
"""


class RedisError(RuntimeError):
    pass


def encode(*parts: object) -> bytes:
    out = [f"*{len(parts)}\r\n".encode()]
    for part in parts:
        data = str(part).encode()
        out.append(b"$" + str(len(data)).encode() + b"\r\n" + data + b"\r\n")
    return b"".join(out)


def decode(stream) -> object:
    """Read one RESP reply from a binary file-like object."""
    line = stream.readline()
    if not line:
        raise ConnectionError("connection closed by the server")
    kind, rest = line[:1], line[1:].rstrip(b"\r\n")
    if kind == b"+":
        return rest.decode()
    if kind == b"-":
        raise RedisError(rest.decode())
    if kind == b":":
        return int(rest)
    if kind == b"$":
        size = int(rest)
        if size < 0:
            return None
        data = stream.read(size + 2)
        if len(data) != size + 2:
            raise ConnectionError("short read")
        return data[:size].decode(errors="replace")
    if kind == b"*":
        count = int(rest)
        return None if count < 0 else [decode(stream) for _ in range(count)]
    raise RedisError(f"unexpected reply {line!r}")


class Redis:
    def __init__(self, host: str, port: int, db: int, prefix: str, password: str, user: str = "",
                 timeout: float = 8.0, retries: int = 3) -> None:
        self.host, self.port, self.db, self.prefix = host, port, db, prefix
        self.password, self.user, self.timeout, self.retries = password, user, timeout, retries
        self._sock: socket.socket | None = None
        self._stream = None

    # ---- connection ------------------------------------------------------------------------------------------
    def _connect(self) -> None:
        self.close()
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock, self._stream = sock, sock.makefile("rb")
        if self.password:
            self._send(*(["AUTH", self.user, self.password] if self.user else ["AUTH", self.password]))
        if self.db:
            self._send("SELECT", self.db)

    def close(self) -> None:
        for resource in (self._stream, self._sock):
            try:
                if resource is not None:
                    resource.close()
            except OSError:
                pass
        self._sock = self._stream = None

    def _send(self, *parts: object) -> object:
        assert self._sock is not None
        self._sock.sendall(encode(*parts))
        return decode(self._stream)

    def command(self, *parts: object) -> object:
        """Run one command; reconnect and retry on network errors (errors reported by Redis are not retried)."""
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                if self._sock is None:
                    self._connect()
                return self._send(*parts)
            except RedisError:
                raise
            except (OSError, ConnectionError, ValueError) as error:
                last = error
                self.close()
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (attempt + 1))
        raise RedisError(f"redis {self.host}:{self.port} unreachable: {last}")

    # ---- the operations the supervisor needs ----------------------------------------------------------------
    def key(self, name: str) -> str:
        return self.prefix + name

    def acquire(self, name: str, owner: str, ttl: int) -> bool:
        if self.command("SET", self.key(name), owner, "NX", "EX", ttl) == "OK":
            return True
        return self.command("GET", self.key(name)) == owner  # a lost reply to an earlier retry must not strand our own lease

    def enqueue_deploy(self, lock_name: str, pending_name: str, active_name: str, seen_name: str, owner: str,
                       ticket: str, payload: str, ttl: int, seen_ttl: int) -> int:
        """Acquire the project lock or queue a merge once; return 1 acquired, 0 queued, 2 already seen."""
        return int(self.command("EVAL", QUEUE_DEPLOY, 4, self.key(lock_name), self.key(pending_name), self.key(active_name),
                                self.key(seen_name), owner, ticket, payload, ttl, seen_ttl))

    def finish_deploy(self, lock_name: str, pending_name: str, active_name: str, owner: str, ttl: int,
                      active_batch: str) -> str | int:
        """Complete an active batch and atomically claim the pending batch, safely on retried replies."""
        return self.command("EVAL", FINISH_DEPLOY, 3, self.key(lock_name), self.key(pending_name), self.key(active_name),
                            owner, ttl, active_batch)

    def renew(self, name: str, owner: str, ttl: int) -> bool:
        return self.command("EVAL", RENEW, 1, self.key(name), owner, ttl) == 1

    def release(self, name: str, owner: str) -> bool:
        return self.command("EVAL", RELEASE, 1, self.key(name), owner) == 1

    def get(self, name: str) -> str | None:
        return self.command("GET", self.key(name)) or None

    def get_many(self, names: list[str]) -> list[str | None]:
        if not names:
            return []
        values = self.command("MGET", *[self.key(n) for n in names])
        return [v or None for v in values]  # type: ignore[union-attr]

    def set(self, name: str, value: str, ttl: int) -> None:
        self.command("SET", self.key(name), value, "EX", ttl)

    def set_persistent(self, name: str, value: str) -> None:
        """Store lifecycle state until its owning workflow explicitly removes it."""
        self.command("SET", self.key(name), value)

    def incr(self, name: str, ttl: int) -> int:
        count = int(self.command("INCR", self.key(name)))  # type: ignore[arg-type]
        self.command("EXPIRE", self.key(name), ttl)
        return count

    def delete(self, name: str) -> None:
        self.command("DEL", self.key(name))

    def scan(self, pattern: str) -> list[str]:
        """Names (without the prefix) of the keys matching `pattern`."""
        found: list[str] = []
        cursor = "0"
        while True:
            cursor, batch = self.command("SCAN", cursor, "MATCH", self.key(pattern), "COUNT", 200)  # type: ignore[misc]
            found += [k[len(self.prefix):] for k in batch]
            if cursor == "0":
                return found
