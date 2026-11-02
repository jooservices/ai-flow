"""Characterization cases for the compose document policy."""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aiflow.v4.worker import container_policy


def _document(**sections: Any) -> dict[str, Any]:
    return {"services": {"app": {}}, **sections}


def _service(**settings: Any) -> dict[str, Any]:
    return {"services": {"app": settings}}


CASES = [
    pytest.param(None, True, 'error:compose file must contain a mapping', id="document-not-mapping"),
    pytest.param(_document(include=[]), True, 'error:compose include is disabled; all compose inputs must be explicit worktree files', id="include-disabled"),
    pytest.param(_document(volumes="bad"), True, 'error:compose volumes must be a mapping', id="volumes-not-mapping"),
    pytest.param(_document(volumes={f"v{i}": {} for i in range(9)}), True, 'error:compose may declare at most 8 named volumes', id="volume-limit-plus-one"),
    pytest.param(_document(volumes={"bad name": {}}), True, 'error:invalid compose volume declaration', id="invalid-volume-declaration"),
    pytest.param(_document(volumes={"cache": "bad"}), True, 'error:invalid compose volume declaration', id="volume-declaration-not-mapping"),
    pytest.param(_document(volumes={"cache": {"name": "shared"}}), True, 'error:custom compose volume names are not allowed; volumes are named per run', id="custom-volume-name"),
    pytest.param(_document(volumes={"cache": {"external": True}}), True, 'error:external and host-backed named volumes are not allowed', id="host-backed-volume"),
    pytest.param(_document(volumes={"cache": {"driver": "other"}}), True, 'error:external and host-backed named volumes are not allowed', id="custom-volume-driver"),
    pytest.param(_document(volumes={"cache": {"driver_opts": {"device": "/"}}}), True, 'error:external and host-backed named volumes are not allowed', id="volume-driver-options"),
    pytest.param(_document(volumes=[]), True, 'return:1', id="falsey-volumes-normalized"),
    pytest.param(_document(volumes={"cache": None}), True, 'return:1', id="null-volume-normalized"),
    pytest.param(_document(volumes={f"v{i}": {} for i in range(8)}), True, 'return:1', id="volume-limit-boundary"),
    pytest.param(_document(secrets="bad"), True, 'error:compose secrets must be a mapping', id="secrets-not-mapping"),
    pytest.param(_document(configs="bad"), True, 'error:compose configs must be a mapping', id="configs-not-mapping"),
    pytest.param(_document(secrets={"bad name": {}}), True, 'error:invalid compose secrets declaration', id="invalid-secret-declaration"),
    pytest.param(_document(secrets={"app_key": "bad"}), True, 'error:invalid compose secrets declaration', id="secret-declaration-not-mapping"),
    pytest.param(_document(configs={"bad name": {}}), True, 'error:invalid compose configs declaration', id="invalid-config-declaration"),
    pytest.param(_document(configs={"app_config": "bad"}), True, 'error:invalid compose configs declaration', id="config-declaration-not-mapping"),
    pytest.param(_document(secrets={"app_key": {"external": True}}), True, 'error:external compose secrets are not allowed', id="external-secret"),
    pytest.param(_document(configs={"app_config": {"external": True}}), True, 'error:external compose configs are not allowed', id="external-config"),
    pytest.param(_document(secrets={"app_key": {"file": "missing.txt"}}), True, 'error:compose and environment files must exist inside the worktree', id="secret-file-missing"),
    pytest.param(_document(configs={"app_config": {"file": "$HOME/config"}}), True, 'error:file paths must be literal paths inside the worktree', id="config-file-not-literal"),
    pytest.param(_document(secrets={"app_key": {"file": "../../outside-file"}}), True, 'error:compose and environment files must stay inside the worktree', id="secret-file-outside"),
    pytest.param(_document(secrets={"app_key": {"file": "folder"}}), True, 'error:compose and environment files must stay inside the worktree', id="secret-file-is-directory"),
    pytest.param(_document(secrets={"app_key": None}), True, 'return:1', id="null-secret-normalized"),
    pytest.param(_document(networks="bad"), True, 'error:compose networks must be a mapping', id="networks-not-mapping"),
    pytest.param(_document(networks={"bad name": {}}), True, 'error:invalid compose network declaration', id="invalid-network-declaration"),
    pytest.param(_document(networks={"backend": ["bad"]}), True, 'error:invalid compose network declaration', id="network-declaration-not-mapping"),
    pytest.param(_document(networks={"outside": {"external": True}}), True, 'error:external, named, host-backed, and custom compose networks are not allowed', id="external-network"),
    pytest.param(_document(networks={"shared": {"name": "shared"}}), True, 'error:external, named, host-backed, and custom compose networks are not allowed', id="named-network"),
    pytest.param(_document(networks={"custom": {"driver": "host"}}), True, 'error:external, named, host-backed, and custom compose networks are not allowed', id="custom-network-driver"),
    pytest.param(_document(networks={"custom": {"driver_opts": {"device": "/"}}}), True, 'error:external, named, host-backed, and custom compose networks are not allowed', id="network-driver-options"),
    pytest.param(_document(networks={"backend": None}), True, 'return:1', id="null-network-normalized"),
    pytest.param(_document(networks=[]), True, 'return:1', id="falsey-networks-normalized"),
    pytest.param(_document(services="bad"), True, 'error:compose must define between 1 and 8 services', id="services-not-mapping"),
    pytest.param(_document(services={}), True, 'error:compose must define between 1 and 8 services', id="services-required-empty"),
    pytest.param(_document(services={f"svc{i}": {} for i in range(9)}), True, 'error:compose must define between 1 and 8 services', id="service-limit-plus-one"),
    pytest.param(_document(services={f"svc{i}": {} for i in range(8)}), True, 'return:8', id="service-limit-boundary"),
    pytest.param(_document(services={"bad name": {}}), True, 'error:invalid compose service', id="invalid-service-name"),
    pytest.param(_document(services={"app": "bad"}), True, 'error:invalid compose service', id="invalid-service-value"),
    pytest.param(_service(privileged=True), True, 'error:privileged containers, added capabilities, and host devices are not allowed', id="privileged-service"),
    pytest.param(_service(cap_add=["NET_ADMIN"]), True, 'error:privileged containers, added capabilities, and host devices are not allowed', id="added-capabilities"),
    pytest.param(_service(devices=["/dev/example"]), True, 'error:privileged containers, added capabilities, and host devices are not allowed', id="host-devices"),
    pytest.param(_service(device_cgroup_rules=["c 1:3 rwm"]), True, 'error:privileged containers, added capabilities, and host devices are not allowed', id="device-cgroup-rules"),
    pytest.param(_service(labels={"aiflow4.run": "other"}), True, 'error:the aiflow4.run label is reserved for broker cleanup', id="reserved-label-mapping"),
    pytest.param(_service(labels=["aiflow4.run=other"]), True, 'error:the aiflow4.run label is reserved for broker cleanup', id="reserved-label-list"),
    pytest.param(_service(labels="aiflow4.run=other"), True, 'return:1', id="other-label-shape-ignored"),
    pytest.param(_service(volumes_from=["other"]), True, 'error:host container controls and inherited mounts are not allowed', id="inherited-container"),
    pytest.param(_service(security_opt=["label=disable"]), True, 'error:host container controls and inherited mounts are not allowed', id="security-options"),
    pytest.param(_service(runtime="other"), True, 'error:host container controls and inherited mounts are not allowed', id="custom-runtime"),
    pytest.param(_service(extends={"service": "base"}), True, 'error:compose service inheritance and host-side watch settings are not allowed', id="service-inheritance"),
    pytest.param(_service(develop={"watch": []}), True, 'error:compose service inheritance and host-side watch settings are not allowed', id="develop-watch"),
    pytest.param(_service(pid="host"), True, 'error:custom pid namespaces are not allowed', id="pid-namespace"),
    pytest.param(_service(ipc="host"), True, 'error:custom ipc namespaces are not allowed', id="ipc-namespace"),
    pytest.param(_service(uts="host"), True, 'error:custom uts namespaces are not allowed', id="uts-namespace"),
    pytest.param(_service(userns_mode="host"), True, 'error:custom userns_mode namespaces are not allowed', id="userns-namespace"),
    pytest.param(_service(cgroup="host"), True, 'error:custom cgroup namespaces are not allowed', id="cgroup-namespace"),
    pytest.param(_service(network_mode="host"), True, 'error:custom container network modes are not allowed', id="custom-network-mode"),
    pytest.param(_service(networks="default"), True, 'error:compose service networks must be a list or mapping', id="service-networks-invalid-type"),
    pytest.param(_service(networks={"default": "ignored-value"}), True, 'return:1', id="service-networks-mapping"),
    pytest.param(_document(networks={"backend": {}}, services={"app": {"networks": ["unknown"]}}), True,
                 'error:compose services may use only networks declared in this project', id="undeclared-service-network"),
    pytest.param(_service(build={"ssh": ["default"]}), True, 'error:build secrets, ssh agents, and additional contexts are not allowed', id="build-ssh"),
    pytest.param(_service(build={"secrets": ["key"]}), True, 'error:build secrets, ssh agents, and additional contexts are not allowed', id="build-secrets"),
    pytest.param(_service(build={"additional_contexts": {"extra": "."}}), True, 'error:build secrets, ssh agents, and additional contexts are not allowed', id="build-extra-contexts"),
    pytest.param(_service(build={"privileged": True}), True, 'error:build secrets, ssh agents, and additional contexts are not allowed', id="privileged-build"),
    pytest.param(_service(build={"context": "$HOME"}), True, 'error:build contexts must be literal directories inside the worktree', id="build-context-not-literal"),
    pytest.param(_service(build={"context": "missing-context"}), True, 'error:build contexts must exist inside the worktree', id="build-context-missing"),
    pytest.param(_service(build={"context": "../../outside-dir"}), True, 'error:build contexts must stay inside the worktree', id="build-context-outside"),
    pytest.param(_service(build={"context": "inside.txt"}), True, 'error:build contexts must stay inside the worktree', id="build-context-is-file"),
    pytest.param(_service(build=["context"]), True, 'error:build secrets, ssh agents, and additional contexts are not allowed', id="build-definition-not-mapping"),
    pytest.param(_service(build={"context": "context", "dockerfile": "missing-file"}), True,
                 'error:compose and environment files must exist inside the worktree', id="dockerfile-missing"),
    pytest.param(_service(build={"context": ".", "dockerfile": "../../outside-file"}), True,
                 'error:compose and environment files must stay inside the worktree', id="dockerfile-outside"),
    pytest.param(_service(build={"context": ".", "network": "host"}), True, 'error:host networking during build is not allowed', id="build-host-network"),
    pytest.param(_service(env_file="missing.env"), True, 'error:compose and environment files must exist inside the worktree', id="env-file-string-missing"),
    pytest.param(_service(env_file={"path": "$HOME/.env"}), True, 'error:file paths must be literal paths inside the worktree', id="env-file-object-not-literal"),
    pytest.param(_service(env_file=[{"path": "../../outside-file"}]), True, 'error:compose and environment files must stay inside the worktree', id="env-file-object-outside"),
    pytest.param(_service(env_file=[]), True, 'return:1', id="env-file-empty-list"),
    pytest.param(_service(volumes=["/cache"]), True, 'error:anonymous compose volumes are not allowed; declare a quota-backed named volume', id="anonymous-short-volume"),
    pytest.param(_service(volumes=["a:b:c:d"]), True, 'error:invalid compose volume syntax', id="short-volume-invalid-parts"),
    pytest.param(_service(volumes=[":/cache"]), True, 'error:anonymous compose volumes are not allowed; declare a quota-backed named volume', id="anonymous-short-volume-empty-source"),
    pytest.param(_service(volumes=["~/cache:/cache"]), True, 'error:compose bind mounts must use literal worktree paths', id="short-bind-not-literal"),
    pytest.param(_service(volumes=["../../outside-dir:/cache"]), True, 'error:containers may bind only the current worktree', id="short-bind-outside"),
    pytest.param(_service(volumes=["./missing-context:/cache"]), True, 'error:bind mounts must exist inside the worktree', id="short-bind-missing"),
    pytest.param(_service(volumes=["/worktree/$VAR:/cache"]), True, 'error:bind mounts must use literal worktree paths', id="short-bind-not-literal-absolute"),
    pytest.param(_service(volumes=["cache:/cache"]), True, 'error:compose named volumes must be declared in the project', id="undeclared-short-named-volume"),
    pytest.param(_document(volumes={"cache": {}}, services={"app": {"volumes": ["cache:/cache:shared"]}}),
                 True, 'error:compose mount propagation and relabel options are not allowed', id="short-volume-propagation"),
    pytest.param(_service(volumes=[{"type": "bind", "source": "../../outside-dir", "target": "/cache"}]),
                 True, 'error:containers may bind only the current worktree', id="long-bind-outside"),
    pytest.param(_service(volumes=[{"type": "bind", "source": ".", "target": "/cache",
                                    "bind": {"propagation": "shared"}}]), True, 'error:bind propagation is not allowed', id="long-bind-propagation"),
    pytest.param(_service(volumes=[{"type": "volume", "target": "/cache"}]), True, 'error:anonymous compose volumes are not allowed; declare a quota-backed named volume', id="anonymous-long-volume"),
    pytest.param(_service(volumes=[{"type": "volume", "source": "cache", "target": "/cache"}]),
                 True, 'error:compose named volumes must be declared in the project', id="undeclared-long-named-volume"),
    pytest.param(_service(volumes=[{"type": "secret", "source": "key", "target": "/run/key"}]),
                 True, 'error:compose mount type is not allowed: secret', id="disallowed-long-mount-type"),
    pytest.param(_service(volumes=[17]), True, 'error:invalid compose volume', id="invalid-volume-value"),
    pytest.param(_document(services={},), False, 'return:0', id="optional-services-empty"),
    pytest.param(_document(services=None), False, 'return:0', id="optional-services-null"),
    pytest.param(_document(volumes={"cache": {}}, services={"app": {"volumes": ["cache:/cache"]}}), True,
                 'return:1', id="named-volume-valid"),
    pytest.param(_document(volumes={"cache": {}}, services={"app": {"volumes": [
        {"type": "volume", "source": "cache", "target": "/cache"}]}}), True, 'return:1', id="long-named-volume-valid"),
    pytest.param(_document(volumes={"cache": {}}, services={"app": {"volumes": [
        {"source": "cache", "target": "/cache"}]}}), True, 'return:1', id="long-volume-default-type"),
    pytest.param(_service(volumes=["./context:/cache:ro"]), True, 'return:1', id="short-bind-valid"),
    pytest.param(_service(volumes=[{"type": "bind", "source": "context", "target": "/cache"}]),
                 True, 'return:1', id="long-bind-valid"),
    pytest.param(_service(volumes=[{"type": "tmpfs", "target": "/tmp"}]), True, 'return:1', id="tmpfs-valid"),
    pytest.param(_service(networks={"default": {}}), True, 'return:1', id="default-network-mapping-valid"),
    pytest.param(_document(networks={"backend": {}}, services={"app": {"networks": ["backend"]}}), True,
                 'return:1', id="declared-service-network-valid"),
    pytest.param(_document(secrets={"app_key": {"file": "inside.txt"}},
                           configs={"app_config": {"file": "inside.txt"}}), True, 'return:1', id="files-valid"),
    pytest.param(_service(build="context"), True, 'return:1', id="build-string-valid"),
    pytest.param(_service(build={"context": "context", "dockerfile": "../inside.txt", "network": "none"}),
                 True, 'return:1', id="build-options-valid"),
    pytest.param(_service(env_file=["inside.txt", {"path": "inside.txt"}]), True, 'return:1', id="env-file-list-valid"),
    pytest.param(_service(env_file="inside.txt"), True, 'return:1', id="env-file-string-valid"),
    pytest.param(_document(), True, 'return:1', id="minimal-valid"),
]


