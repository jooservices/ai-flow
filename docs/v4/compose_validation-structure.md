# Compose validation structure

`_validate_compose_document` in `aiflow/v4/worker/container_policy.py` preserves the refusal order. It checks the
document header, named volumes, secret files, config files, networks, then the service count and each service.

Top-level checks are `_validate_compose_document_header`, `_validate_compose_named_volumes`, two explicit calls to
`_validate_compose_resource_files`, `_validate_compose_networks`, and `_validate_compose_services`. Each service goes
through `_validate_compose_service`, which calls named checks for names, privileges, labels, host controls,
inheritance, namespaces, network mode and membership, build settings, environment files, and mounts. Build checks
use `_validate_compose_build_options`, `_validate_compose_build_files`, and `_validate_compose_build_network`; long
mounts go through `_validate_compose_long_volume` and its bind or named-volume check.

Add a rule as a small module-level function beside its related checks, then call it explicitly at the right point in
the sequence. Add literal expected results to `tests/v4/test_compose_validation_parity.py` before changing a rule;
that table keeps the current refusal text and ordering pinned.
