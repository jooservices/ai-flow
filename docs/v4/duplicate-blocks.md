# Duplicate block check

The `duplicate-blocks` step scans Python files under `aiflow/v4/`. It trims each line and ignores blank and
comment-only lines, then reports maximal blocks of at least eight identical normalized lines with each file and
starting line. Generated, vendored and fixture directories are skipped.

The JSON baseline records a fingerprint and occurrence count for each existing block. The check fails on a new
fingerprint, a higher occurrence count, or a baseline that adds entries compared with its base revision. It reports
baseline entries whose blocks disappeared or became less common so those entries can be removed or reduced.

CI names the job `duplicate-blocks`; the matching local gate step has the same name and runs next to `giant-files`.
When adding a guard, give its local gate step the same `ci_job` name, add its CI job beside the other guards, and
cover the matching command in a parity test. Keep the baseline shrink-only.
