# CLI structure

The v4 CLI is a developer and CI tool. It is not an end-user path; end-user operations belong in Command Center.

## Adding a command

1. Add its parser arguments in the relevant `add_*_commands(sub)` function in `aiflow/v4/cli.py`. Keep parser groups small and preserve defaults, choices, help text, and required flags.
2. Add a `run_<command>(args, settings)` dispatcher for its behavior. Commands that run before settings are loaded can use the existing settings-free path.
3. Add the command name and handler to `_COMMANDS`.
4. Add literal parse cases to `PARSE_CASES` in `tests/v4/test_cli_parser_parity.py`, then review and update `tests/v4/fixtures/cli_parser_spec.json` if the parser specification or help text intentionally changes.
