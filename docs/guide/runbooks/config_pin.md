# Runbook: config_pin

**Assumes:** you are an Owner or Super Administrator with write access to the `aiflow-control` repository. Raised in `control/incidents.py`.

## Symptom

Critical incident "versions.lock has no pin for TOOL (PROJECT)". The project gets no tickets on any node.

## Cause

The project declares a stack (for example PHP) that needs a tool, but `aiflow-control/versions.lock` has no
`tools.TOOL` entry. Without a pin no node can be sure to install the right version.

## Fix

1. In `aiflow-control/versions.lock` add `tools.TOOL` with a version or a release line such as `8.3`.
2. Wait for the next control repository refresh. The incident closes with "the pin exists", and nodes install the tool ([node_tools](node_tools.md)).
3. Or remove the stack from the project if it is not needed ([Projects](../screens/projects.md)).
