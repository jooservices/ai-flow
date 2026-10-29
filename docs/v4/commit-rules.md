# Commit and pull request title rules

AI Flow never sends a title or commit message that the repository would reject. `aiflow/v4/core/commit_rules.py` holds
the rules of `.github/commitlint.config.mjs` (type list, lower-case type, non-empty subject, header of 10 to 120
characters, no trailing full stop, subject not starting in lower case) and the pull request pattern of
`.github/workflows/semantic-pr.yml` (the subject starts with an uppercase ASCII letter).

- `normalize_title(kind, key, summary)` builds the title: it cuts only at word boundaries, stays within 100 characters,
  removes trailing punctuation, capitalizes the subject and always satisfies the rules. A subject that does not start
  with an ASCII capital (for example Vietnamese) starts with the ticket key: `feat: JAIF-1: subject`.
- The developer worker validates the title and the commit message before it commits. A violation that survives
  normalization ends the run as `failed` with error kind `invalid_title`; nothing is committed or pushed.
- `tests/v4/test_commit_rules.py` keeps a fixture of headers checked against the real commitlint. When the commitlint
  config changes, regenerate the fixture and update the module together.
