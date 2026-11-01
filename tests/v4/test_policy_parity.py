"""Characterization cases for the brokered container command allowlist."""

import pytest

from aiflow.v4.control.policy import (
    _container_build_arguments_allowed,
    _container_build_value_allowed,
    _container_command_allowed,
    _container_command_arguments,
    _container_compose_arguments_allowed,
    _container_compose_prefix,
    _container_compose_run_allowed,
    _container_compose_up_allowed,
    _container_run_arguments_allowed,
    _container_run_mount_allowed,
    _container_run_mount_option_step,
    _container_run_option_step,
)


@pytest.mark.parametrize(
    ("name", "command", "expected"),
    [
        ('empty', '', False),
        ('whitespace', '   ', False),
        ('wrong executable', 'podman run alpine', False),
        ('docker without subcommand', 'docker', False),
        ('shell expansion', 'docker build $PWD', False),
        ('backtick expansion', 'docker build `pwd`', False),
        ('input redirect', 'docker build < Dockerfile', False),
        ('output redirect', 'docker build > image.tar', False),
        ('newline', 'docker build .\ndocker compose up', False),
        ('unterminated quote', 'docker build "unterminated', False),
        ('unknown subcommand', 'docker pull alpine', False),
        ('build default context', 'docker build .', True),
        ('build nested context', 'docker build src', True),
        ('build absolute in worktree', 'docker build /worktree/src', True),
        ('build no context', 'docker build --quiet', False),
        ('build multiple contexts', 'docker build . src', False),
        ('build all safe option forms', 'docker build --pull --no-cache --quiet -f Dockerfile --file Dockerfile.alt -t app --tag app2 --target final --platform linux/amd64 --build-arg X=1 --progress plain --network default .', True),
        ('build missing option value', 'docker build --tag', False),
        ('build file outside', 'docker build -f ../Dockerfile .', False),
        ('build network none equals', 'docker build --network=none .', True),
        ('build network invalid', 'docker build --network host .', False),
        ('build network invalid equals', 'docker build --network=host .', False),
        ('build unknown option', 'docker build --privileged .', False),
        ('build context outside', 'docker build ../other', False),
        ('run volume root', 'docker run -v /worktree:/src alpine', True),
        ('run volume with mode', 'docker run --volume /worktree:/src:ro alpine', True),
        ('run volume both modes', 'docker run -v /worktree:/src:ro,rw alpine', True),
        ('run mount too many fields', 'docker run -v /worktree:/src:ro:extra alpine', False),
        ('run bind mount src', 'docker run --mount type=bind,src=/worktree,dst=/src alpine', True),
        ('run bind mount source alias', 'docker run --mount=type=bind,source=/worktree,target=/src alpine', True),
        ('run all safe options', 'docker run --rm -i --interactive -t --tty --init --name job --workdir /src -w /src --env X=1 -e Y=2 --publish 80:80 -p 81:81 --expose 82 --entrypoint sh --user 1000 -u 1000 --label a=b --platform linux/amd64 --network bridge --hostname box --dns 1.1.1.1 --pull always --memory 1g --cpus 2 -v /worktree:/src alpine echo ok', True),
        ('run network default', 'docker run --network default -v /worktree:/src alpine', True),
        ('run network none equals', 'docker run --network=none -v /worktree:/src alpine', True),
        ('run network invalid value', 'docker run --network host -v /worktree:/src alpine', False),
        ('run network invalid equals', 'docker run --network=container:other -v /worktree:/src alpine', False),
        ('run missing value option', 'docker run --name', False),
        ('run missing volume value', 'docker run -v alpine', False),
        ('run missing mount value', 'docker run --mount alpine', False),
        ('run unknown option', 'docker run --privileged -v /worktree:/src alpine', False),
        ('run no image', 'docker run -v /worktree:/src', False),
        ('run no mount', 'docker run alpine', False),
        ('run duplicate mounts', 'docker run -v /worktree:/src -v /worktree:/other alpine', False),
        ('run mount source outside', 'docker run -v /:/host alpine', False),
        ('run mount source subdirectory', 'docker run -v /worktree/sub:/src alpine', False),
        ('run invalid volume mode', 'docker run -v /worktree:/src:z alpine', False),
        ('run bind mount wrong type', 'docker run --mount type=volume,src=/worktree,dst=/src alpine', False),
        ('run bind mount propagation', 'docker run --mount type=bind,src=/worktree,dst=/src,bind-propagation=rshared alpine', False),
        ('run bind mount missing source', 'docker run --mount type=bind,dst=/src alpine', False),
        ('run separator accepts invalid mount', 'docker run -v /:/host alpine -- echo ok', True),
        ('run separator needs image and mount', 'docker run -v /worktree:/src -- echo ok', False),
        ('run separator needs one mount', 'docker run alpine -- echo ok', False),
        ('compose up', 'docker compose up', True),
        ('compose run', 'docker compose run composer composer ci', True),
        ('compose rejects project name short', 'docker compose -p app up', False),
        ('compose rejects project name long', 'docker compose --project-name app up', False),
        ('compose file and profile', 'docker compose -f compose.yml --profile test up -d', True),
        ('compose all separate path options', 'docker compose --file compose.yml --env-file .env --project-directory . up', True),
        ('compose all equals path options', 'docker compose --file=compose.yml --env-file=.env --project-directory=. up', True),
        ('compose equals file outside', 'docker compose --file=../compose.yml up', False),
        ('compose outside file', 'docker compose -f ../compose.yml up', False),
        ('compose missing file value', 'docker compose --file', False),
        ('compose invalid equals profile', 'docker compose --profile=test up', False),
        ('compose missing subcommand', 'docker compose', False),
        ('compose unsupported subcommand', 'docker compose down', False),
        ('compose run missing service', 'docker compose run', False),
        ('compose up all safe flags', 'docker compose up -d --detach --abort-on-container-exit --build --no-build --pull --remove-orphans --force-recreate --no-recreate --no-deps --wait --quiet-pull --renew-anon-volumes --timestamps --timeout 10 -t 5 --timeout=20 service', True),
        ('compose up missing timeout value accepted', 'docker compose up --timeout', True),
        ('compose up equals timeout accepted', 'docker compose up --timeout=5', True),
        ('compose up unknown option', 'docker compose up --scale web=2', False),
        ('compose up lone dash', 'docker compose up -', False),
        ('compose run all safe flags and values', 'docker compose run --rm --build --no-deps --quiet-pull -T --no-TTY --service-ports --use-aliases --entrypoint sh --env X=1 -e Y=2 --user 1000 -u 1000 --workdir /src -w /src composer composer ci', True),
        ('compose run equals value options', 'docker compose run --entrypoint=sh --env=X=1 -e=Y=2 --user=1000 -u=1000 --workdir=/src -w=/src composer', True),
        ('compose run value without service', 'docker compose run --env X=1', False),
        ('compose run missing value rejected', 'docker compose run --env', False),
        ('compose run unknown option', 'docker compose run --cap-add SYS_ADMIN composer', False),
        ('compose run options after service are command', 'docker compose run composer --privileged', True),
    ],
)
def test_container_command_decisions_match_the_pre_refactor_table(name, command, expected):
    assert _container_command_allowed(command, "/worktree") is expected, name


