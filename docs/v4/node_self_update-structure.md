# Node self-update structure

The node agent's `self_update` in `aiflow/v4/node/release.py` keeps the channel and version gates, the ten-minute
retry memory and the incoming-directory setup. It then calls these named steps in order:

1. `_fetch_release_manifest` downloads the manifest and validates its version, checksum and versioned asset paths.
2. `_download_release_assets` downloads the tarball and signature bundle, writes them atomically, checks the SHA256
   checksum and verifies the cosign signature.
3. `_schedule_release_install` starts the systemd installer unit with the verified files and checksum.

Every rejected release goes through `_reject_release`, which logs the reason and reports the release failure to the
control plane. The develop channel remains a separate `self_update_develop` path and keeps its own retry memory.

To add or change a step, call it explicitly from `self_update` or from the owning step in the order above, add a
direct unit test for the step, and add a representative input and literal expected output to
`tests/v4/test_node_self_update_parity.py` and its fixture.
