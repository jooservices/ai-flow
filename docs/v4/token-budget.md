# Token budget per task by size

Each ticket (a task) has a token budget that depends on its size. Phase one only measures: a task above its budget is
listed, and nothing stops or reassigns it. Phase two enforces the budget after the owner has reviewed the numbers.

## Steps

1. **Meter:** the tokens of a task are its completed `dev` and `verify` tokens, summed per ticket by
   `capacity.history`.
2. **Group:** `token_budget.budget_group` maps the ticket size to one of four groups: XS and S are `small`, M is
   `medium`, L is `large`, XL is `huge`. An unknown size is refused, so no task goes unchecked.
3. **Compare:** `token_budget.find_over_budget` counts the checked tasks and lists each task whose tokens are above its
   group's budget, largest overrun first. A task exactly at its budget is not over.

## Budgets

`token_budget.BUDGET_TOKENS` holds the budget of each group. The values are provisional: they are the upper token
bounds of the starting ranges in `capacity.SIZE_BASELINES` until the owner sets them from the measured history.

## Adding to the steps

- A new size label goes into `SIZE_GROUPS`; the test that compares it with `capacity.SIZES` fails until it is mapped.
- Phase two adds the stop or reassignment as a separate step that reads `find_over_budget`; the pure module stays
  free of database and HTTP calls.
