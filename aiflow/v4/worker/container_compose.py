"""Validation and immutable snapshots for the container broker's Compose interface."""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Callable

import yaml

from .container_policy import ACTIVE_CONTAINER_LIMIT, ContainerPolicyError


def _snapshot_section_files(data: dict[str, Any], root: Path, project_dir: Path,
                            map_worktree_path: Callable | None, inside_file: Callable) -> None:
    for section in ("secrets", "configs"):
        entries = (data.get(section) or {}).values()
        for entry in entries:
            if isinstance(entry, dict) and entry.get("file"):
                entry["file"] = _mapped_path(inside_file(str(entry["file"]), root, base=project_dir),
                                             map_worktree_path)


def _snapshot_build(service: dict[str, Any], root: Path, project_dir: Path, map_worktree_path: Callable | None,
                    inside_file: Callable, inside_directory: Callable) -> None:
    if not service.get("build"):
        return
    build = service["build"]
    build = {"context": build} if isinstance(build, str) else build
    context = inside_directory(str(build.get("context") or "."), root, base=project_dir)
    build["context"] = _mapped_path(context, map_worktree_path)
    if build.get("dockerfile"):
        dockerfile = inside_file(str(context / str(build["dockerfile"])), root)
        build["dockerfile"] = _mapped_path(dockerfile, map_worktree_path)
    service["build"] = build


def _snapshot_env_files(service: dict[str, Any], root: Path, project_dir: Path,
                        map_worktree_path: Callable | None,
                        inside_file: Callable) -> None:
    env_files = service.get("env_file") or []
    if isinstance(env_files, (str, dict)):
        env_files = [env_files]
    normalized = []
    for item in env_files:
        if isinstance(item, dict):
            item = dict(item)
            item["path"] = _mapped_path(inside_file(str(item.get("path") or ""), root, base=project_dir),
                                        map_worktree_path)
            normalized.append(item)
        else:
            normalized.append(_mapped_path(inside_file(str(item), root, base=project_dir), map_worktree_path))
    if service.get("env_file"):
        service["env_file"] = normalized


def _snapshot_short_volume(volume: str, root: Path, project_dir: Path,
                           map_worktree_path: Callable | None,
                           compose_bind_source: Callable, validate_bind: Callable) -> Any:
    parts = volume.split(":")
    source = parts[0]
    if not compose_bind_source(source):
        return volume
    source_path = Path(source) if Path(source).is_absolute() else project_dir / source
    bind: dict[str, Any] = {"type": "bind", "source": _mapped_path(validate_bind(str(source_path), root),
                                                                         map_worktree_path),
                            "target": parts[1]}
    if len(parts) == 3:
        bind["read_only"] = parts[2] == "ro"
    return bind


def _snapshot_long_volume(volume: dict[str, Any], root: Path, project_dir: Path,
                          map_worktree_path: Callable | None,
                          validate_bind: Callable) -> dict[str, Any]:
    volume = dict(volume)
    source = str(volume.get("source") or "")
    source_path = Path(source) if Path(source).is_absolute() else project_dir / source
    volume["source"] = _mapped_path(validate_bind(str(source_path), root), map_worktree_path)
    return volume


def _snapshot_volumes(service: dict[str, Any], root: Path, project_dir: Path,
                      map_worktree_path: Callable | None,
                      compose_bind_source: Callable, validate_bind: Callable) -> None:
    normalized = []
    for volume in service.get("volumes") or []:
        if isinstance(volume, str):
            normalized.append(_snapshot_short_volume(volume, root, project_dir, map_worktree_path,
                                                       compose_bind_source, validate_bind))
        elif isinstance(volume, dict) and str(volume.get("type") or "volume") == "bind":
            normalized.append(_snapshot_long_volume(volume, root, project_dir, map_worktree_path, validate_bind))
        else:
            normalized.append(volume)
    if "volumes" in service:
        service["volumes"] = normalized


