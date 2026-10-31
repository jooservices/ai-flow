"""Mount and compose-file policy of the container runtime: which paths a container may bind and which compose settings are refused.

Moved out of containers.py (JAIF-509) without changes; containers.py re-exports the names."""
from __future__ import annotations

import re

from pathlib import Path
from typing import Any


ACTIVE_CONTAINER_LIMIT = 8
_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,62}\Z")


def _compose_bind_source(source: str) -> bool:
    return Path(source).is_absolute() or source == "." or source.startswith(("./", "../"))


class ContainerPolicyError(ValueError):
    """A command or compose file requested access outside the run's container policy."""


def _within(path: str | Path, root: Path) -> bool:
    try:
        resolved = Path(path)
        if not resolved.is_absolute():
            resolved = root / resolved
        resolved = resolved.resolve(strict=False)
        base = root.resolve(strict=True)
        return resolved == base or base in resolved.parents
    except (OSError, RuntimeError, ValueError):
        return False


def _inside_file(path: str, root: Path, *, base: Path | None = None) -> Path:
    if not path or any(char in path for char in "$`\\") or path.startswith("~"):
        raise ContainerPolicyError("file paths must be literal paths inside the worktree")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContainerPolicyError("compose and environment files must exist inside the worktree") from exc
    if not _within(candidate, root) or not candidate.is_file():
        raise ContainerPolicyError("compose and environment files must stay inside the worktree")
    return candidate


def _inside_directory(path: str, root: Path, *, base: Path | None = None) -> Path:
    if not path or any(char in path for char in "$`\\") or path.startswith("~"):
        raise ContainerPolicyError("build contexts must be literal directories inside the worktree")
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContainerPolicyError("build contexts must exist inside the worktree") from exc
    if not _within(candidate, root) or not candidate.is_dir():
        raise ContainerPolicyError("build contexts must stay inside the worktree")
    return candidate


def _validate_bind(source: str, root: Path, *, exact_worktree: bool = False, base: Path | None = None) -> Path:
    if not source or any(char in source for char in "$`\\") or source.startswith("~"):
        raise ContainerPolicyError("bind mounts must use literal worktree paths")
    candidate = Path(source)
    if not candidate.is_absolute():
        candidate = (base or root) / candidate
    try:
        candidate = candidate.resolve(strict=True)
    except OSError as exc:
        raise ContainerPolicyError("bind mounts must exist inside the worktree") from exc
    if not _within(candidate, root) or (exact_worktree and candidate != root.resolve(strict=True)):
        raise ContainerPolicyError("containers may bind only the current worktree")
    return candidate


def _normalize_mount(value: str, root: Path, *, exact_worktree: bool = False, base: Path | None = None,
                    map_source=None) -> str:
    if value.startswith("mount="):
        fields: dict[str, str] = {}
        for field in value[6:].split(","):
            key, sep, item = field.partition("=")
            if not sep or key in fields:
                raise ContainerPolicyError("invalid --mount syntax")
            fields[key] = item
        if fields.get("type") != "bind" or set(fields) - {"type", "src", "source", "dst", "destination", "target", "readonly", "readwrite"}:
            raise ContainerPolicyError("--mount accepts only one worktree bind mount")
        source_keys = set(fields) & {"src", "source"}
        target_keys = set(fields) & {"dst", "destination", "target"}
        if len(source_keys) != 1 or len(target_keys) != 1:
            raise ContainerPolicyError("--mount needs one source and one destination")
        source = fields.get("src", fields.get("source", ""))
        if "readonly" in fields and fields["readonly"].lower() not in {"true", "1"}:
            raise ContainerPolicyError("invalid read-only mount option")
        if "readwrite" in fields and fields["readwrite"].lower() not in {"true", "1"}:
            raise ContainerPolicyError("invalid read-write mount option")
        resolved = _validate_bind(source, root, exact_worktree=exact_worktree, base=base)
        fields["src" if "src" in fields or "source" not in fields else "source"] = (
            str(map_source(resolved)) if map_source else str(resolved))
        return "mount=" + ",".join(f"{key}={item}" for key, item in fields.items())
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise ContainerPolicyError("volume syntax must be source:target[:ro|rw]")
    if len(parts) == 3 and parts[2] not in {"ro", "rw"}:
        raise ContainerPolicyError("bind mount propagation and relabel options are not allowed")
    resolved = _validate_bind(parts[0], root, exact_worktree=exact_worktree, base=base)
    parts[0] = str(map_source(resolved)) if map_source else str(resolved)
    return ":".join(parts)


