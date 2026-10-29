# Epics and verification without a verdict

Epics are containers, not work. An epic is never offered as intake, backlog, development, verification, triage or
post-merge work, whatever its labels (`GET /v1/work/candidates` omits it and `POST /v1/work/claim` refuses it as not
eligible), and the idle-flow check ignores it (JAIF-439). The remaining parts of JAIF-271
(epic status from its children, verification retried on another account, the incident and the Dashboard list) follow in
JAIF-440 to JAIF-444.
