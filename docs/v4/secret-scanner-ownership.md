# Shared secret scanner

The ordered patterns, `Rejected` exception, and `scan_secrets` function live in `aiflow.v4.core.secret_scan`. The
module uses only Python's regular-expression library and has no dependency on legacy memory or control code. The
legacy `aiflow.memory.guard` module re-exports the same objects so existing memory and rescue callers keep their
imports and behavior. Owner escalation notifications import directly from v4 core.

To recognize another secret format, add its compiled expression to `SECRETS` in the intended priority order and add
a synthetic positive case to `tests/v4/test_secret_scan.py`. Keep rejection text free of the matched value and cover
the new format through both public import paths.