HELPER_CASES = [
    ("_validate_compose_document_header", 'error:compose include is disabled; all compose inputs must be explicit worktree files'),
    ("_validate_compose_named_volumes", 'error:compose volumes must be a mapping'),
    ("_validate_compose_named_volume", 'error:invalid compose volume declaration'),
    ("_validate_compose_volume_name", 'error:custom compose volume names are not allowed; volumes are named per run'),
    ("_validate_compose_volume_backend", 'error:external and host-backed named volumes are not allowed'),
    ("_validate_compose_resource_files", 'error:compose secrets must be a mapping'),
    ("_validate_compose_networks", 'error:compose networks must be a mapping'),
    ("_validate_compose_services", 'error:compose must define between 1 and 8 services'),
    ("_validate_compose_service_name", 'error:invalid compose service'),
    ("_validate_compose_service_privileges", 'error:privileged containers, added capabilities, and host devices are not allowed'),
    ("_validate_compose_service_labels", 'error:the aiflow4.run label is reserved for broker cleanup'),
    ("_validate_compose_service_host_controls", 'error:host container controls and inherited mounts are not allowed'),
    ("_validate_compose_service_inheritance", 'error:compose service inheritance and host-side watch settings are not allowed'),
    ("_validate_compose_service_namespaces", 'error:custom pid namespaces are not allowed'),
    ("_validate_compose_service_network_mode", 'error:custom container network modes are not allowed'),
    ("_validate_compose_service_networks", 'error:compose services may use only networks declared in this project'),
    ("_validate_compose_service_build", 'error:build secrets, ssh agents, and additional contexts are not allowed'),
    ("_validate_compose_build_options", 'error:build secrets, ssh agents, and additional contexts are not allowed'),
    ("_validate_compose_build_files", 'error:build contexts must exist inside the worktree'),
    ("_validate_compose_build_network", 'error:host networking during build is not allowed'),
    ("_validate_compose_service_env_files", 'error:compose and environment files must exist inside the worktree'),
    ("_validate_compose_service_volumes", 'error:invalid compose volume'),
    ("_validate_compose_service_volume", 'error:invalid compose volume'),
    ("_validate_compose_long_volume", 'error:compose mount type is not allowed: secret'),
    ("_validate_compose_bind_volume", 'error:containers may bind only the current worktree'),
    ("_validate_compose_long_named_volume", 'error:anonymous compose volumes are not allowed; declare a quota-backed named volume'),
    ("_validate_compose_service", 'error:invalid compose service'),
    ("_validate_compose_document", 'error:compose file must contain a mapping'),
]


