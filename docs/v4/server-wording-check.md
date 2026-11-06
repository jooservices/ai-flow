# Server wording check

The intake check rejects a sentence when it directs someone to run, connect, log in or use a named computer or
system. It ignores names and code by themselves, including event fields, dependency lock files and supported systems
such as Ubuntu, the Linux-on-Windows subsystem and macOS. A refusal quotes the sentence and suggests rewriting it as
a product requirement with an observable result.

## Routes

`POST /v1/work/claim` returns `ticket.intake_wording_refusals`, the durable refusal count intake uses to decide whether
to pass through the next rewrite.

## Check steps

1. `RULES` finds candidate names in `aiflow/v4/core/environment.py`.
2. `_is_system_instruction` and `_has_action_target` keep a host, address, machine or network match only when the
   sentence directs an action to it (or names a network with “via” or “through”). `_wording_spans` skips inline and
   fenced code, and `find_server_wording` returns the refused sentence.
3. `rejection_message` names that sentence and provides a requirement rewrite pattern. Intake uses this message when
   it leaves a ticket in Intake.
4. Each refusal stores `server_wording_refusal` in the intake run details. `intake_extras` counts those stored runs;
   after two, `parse` allows the next rewrite through and `refined` records that intake passed it through.

The same rule protects ticket summaries and the open-ticket scan. A ticket author sees a refusal comment on the Jira
ticket while it remains in Intake. After two refusals, intake marks the ticket ready and moves it to To Do with a
comment explaining that it passed through to stop the retry loop.

## Extending the check

Add a narrow candidate pattern to `RULES` and keep the action context check in `_is_system_instruction`. Add a small
case to `tests/v4/test_environment.py` for the candidate and a full rewrite or intake run for its expected outcome.
When adding a new refusal outcome, persist the `server_wording_refusal` marker in run details so the existing per-ticket
count remains durable. Keep product field names, file names, quoted code and supported operating systems in passing
examples.
