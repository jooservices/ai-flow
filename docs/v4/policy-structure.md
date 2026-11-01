# Container command policy structure

`_container_command_allowed` parses a command with `_container_command_arguments`, then explicitly dispatches to the
build, run or compose checks. Build values and contexts are checked by `_container_build_value_allowed` and
`_container_build_arguments_allowed`. Run options and mounts are checked by `_container_run_mount_option_step`,
`_container_run_option_step`, `_container_run_mount_allowed` and `_container_run_arguments_allowed`. Compose options,
`up` flags and `run` flags are
checked by `_container_compose_prefix`, `_container_compose_up_allowed`, `_container_compose_run_allowed` and
`_container_compose_arguments_allowed`.

To add a permitted command form, add a named check for its rule and call it explicitly from the dispatcher or the
owning subcommand check. Keep the existing check order, add literal characterization cases for allowed and refused
forms, and add direct unit coverage for the new helper.

## Route

The route is unchanged: `POST /v1/policy/decide` asks whether a command is allowed. The container command check described above is one of the rules it evaluates.