def _helper_arguments(root: Path, project_dir: Path) -> dict[str, tuple[Any, ...]]:
    outside = root.parent / "outside-dir"
    return {
        "_validate_compose_document_header": ({"include": None},),
        "_validate_compose_named_volumes": ({"volumes": "bad"},),
        "_validate_compose_named_volume": ("bad name", {}),
        "_validate_compose_volume_name": ({"name": "shared"},),
        "_validate_compose_volume_backend": ({"external": True},),
        "_validate_compose_resource_files": ({"secrets": "bad"}, "secrets", root, project_dir),
        "_validate_compose_networks": ({"networks": "bad"},),
        "_validate_compose_services": ({"services": {}}, True),
        "_validate_compose_service_name": ("bad name", {}),
        "_validate_compose_service_privileges": ({"privileged": True},),
        "_validate_compose_service_labels": ({"labels": ["aiflow4.run=other"]},),
        "_validate_compose_service_host_controls": ({"runtime": "other"},),
        "_validate_compose_service_inheritance": ({"extends": {"service": "base"}},),
        "_validate_compose_service_namespaces": ({"pid": "host"},),
        "_validate_compose_service_network_mode": ({"network_mode": "host"},),
        "_validate_compose_service_networks": ({"networks": ["unknown"]}, {}),
        "_validate_compose_service_build": ({"build": {"ssh": ["default"]}}, root, project_dir),
        "_validate_compose_build_options": ({"ssh": ["default"]},),
        "_validate_compose_build_files": ({"context": "missing"}, root, project_dir),
        "_validate_compose_build_network": ({"network": "host"},),
        "_validate_compose_service_env_files": ({"env_file": "missing.env"}, root, project_dir),
        "_validate_compose_service_volumes": ({"volumes": [17]}, project_dir, root, set()),
        "_validate_compose_service_volume": (17, project_dir, root, set()),
        "_validate_compose_long_volume": ({"type": "secret"}, project_dir, root, set()),
        "_validate_compose_bind_volume": ({"source": "../../outside-dir"}, project_dir, root),
        "_validate_compose_long_named_volume": ({}, set()),
        "_validate_compose_service": ("bad name", {}, set(), {}, root, project_dir),
        "_validate_compose_document": (None, root / "compose.yaml", project_dir, root),
    }


