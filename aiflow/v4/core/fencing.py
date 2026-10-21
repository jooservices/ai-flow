"""Leases and fencing tokens in Redis (Lua, atomic).

A ticket step is owned by one run at a time: `lease:<KEY>` holds "<run_id>:<fence>" with a PX expiry, and
`fence:<KEY>` is a counter that only grows. Every write to Jira or GitHub carries (run_id, fence) and is
checked here first, so a worker that lost its lease (stall, network split, restart) can never write again:
the next owner has a larger fence. Leases are renewed by heartbeats; an expired lease frees the ticket.
"""
from __future__ import annotations

from dataclasses import dataclass

import redis

CLAIM = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  return {0, redis.call('GET', KEYS[1])}
end
local f = redis.call('INCR', KEYS[2])
local v = ARGV[1] .. ':' .. f
redis.call('SET', KEYS[1], v, 'PX', tonumber(ARGV[2]))
return {f, v}
"""
RENEW = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
end
return 0
"""
CHECK = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return 1 end
return 0
"""
RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end
return 0
"""
LEADER = """
local cur = redis.call('GET', KEYS[1])
if not cur then
  redis.call('SET', KEYS[1], ARGV[1], 'PX', tonumber(ARGV[2]))
  return 1
end
if cur == ARGV[1] then
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[2]))
  return 1
end
return 0
"""


@dataclass(frozen=True)
class Claim:
    ok: bool
    fence: int
    holder: str  # "<run_id>:<fence>" of whoever holds the lease


class Leases:
    def __init__(self, client: redis.Redis, prefix: str = "aiflow4:") -> None:
        self.r = client
        self.p = prefix
        self._claim = client.register_script(CLAIM)
        self._renew = client.register_script(RENEW)
        self._check = client.register_script(CHECK)
        self._release = client.register_script(RELEASE)
        self._leader = client.register_script(LEADER)

    @classmethod
    def from_url(cls, url: str, prefix: str = "aiflow4:") -> "Leases":
        return cls(redis.Redis.from_url(url, decode_responses=True, socket_timeout=5, health_check_interval=30), prefix)

    def _k(self, kind: str, key: str) -> str:
        return f"{self.p}{kind}:{key}"

    # --- ticket leases --------------------------------------------------------------------------------------
    def claim(self, key: str, run_id: str, ttl_ms: int) -> Claim:
        fence, holder = self._claim(keys=[self._k("lease", key), self._k("fence", key)], args=[run_id, ttl_ms])
        return Claim(ok=int(fence) > 0, fence=int(fence), holder=str(holder or ""))

    def renew(self, key: str, run_id: str, fence: int, ttl_ms: int) -> bool:
        return int(self._renew(keys=[self._k("lease", key)], args=[f"{run_id}:{fence}", ttl_ms])) == 1

    def valid(self, key: str, run_id: str, fence: int) -> bool:
        return int(self._check(keys=[self._k("lease", key)], args=[f"{run_id}:{fence}"])) == 1

    def release(self, key: str, run_id: str, fence: int) -> bool:
        return int(self._release(keys=[self._k("lease", key)], args=[f"{run_id}:{fence}"])) == 1

    def holder(self, key: str) -> str | None:
        return self.r.get(self._k("lease", key))

    def master_holder(self, scope: str = "global") -> str | None:
        """Current fenced owner of a master monitoring scope, if any."""
        return self.holder(f"master:{scope}")

    def ttl_ms(self, key: str) -> int:
        return int(self.r.pttl(self._k("lease", key)))

    def held_keys(self) -> list[str]:
        prefix = self._k("lease", "")
        return [k[len(prefix):] for k in self.r.scan_iter(match=prefix + "*", count=500)]

    # --- account cooldowns (quota) --------------------------------------------------------------------------
    def cooldown(self, account: str, seconds: int, reason: str) -> None:
        self.r.set(self._k("cooldown", account), reason, ex=max(1, int(seconds)))

    def cooling(self, account: str) -> tuple[str, int] | None:
        k = self._k("cooldown", account)
        reason = self.r.get(k)
        return (reason, int(self.r.ttl(k))) if reason else None

    def uncool(self, account: str) -> None:
        self.r.delete(self._k("cooldown", account))

    # --- leader election for background loops ----------------------------------------------------------------
    def leader(self, loop: str, owner: str, ttl_ms: int) -> bool:
        return int(self._leader(keys=[self._k("leader", loop)], args=[owner, ttl_ms])) == 1