def compose_snapshot(data: dict[str, Any], compose_file: Path, project_dir: Path, root: Path,
                     runtime_dir: Path, *, map_worktree_path: Callable | None = None,
                     inside_file: Callable, inside_directory: Callable, engine_identity: Callable,
                     compose_bind_source: Callable, validate_bind: Callable) -> Path:
    """Write the validated Compose model to broker storage with pinned worktree paths."""
    _snapshot_section_files(data, root, project_dir, map_worktree_path, inside_file)
    for service in (data.get("services") or {}).values():
        if not isinstance(service, dict):
            continue
        _snapshot_build(service, root, project_dir, map_worktree_path, inside_file, inside_directory)
        _snapshot_env_files(service, root, project_dir, map_worktree_path, inside_file)
        _snapshot_volumes(service, root, project_dir, map_worktree_path,
                          compose_bind_source, validate_bind)
    snapshot = runtime_dir / f"compose-input-{secrets.token_hex(6)}.yaml"
    snapshot.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    uid, gid, _ = engine_identity()
    os.chown(snapshot, uid, gid)
    snapshot.chmod(0o600)
    return snapshot


def _mapped_path(path: Path, map_worktree_path: Callable | None) -> str:
    return str(map_worktree_path(path) if map_worktree_path else path)


def compose_env_snapshot(env_files: list[Path], project_dir: Path, runtime_dir: Path,
                         read_worktree_file: Callable | None, *, engine_identity: Callable) -> Path:
    """Write the file Compose interpolates from, preserving its original source order."""
    sources = list(env_files)
    default = project_dir / ".env"
    if not sources and os.path.lexists(default):
        if default.is_symlink():
            raise ContainerPolicyError("the compose .env file must not be a symbolic link")
        sources = [default]
    contents = []
    for path in sources:
        try:
            text = read_worktree_file(path) if read_worktree_file else path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise ContainerPolicyError("compose environment file could not be read") from exc
        contents.append(text if not text or text.endswith("\n") else text + "\n")
    snapshot = runtime_dir / f"compose-env-{secrets.token_hex(6)}.env"
    snapshot.write_text("".join(contents), encoding="utf-8")
    uid, gid, _ = engine_identity()
    os.chown(snapshot, uid, gid)
    snapshot.chmod(0o600)
    return snapshot


def _option_value(argv: list[str], index: int, option: str) -> tuple[str, int]:
    if "=" in option:
        return option.split("=", 1)[1], 1
    if index + 1 >= len(argv):
        raise ContainerPolicyError(f"{option} needs a value")
    return argv[index + 1], 2


def _consume_path_option(argv: list[str], index: int, root: Path, cwd: Path, files: list[Path],
                         env_files: list[Path], project_args: list[str], project_dir: Path | None,
                         inside_file: Callable, inside_directory: Callable) -> tuple[int, Path | None, bool]:
    arg = argv[index]
    if arg in {"-f", "--file"} or arg.startswith("--file="):
        value, advance = _option_value(argv, index, arg)
        files.append(inside_file(value, root, base=cwd))
        project_args.extend(["-f", str(files[-1])])
        return index + advance, project_dir, True
    if arg == "--env-file" or arg.startswith("--env-file="):
        value, advance = _option_value(argv, index, arg)
        env_files.append(inside_file(value, root, base=cwd))
        return index + advance, project_dir, True
    if arg == "--project-directory" or arg.startswith("--project-directory="):
        value, advance = _option_value(argv, index, arg)
        return index + advance, inside_directory(value, root, base=cwd), True
    return index, project_dir, False


def _parse_compose_options(argv: list[str], root: Path, cwd: Path, *, inside_file: Callable,
                            inside_directory: Callable) -> tuple[int, list[Path], list[Path], Path | None,
                                                                  list[str]]:
    index, files, env_files, project_dir, project_args = 1, [], [], None, []
    while index < len(argv):
        index, project_dir, handled = _consume_path_option(
            argv, index, root, cwd, files, env_files, project_args, project_dir, inside_file, inside_directory)
        if handled:
            continue
        arg = argv[index]
        if arg in {"-p", "--project-name"} or arg.startswith("--project-name="):
            raise ContainerPolicyError("compose project names are assigned per run")
        if arg in {"--profile", "--ansi", "--progress"}:
            if index + 1 >= len(argv):
                raise ContainerPolicyError(f"{arg} needs a value")
            if arg == "--profile":
                project_args.extend([arg, argv[index + 1]])
            index += 2
        elif arg.startswith("--profile=") or arg.startswith("--ansi=") or arg.startswith("--progress="):
            if arg.startswith("--profile="):
                project_args.append(arg)
            index += 1
        else:
            break
    return index, files, env_files, project_dir, project_args


