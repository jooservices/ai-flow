# Broad exception guard

The `broad-except` step checks Python handlers under `aiflow/v4/` in CI and the local gate. It counts `except Exception`,
`except BaseException`, and bare `except:` handlers unless a nearby comment gives a reason of at least 12 characters
after a dash or colon. A comment may be on the handler line, the previous line, or the first body line.

`docs/v4/broad-except-baseline.json` records the current unexplained counts per file. New files and increased counts fail;
reductions are reported so the corresponding baseline entries can be lowered. When the PR base has no baseline yet, the
first baseline may contain only findings already present in that base's source tree. After that, it can only shrink
compared with the PR base. To refresh it from the source tree used for the first baseline, run:

```sh
python3 -m aiflow.v4.core.broad_except baseline --from origin/develop --write
```

The local gate step is named `broad-except`; the matching CI job is `Broad exceptions`. To add another invocation, keep
the command in sync in `aiflow/v4/core/local_gate.py` and `.github/workflows/ci.yml`.
