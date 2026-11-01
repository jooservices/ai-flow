# Review metrics

The review metrics summarize weekly waits for human review or an owner, finished tickets without recorded human-review activity, wait durations, and review decisions. `calculate_review_metrics` keeps the result stable while named helpers group rows, classify transitions, measure intervals, summarize decisions, and build suggestions; add a new metric as a helper with its own test.