def _short_compose_volume(value: str, project_dir: Path, root: Path, named_volumes: set[str]) -> None:
    parts = value.split(":")
    if len(parts) == 1:
        raise ContainerPolicyError("anonymous compose volumes are not allowed; declare a quota-backed named volume")
    if len(parts) not in {2, 3}:
        raise ContainerPolicyError("invalid compose volume syntax")
    source = parts[0]
    if not source:
        raise ContainerPolicyError("anonymous compose volumes are not allowed; declare a quota-backed named volume")
    if _compose_bind_source(source):
        _validate_bind(str(Path(source) if Path(source).is_absolute() else project_dir / source), root)
    elif source.startswith(("~", "$")) or "\\" in source:
        raise ContainerPolicyError("compose bind mounts must use literal worktree paths")
    elif source and source not in named_volumes:
        raise ContainerPolicyError("compose named volumes must be declared in the project")
    if len(parts) == 3 and set(parts[2].split(",")) - {"ro", "rw"}:
        raise ContainerPolicyError("compose mount propagation and relabel options are not allowed")


def _validate_compose_document(data: Any, compose_file: Path, project_dir: Path, root: Path,
                               *, require_services: bool = True) -> int:
    if not isinstance(data, dict):
        raise ContainerPolicyError("compose file must contain a mapping")
    if "include" in data:
        raise ContainerPolicyError("compose include is disabled; all compose inputs must be explicit worktree files")
    raw_volumes = data.get("volumes") or {}
    if not isinstance(raw_volumes, dict):
        raise ContainerPolicyError("compose volumes must be a mapping")
    named_volumes = set(raw_volumes)
    if len(named_volumes) > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError(f"compose may declare at most {ACTIVE_CONTAINER_LIMIT} named volumes")
    for name, volume in raw_volumes.items():
        if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(volume or {}, dict):
            raise ContainerPolicyError("invalid compose volume declaration")
        if (volume or {}).get("name"):
            raise ContainerPolicyError("custom compose volume names are not allowed; volumes are named per run")
        if ((volume or {}).get("external") or (volume or {}).get("driver_opts")
                or (volume or {}).get("driver") not in (None, "", "local")):
            raise ContainerPolicyError("external and host-backed named volumes are not allowed")
    for section in ("secrets", "configs"):
        entries = data.get(section) or {}
        if not isinstance(entries, dict):
            raise ContainerPolicyError(f"compose {section} must be a mapping")
        for name, entry in entries.items():
            if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(entry or {}, dict):
                raise ContainerPolicyError(f"invalid compose {section} declaration")
            entry = entry or {}
            if entry.get("external"):
                raise ContainerPolicyError(f"external compose {section} are not allowed")
            if entry.get("file"):
                _inside_file(str(entry["file"]), root, base=project_dir)
    networks = data.get("networks") or {}
    if not isinstance(networks, dict):
        raise ContainerPolicyError("compose networks must be a mapping")
    for name, network in networks.items():
        network = network or {}
        if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(network, dict):
            raise ContainerPolicyError("invalid compose network declaration")
        if (network.get("external") or network.get("name") or network.get("driver_opts")
                or network.get("driver") not in (None, "", "bridge")):
            raise ContainerPolicyError("external, named, host-backed, and custom compose networks are not allowed")
    services = data.get("services") or {}
    if not isinstance(services, dict) or (require_services and not services) or len(services) > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError(f"compose must define between 1 and {ACTIVE_CONTAINER_LIMIT} services")
    for name, service in services.items():
        if not _SAFE_NAME.fullmatch(str(name)) or not isinstance(service, dict):
            raise ContainerPolicyError("invalid compose service")
        if (service.get("privileged") not in (None, False) or service.get("cap_add") or service.get("devices")
                or service.get("device_cgroup_rules")):
            raise ContainerPolicyError("privileged containers, added capabilities, and host devices are not allowed")
        labels = service.get("labels") or {}
        label_keys = labels.keys() if isinstance(labels, dict) else labels if isinstance(labels, list) else []
        if any(str(label).partition("=")[0] == "aiflow4.run" for label in label_keys):
            raise ContainerPolicyError("the aiflow4.run label is reserved for broker cleanup")
        if service.get("volumes_from") or service.get("security_opt") or service.get("runtime"):
            raise ContainerPolicyError("host container controls and inherited mounts are not allowed")
        if service.get("extends") or service.get("develop"):
            raise ContainerPolicyError("compose service inheritance and host-side watch settings are not allowed")
        for key in ("pid", "ipc", "uts", "userns_mode", "cgroup"):
            if service.get(key) not in (None, ""):
                raise ContainerPolicyError(f"custom {key} namespaces are not allowed")
        network_mode = str(service.get("network_mode") or "").lower()
        if network_mode:
            raise ContainerPolicyError("custom container network modes are not allowed")
        service_networks = service.get("networks") or []
        if isinstance(service_networks, dict):
            service_networks = list(service_networks)
        if not isinstance(service_networks, list):
            raise ContainerPolicyError("compose service networks must be a list or mapping")
        if any(str(item) not in networks and str(item) != "default" for item in service_networks):
            raise ContainerPolicyError("compose services may use only networks declared in this project")
        if service.get("build"):
            build = service["build"]
            build = {"context": build} if isinstance(build, str) else build
            if (not isinstance(build, dict) or build.get("ssh") or build.get("secrets") or build.get("additional_contexts")
                    or build.get("privileged") not in (None, False)):
                raise ContainerPolicyError("build secrets, ssh agents, and additional contexts are not allowed")
            context = _inside_directory(str(build.get("context") or "."), root, base=project_dir)
            dockerfile = build.get("dockerfile")
            if dockerfile:
                _inside_file(str(context / str(dockerfile)), root)
            if str(build.get("network") or "").lower() not in {"", "default", "none"}:
                raise ContainerPolicyError("host networking during build is not allowed")
        env_files = service.get("env_file") or []
        if isinstance(env_files, (str, dict)):
            env_files = [env_files]
        for item in env_files:
            path = item.get("path") if isinstance(item, dict) else item
            _inside_file(str(path), root, base=project_dir)
        for volume in service.get("volumes") or []:
            if isinstance(volume, str):
                _short_compose_volume(volume, project_dir, root, named_volumes)
            elif isinstance(volume, dict):
                kind = str(volume.get("type") or "volume")
                if kind == "bind":
                    source = str(volume.get("source") or "")
                    _validate_bind(source if Path(source).is_absolute() else str(project_dir / source), root)
                    bind = volume.get("bind") or {}
                    if bind.get("propagation"):
                        raise ContainerPolicyError("bind propagation is not allowed")
                elif kind == "volume":
                    source = str(volume.get("source") or "")
                    if not source:
                        raise ContainerPolicyError("anonymous compose volumes are not allowed; declare a quota-backed named volume")
                    if source not in named_volumes:
                        raise ContainerPolicyError("compose named volumes must be declared in the project")
                elif kind != "tmpfs":
                    raise ContainerPolicyError(f"compose mount type is not allowed: {kind}")
            else:
                raise ContainerPolicyError("invalid compose volume")
    return len(services)
