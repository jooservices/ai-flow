# JAIF-513 canary decision characterization

This page records the canary decision for the JAIF-513 incident counts under an explicit example policy. The regression
test calls `aiflow.v4.control.canary.decide` directly; it does not change production thresholds or behavior.

## Recorded counts

Candidate `c850e5419e` was tested on canary node `node1`, with previous release `b6a49908b1`. Over 24 hours, the
canary had 4 infrastructure failures in 7 runs and the baseline had 3 in 16 runs. The suspected commit
`c850e5419e8e8a04c0e3a5969768c92c6609fd8b` is not a proven cause of those failures.

## Explicit example policy

The test uses `Thresholds.from_values(300, 3, 900, "2", 3)`: minimum age 300 seconds, minimum 3 runs, maximum age
900 seconds, failure ratio 2, and minimum 3 infrastructure failures. With elapsed time 0 and the candidate installed,
the counts produce `rollback` with cause `failures`.

Keeping every other input the same and setting the failure ratio to `"4"` produces `wait`. These are results under
the stated example policy. The active incident thresholds were not recorded, and neither those thresholds nor the
cause of the failures can be inferred from the counts alone.

## How to extend

Keep this characterization tied to the explicit threshold values and recorded counts. Change the example policy only
when new evidence supports it; investigate the incident cause separately from this regression test.