def _find_compose_files(files: list[Path], cwd: Path, root: Path, project_args: list[str],
                        inside_file: Callable) -> list[Path]:
    if files:
        return files
    default = next((cwd / name for name in ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
                    if (cwd / name).is_file()), None)
    if default is None:
        raise ContainerPolicyError("no compose file was found in the worktree")
    files.append(inside_file(str(default), root))
    project_args.extend(["-f", str(files[-1])])
    return files


def _load_compose_documents(files: list[Path], project_dir: Path, root: Path,
                            read_worktree_file: Callable | None, validate_document: Callable
                            ) -> tuple[dict[str, Any], set[str], list[tuple[Path, dict[str, Any]]]]:
    all_services: dict[str, Any] = {}
    all_volumes: set[str] = set()
    documents = []
    for index, compose_file in enumerate(files):
        try:
            content = read_worktree_file(compose_file) if read_worktree_file else compose_file.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
        except (OSError, yaml.YAMLError) as exc:
            raise ContainerPolicyError("compose file could not be read") from exc
        validate_document(data, compose_file, project_dir, root, require_services=index == 0)
        if isinstance(data, dict):
            all_services.update(data.get("services") or {})
            all_volumes.update(data.get("volumes") or {})
        documents.append((compose_file, data))
    return all_services, all_volumes, documents


def _validate_compose_counts(all_services: dict[str, Any], all_volumes: set[str], active_count: int) -> None:
    service_count = len(all_services)
    if not service_count:
        raise ContainerPolicyError("compose file does not define services")
    if service_count > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError(f"compose may define at most {ACTIVE_CONTAINER_LIMIT} services")
    if len(all_volumes) > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError(f"compose may declare at most {ACTIVE_CONTAINER_LIMIT} named volumes")
    if active_count + service_count > ACTIVE_CONTAINER_LIMIT:
        raise ContainerPolicyError("the node container limit is full; retry after a run cleans up")


def _consume_compose_run_option(tail: list[str], index: int, safe_flags: set[str],
                                value_flags: set[str]) -> int:
    option = tail[index]
    if option in safe_flags:
        return index + 1
    if option in value_flags:
        if index + 1 >= len(tail):
            raise ContainerPolicyError(f"{option} needs a value")
        return index + 2
    if any(option.startswith(flag + "=") for flag in value_flags if flag.startswith("--")):
        return index + 1
    raise ContainerPolicyError(f"compose run option is not allowed: {option}")


def _compose_run_service_index(tail: list[str], value_flags: set[str]) -> int:
    index = 0
    while index < len(tail) and tail[index].startswith("-"):
        index += 2 if tail[index] in value_flags else 1
    return index


def _validate_compose_run_tail(tail: list[str], all_services: dict[str, Any]) -> list[str]:
    if not tail:
        raise ContainerPolicyError("docker compose run needs a service")
    safe_flags = {"--rm", "--build", "--no-deps", "--quiet-pull", "-T", "--no-TTY",
                  "--service-ports", "--use-aliases"}
    value_flags = {"--entrypoint", "--env", "-e", "--user", "-u", "--workdir", "-w"}
    index = 0
    while index < len(tail) and tail[index].startswith("-"):
        index = _consume_compose_run_option(tail, index, safe_flags, value_flags)
    if index == len(tail):
        raise ContainerPolicyError("docker compose run needs a service")
    if "--rm" not in tail:
        tail = ["--rm", *tail]
    service_index = _compose_run_service_index(tail, value_flags)
    if service_index >= len(tail) or tail[service_index] not in all_services:
        raise ContainerPolicyError("docker compose run must name a service in this compose project")
    return tail


def _validate_compose_up_tail(tail: list[str], all_services: dict[str, Any]) -> None:
    if not tail or not tail[0].startswith("-"):
        return
    safe = {"-d", "--detach", "--abort-on-container-exit", "--build", "--no-build", "--pull",
            "--remove-orphans", "--force-recreate", "--no-recreate", "--no-deps", "--wait", "--quiet-pull",
            "--renew-anon-volumes", "--timestamps"}
    index = 0
    while index < len(tail):
        item = tail[index]
        if item in safe:
            index += 1
        elif item in {"--timeout", "-t"}:
            index += 2
        elif item.startswith("--timeout="):
            index += 1
        elif item.startswith("-"):
            raise ContainerPolicyError(f"compose up option is not allowed: {item}")
        else:
            if item not in all_services:
                raise ContainerPolicyError("compose up may name only configured services")
            index += 1


