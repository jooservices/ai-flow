# Shared worker fairness

Workers that serve more than one Jira project interleave ready tickets by project. Jira still provides each
project's ticket order, sorted by priority descending and then creation time ascending. The supervisor takes the
first ready ticket from each project before taking a second from any project, preserving that within-project order.

Rework tickets keep their existing preference over regular ready tickets. The project round-robin applies within
the rework group first and then within the regular group, giving each represented project a turn before any one
project gets its second ticket in that group. After a successful step, the supervisor records which project was
served so the next round starts with the project that has waited longest.

This bounds fairness by candidate turns, not elapsed wall-clock time. Eligibility, worker availability and the
duration of a step can still affect when a ticket runs.
