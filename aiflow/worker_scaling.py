"""Shared reporting and shutdown helpers for configuration-managed legacy workers."""
from __future__ import annotations


def configuration_summary(cfg) -> tuple[str, list[str]]:
    """Return the worker count line used by `aiflow-work check` and its configured ids."""
    from . import health

    ids = health.configured_worker_ids(cfg)
    running = health.running_worker_count(cfg)
    cap = cfg.registry.max_workers.get(cfg.machine)
    line = (f"machine {cfg.machine}: {len(ids)} configured worker(s) ({', '.join(ids) or 'none'}), "
            f"{running} running, max_workers {cap or 'not set'}, "
            f"config {'from TOML files' if cfg.registry.from_files else 'legacy .env'}")
    return line, ids


def clear_worker_heartbeat(redis, cfg) -> None:
    """Remove this worker's fresh heartbeat after its graceful process exit."""
    from .lock import RedisError

    try:
        redis.delete(f"hb:{cfg.machine}:{cfg.worker_label}")
    except RedisError:
        pass


def run_worker(supervisor, redis, cfg) -> None:
    """Run the worker and clear its heartbeat whenever the process exits."""
    try:
        supervisor.run_forever()
    finally:
        clear_worker_heartbeat(redis, cfg)