def _validate_compose_subcommand(argv: list[str], index: int, all_services: dict[str, Any]) -> tuple[str, list[str]]:
    if index >= len(argv) or argv[index] not in {"up", "run"}:
        raise ContainerPolicyError("only docker compose up and run are supported")
    subcommand, tail = argv[index], argv[index + 1:]
    if subcommand == "run":
        tail = _validate_compose_run_tail(tail, all_services)
    else:
        _validate_compose_up_tail(tail, all_services)
    return subcommand, tail


def _snapshot_compose_inputs(documents: list[tuple[Path, dict[str, Any]]], files: list[Path], project_dir: Path,
                             root: Path, runtime_dir: Path, project_args: list[str], env_files: list[Path],
                             map_worktree_path: Callable | None, read_worktree_file: Callable | None,
                             compose_snapshot: Callable, env_snapshot: Callable) -> list[Path]:
    snapshots = [compose_snapshot(data, compose_file, project_dir, root, runtime_dir,
                                  map_worktree_path=map_worktree_path)
                 for compose_file, data in documents]
    paths = dict(zip(files, snapshots))
    for index, option in enumerate(project_args[:-1]):
        if option == "-f":
            project_args[index + 1] = str(paths[Path(project_args[index + 1])])
    env_file = env_snapshot(env_files, project_dir, runtime_dir, read_worktree_file)
    snapshots.append(env_file)
    project_args.extend(["--env-file", str(env_file)])
    return snapshots


def _write_compose_override(runtime_dir: Path, project_name: str, all_services: dict[str, Any],
                            engine_identity: Callable, container_layer_limit: str,
                            build_limit: str) -> Path:
    override = runtime_dir / f"compose-{secrets.token_hex(6)}.yaml"
    services_override: dict[str, dict[str, Any]] = {}
    for name in all_services:
        service_override: dict[str, Any] = {"storage_opt": {"size": container_layer_limit},
                                            "labels": {"aiflow4.run": project_name}}
        if (all_services.get(name) or {}).get("build"):
            service_override["build"] = {"storage_opt": {"size": build_limit}}
        services_override[name] = service_override
    override_data: dict[str, Any] = {"services": services_override}
    override.write_text(yaml.safe_dump(override_data, sort_keys=False), encoding="utf-8")
    uid, gid, _ = engine_identity()
    os.chown(override, uid, gid)
    override.chmod(0o600)
    return override


def compose_command(argv: list[str], root: Path, cwd: Path, runtime_dir: Path, project_name: str, *,
                    map_worktree_path: Callable | None = None, read_worktree_file: Callable | None = None,
                    inside_file: Callable, inside_directory: Callable, validate_document: Callable,
                    within: Callable, active_containers: Callable, compose_snapshot: Callable,
                    env_snapshot: Callable, engine_identity: Callable,
                    container_layer_limit: str, build_limit: str) -> tuple[list[str], list[Path], str]:
    """Validate one Compose command, write broker-owned snapshots, and return the exact provider argv."""
    if not argv or argv[0] != "compose":
        raise ContainerPolicyError("only docker compose up and run are supported")
    index, files, env_files, project_dir, project_args = _parse_compose_options(
        argv, root, cwd, inside_file=inside_file, inside_directory=inside_directory)
    files = _find_compose_files(files, cwd, root, project_args, inside_file)
    project_dir = project_dir or files[0].parent
    if not within(project_dir, root):
        raise ContainerPolicyError("compose project directory must be inside the worktree")
    all_services, all_volumes, documents = _load_compose_documents(
        files, project_dir, root, read_worktree_file, validate_document)
    _validate_compose_counts(all_services, all_volumes, len(active_containers()))
    subcommand, tail = _validate_compose_subcommand(argv, index, all_services)
    snapshots = _snapshot_compose_inputs(
        documents, files, project_dir, root, runtime_dir, project_args, env_files,
        map_worktree_path, read_worktree_file, compose_snapshot, env_snapshot)
    override = _write_compose_override(runtime_dir, project_name, all_services, engine_identity,
                                       container_layer_limit, build_limit)
    project_args.extend(["-f", str(override), "-p", project_name])
    return [*project_args, subcommand, *tail], snapshots, subcommand
