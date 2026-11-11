# Triage fault case citations

Triage runs appear in the console Runs table, and study cases are available in Knowledge. When triage receives relevant
study cases about triage or AI Flow faults, its prompt asks it to consider them before deciding. If a case informs the
decision, triage is asked to cite the delivered numeric knowledge item ID and explain its relevance in the existing
`WHY` field. Case text is treated as untrusted data.

This is a prompt request, not an enforced citation requirement or proof that the model followed it. Decisions may be
made without a case citation when no case is relevant or used. Decision paths present results differently, so this does
not promise that every decision's `WHY` appears in a Jira comment. The existing `KNOWLEDGE_USED_IDS` report remains a
separate indication of which delivered items the model reports using.