def test_container_command_argument_parser():
    assert _container_command_arguments("docker run alpine") == ["run", "alpine"]
    assert _container_command_arguments("docker build $PWD") is None
    assert _container_command_arguments("podman run alpine") is None


def test_container_build_value_check():
    assert _container_build_value_allowed("-f", "Dockerfile", "/worktree")
    assert not _container_build_value_allowed("--file", "../Dockerfile", "/worktree")
    assert _container_build_value_allowed("--network", "none", "/worktree")
    assert not _container_build_value_allowed("--network", "host", "/worktree")
    assert _container_build_value_allowed("--tag", "app", "/worktree")


def test_container_build_argument_check():
    assert _container_build_arguments_allowed(["--pull", "."], "/worktree")
    assert not _container_build_arguments_allowed(["--privileged", "."], "/worktree")
    assert _container_build_arguments_allowed(["."], "/worktree")


def test_container_run_option_step():
    assert _container_run_option_step(["--rm"], 0) == (1, None, False)
    assert _container_run_option_step(["-v", "/worktree:/src"], 0) == (2, "/worktree:/src", False)
    assert _container_run_option_step(["--mount=type=bind,src=/worktree"], 0) == (
        1, "mount=type=bind,src=/worktree", False)
    assert _container_run_option_step(["--network", "host"], 0) is None
    assert _container_run_option_step(["alpine"], 0) == (1, None, True)


def test_container_run_mount_option_step():
    assert _container_run_mount_option_step(["-v", "/worktree:/src"], 0) == (2, "/worktree:/src", False)
    assert _container_run_mount_option_step(["--mount", "type=bind,src=/worktree"], 0) == (
        2, "mount=type=bind,src=/worktree", False)
    assert _container_run_mount_option_step(["--mount=type=bind,src=/worktree"], 0) == (
        1, "mount=type=bind,src=/worktree", False)
    assert _container_run_mount_option_step(["alpine"], 0) is None


def test_container_run_mount_check():
    assert _container_run_mount_allowed("/worktree:/src:ro", "/worktree")
    assert not _container_run_mount_allowed("/:/host", "/worktree")
    assert not _container_run_mount_allowed("/worktree:/src:z", "/worktree")
    assert _container_run_mount_allowed("mount=type=bind,src=/worktree,dst=/src", "/worktree")
    assert not _container_run_mount_allowed(
        "mount=type=bind,src=/worktree,dst=/src,bind-propagation=rshared", "/worktree")


def test_container_run_argument_check_preserves_separator_shortcut():
    assert _container_run_arguments_allowed(["-v", "/worktree:/src", "alpine"], "/worktree")
    assert not _container_run_arguments_allowed(["alpine"], "/worktree")
    assert _container_run_arguments_allowed(["-v", "/:/host", "alpine", "--", "echo"], "/worktree")


def test_container_compose_prefix_check():
    assert _container_compose_prefix(["-f", "compose.yml", "--profile", "test", "up"], "/worktree") == 4
    assert _container_compose_prefix(["--file=../compose.yml", "up"], "/worktree") is None
    assert _container_compose_prefix(["-p", "project", "up"], "/worktree") is None


def test_container_compose_up_check():
    assert _container_compose_up_allowed(["--timeout"])
    assert _container_compose_up_allowed(["--timeout=5", "service"])
    assert not _container_compose_up_allowed(["--scale", "web=2"])


def test_container_compose_run_check():
    assert _container_compose_run_allowed(["--env", "X=1", "composer", "ci"])
    assert not _container_compose_run_allowed(["--cap-add", "SYS_ADMIN", "composer"])
    assert not _container_compose_run_allowed(["--env"])


def test_container_compose_argument_dispatch():
    assert _container_compose_arguments_allowed(["compose", "up", "-d"], "/worktree")
    assert _container_compose_arguments_allowed(["compose", "-f", "compose.yml", "run", "composer"], "/worktree")
    assert not _container_compose_arguments_allowed(["compose", "down"], "/worktree")