def _format_call(call, *args, **kwargs) -> str:
    try:
        result = call(*args, **kwargs)
    except container_policy.ContainerPolicyError as exc:
        return f"error:{exc}"
    return f"return:{result}"


def _format_result(data: Any, compose_file: Path, project_dir: Path, root: Path, require_services: bool) -> str:
    return _format_call(container_policy._validate_compose_document,
                        data, compose_file, project_dir, root, require_services=require_services)


@pytest.mark.parametrize(("helper_name", "expected"), HELPER_CASES)
def test_each_compose_validation_helper_without_services(tmp_path, helper_name, expected):
    root = tmp_path / "worktree"
    project_dir = root / "project"
    project_dir.mkdir(parents=True)
    (tmp_path / "outside-dir").mkdir()
    call = getattr(container_policy, helper_name)
    actual = _format_call(call, *_helper_arguments(root, project_dir)[helper_name])
    assert actual == expected


@pytest.mark.parametrize(("data", "require_services", "expected"), CASES)
def test_compose_validation_matches_characterized_outputs(tmp_path, data, require_services, expected):
    root = tmp_path / "worktree"
    root.mkdir()
    project_dir = root / "project"
    project_dir.mkdir()
    (project_dir / "inside.txt").write_text("plain fixture data")
    (project_dir / "context").mkdir()
    (project_dir / "folder").mkdir()
    (tmp_path / "outside-file").write_text("plain fixture data")
    (tmp_path / "outside-dir").mkdir()
    actual = _format_result(data, root / "compose.yaml", project_dir, root, require_services)
    assert actual == expected
