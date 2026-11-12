# Duplication check

The v4 duplication check scans Python source under `aiflow/v4/`, including nested generated, vendor and fixture
directories. It keeps original line numbers while trimming whitespace and ignoring blank and comment-only lines.

`scan_tree` reads each Python file, `normalize_source` prepares its lines, and `find_duplicates` reports matching
maximal blocks of eight or more lines with stable fingerprints. `evaluate` enforces the shrink-only occurrence
baseline, while `check` compares the working tree with the baseline at the chosen Git base. The local gate and PR
check run the same guard.

To extend the check, add a test for the new scan or baseline rule and keep the existing path, normalization,
fingerprint and occurrence behavior covered. Add no directory-name exclusions unless the scan requirements change.
