# SQL boundary check

The pull-request guard scans Python files under `aiflow/v4/` and finds SQL in AST string constants, including f-string
constant parts and implicit concatenations. It skips module, class and function docstrings. The baseline records the
current count per file; new files and increases fail, while lower counts are reported and the baseline can only shrink.

SQL belongs in aggregate repositories (`control/features/*/repository.py` and `control/repositories/*.py`) or the
database boundary (`control/db.py`, `control/platform/db.py`, `control/platform/session.py`, and `control/platform/data/`).
These allowed paths are listed in `aiflow/v4/core/sql_boundary.py`. Existing SQL elsewhere is tracked in
`sql-outside-data-access-baseline.json` until its owning work moves it to a repository.

The CI job is `SQL boundary`; the matching local gate step is `sql-boundary`. To add a data-access package, update the
guard's allowed path patterns to match the architecture, and add focused scanner tests. Do not increase or add entries
to the baseline; reduce counts when SQL is moved or removed.
