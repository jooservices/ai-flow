"""Literal characterization of the GitHub project conditions before extraction."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from aiflow.v4.control import gateway_helpers, route_deps, system_check


_REQUIRED_PERMISSIONS = {
    "contents": "write", "pull_requests": "write", "workflows": "write",
    "actions": "read", "checks": "read", "statuses": "read", "administration": "write",
}
_APP = {"id": "app-1", "account": "acme", "spec": {"repos": [{"full_name": "acme/repo"}]}}
_APP_COMPLETE = {**_APP, "spec": {"repos": [{"full_name": "acme/repo"}], "permissions": _REQUIRED_PERMISSIONS}}

_CASES = [
    {"name":"pending-empty", "repos":[], "provider":False},
    {"name":"pending-one", "repos":["acme/repo"], "provider":False, "bases":{"acme/repo":"main"}},
    {"name":"pending-filters", "repos":["", None, 7, "acme/repo", "Acme/Repo"], "provider":False},
    {"name":"list-error", "repos":["acme/repo"], "list_error":True, "bases":{"acme/repo":"main"}},
    {"name":"assignments-error", "repos":["acme/repo"], "connections":[_APP_COMPLETE], "assignments_error":True, "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"empty-repos-connection", "repos":[], "connections":[_APP_COMPLETE]},
    {"name":"empty-repos-no-connection", "repos":[]},
    {"name":"route-failure-no-fallback", "repos":["acme/repo"], "bases":{"acme/repo":"main"}},
    {"name":"route-owner-fallback", "repos":["Acme/repo"], "connections":[_APP_COMPLETE], "bases":{"Acme/repo":"main"}},
    {"name":"route-assignment-fallback", "repos":["other/repo"], "connections":[{"id":"app-1","account":"anything","spec":{"repos":[{"full_name":"other/repo"}]}}], "assignments":{"other/repo":"app-1"}, "bases":{"other/repo":"main"}, "routes":{"other/repo":"raise"}},
    {"name":"route-returns-none", "repos":["acme/repo"], "connections":[_APP_COMPLETE], "routes":{"acme/repo":None}, "bases":{"acme/repo":"main"}},
    {"name":"installation-error", "repos":["acme/repo"], "connections":[_APP_COMPLETE], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}, "connection_spec_patch":{"repo_error":True}},
    {"name":"installation-malformed-rows", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[None, {"name":"acme/repo"}, {"full_name":""}], "permissions":_REQUIRED_PERMISSIONS}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"token-full", "repos":["acme/repo"], "connections":[{"id":"token-1","account":"acme","spec":{"kind":"token","repos":[{"full_name":"ACME/REPO"}]}}], "routes":{"acme/repo":"token-1"}, "bases":{"acme/repo":"main"}},
    {"name":"token-no-access", "repos":["acme/repo"], "connections":[{"id":"token-1","account":"acme","spec":{"kind":"token","repos":[]}}], "routes":{"acme/repo":"token-1"}, "bases":{"acme/repo":"main"}},
    {"name":"app-no-installation-id", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[{"full_name":"acme/repo"}], "meta":{}}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"permissions-error", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[{"full_name":"acme/repo"}], "permission_error":True}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"permissions-missing-one", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[{"full_name":"acme/repo"}], "permissions":{**_REQUIRED_PERMISSIONS,"workflows":"read"}}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"permissions-missing-many", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[{"full_name":"acme/repo"}], "permissions":{"contents":"read","statuses":"write"}}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"base-unconfigured", "repos":["acme/repo"], "connections":[_APP_COMPLETE], "routes":{"acme/repo":"app-1"}},
    {"name":"repository-inaccessible", "repos":["acme/repo"], "connections":[_APP_COMPLETE], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}, "connection_spec_patch":{"repos":[]}},
    {"name":"base-missing", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[{"full_name":"acme/repo"}], "permissions":_REQUIRED_PERMISSIONS, "branches":{("repo","main"):False}}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"base-error", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[{"full_name":"acme/repo"}], "permissions":_REQUIRED_PERMISSIONS, "branch_errors":["repo"]}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},
    {"name":"multiple-app-complete", "repos":["acme/repo","beta/other"], "connections":[_APP_COMPLETE,{"id":"app-2","account":"beta","spec":{"repos":[{"full_name":"beta/other"}], "permissions":_REQUIRED_PERMISSIONS}}], "routes":{"acme/repo":"app-1","beta/other":"app-2"}, "bases":{"acme/repo":"main","beta/other":"main"}},
    {"name":"multiple-app-one-permission-error", "repos":["acme/repo","beta/other"], "connections":[_APP_COMPLETE,{"id":"app-2","account":"beta","spec":{"repos":[{"full_name":"beta/other"}], "permission_error":True}}], "routes":{"acme/repo":"app-1","beta/other":"app-2"}, "bases":{"acme/repo":"main","beta/other":"main"}},
    {"name":"multiple-app-missing-one", "repos":["acme/repo","beta/other"], "connections":[_APP_COMPLETE,{"id":"app-2","account":"beta","spec":{"repos":[{"full_name":"beta/other"}], "permissions":{**_REQUIRED_PERMISSIONS,"contents":"read"}}}], "routes":{"acme/repo":"app-1","beta/other":"app-2"}, "bases":{"acme/repo":"main","beta/other":"main"}},
    {"name":"assignments-error-discards-connections", "repos":["acme/repo"], "connections":[_APP_COMPLETE], "assignments_error":True, "routes":{"acme/repo":"raise"}, "bases":{"acme/repo":"main"}},
    {"name":"unknown-assignment-blocks-owner-fallback", "repos":["acme/repo"], "connections":[_APP_COMPLETE], "assignments":{"acme/repo":"missing"}, "routes":{"acme/repo":"raise"}, "bases":{"acme/repo":"main"}},
    {"name":"invalid-installation-id", "repos":["acme/repo"], "connections":[{"id":"app-1","account":"acme","spec":{"repos":[{"full_name":"acme/repo"}], "permissions":_REQUIRED_PERMISSIONS, "meta":{"installation_id":"not-an-int"}}}], "routes":{"acme/repo":"app-1"}, "bases":{"acme/repo":"main"}},]

_EXPECTED = {
    'pending-empty': '[{"id":"github-installation","name":"GitHub installation","result":"needs_action","detail":"The GitHub installation check has not completed.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub permissions check has not completed.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true}]',
    'pending-one': '[{"id":"github-installation","name":"GitHub installation","result":"needs_action","detail":"The GitHub installation check has not completed.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub permissions check has not completed.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access has not been verified.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch has not been verified.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true}]',
    'pending-filters': '[{"id":"github-installation","name":"GitHub installation","result":"needs_action","detail":"The GitHub installation check has not completed.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub permissions check has not completed.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access has not been verified.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch has not been verified.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"repository-access:Acme/Repo","name":"Repository access · Acme/Repo","result":"needs_action","detail":"Repository access has not been verified.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true},{"id":"base-branch:Acme/Repo","name":"Base branch · Acme/Repo","result":"needs_action","detail":"The configured base branch has not been verified.","action":"Wait for the provider-backed System check to finish before starting work.","affects_work":true}]',
    'list-error': '[{"id":"github-installation","name":"GitHub connection","result":"needs_action","detail":"A GitHub connection for this project could not be verified.","action":"Connect GitHub or assign a working connection to each repository in the Projects screen.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"GitHub App permissions could not be verified for the selected connection.","action":"Reconnect or assign a GitHub App connection in the Projects screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access could not be verified.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'assignments-error': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'empty-repos-connection': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"A GitHub connection is available for this project.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"No GitHub App permissions are required until a repository is configured.","action":"","affects_work":true}]',
    'empty-repos-no-connection': '[{"id":"github-installation","name":"GitHub connection","result":"needs_action","detail":"A GitHub connection for this project could not be verified.","action":"Connect GitHub or assign a working connection to each repository in the Projects screen.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"No GitHub App permissions are required until a repository is configured.","action":"","affects_work":true}]',
    'route-failure-no-fallback': '[{"id":"github-installation","name":"GitHub connection","result":"needs_action","detail":"A GitHub connection for this project could not be verified.","action":"Connect GitHub or assign a working connection to each repository in the Projects screen.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"GitHub App permissions could not be verified for the selected connection.","action":"Reconnect or assign a GitHub App connection in the Projects screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access could not be verified.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'route-owner-fallback': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:Acme/repo","name":"Repository access · Acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:Acme/repo","name":"Base branch · Acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'route-assignment-fallback': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:other/repo","name":"Repository access · other/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:other/repo","name":"Base branch · other/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'route-returns-none': '[{"id":"github-installation","name":"GitHub connection","result":"needs_action","detail":"A GitHub connection for this project could not be verified.","action":"Connect GitHub or assign a working connection to each repository in the Projects screen.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"GitHub App permissions could not be verified for the selected connection.","action":"Reconnect or assign a GitHub App connection in the Projects screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access could not be verified.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'installation-error': '[{"id":"github-installation","name":"GitHub connection","result":"needs_action","detail":"A GitHub connection for this project could not be verified.","action":"Connect GitHub or assign a working connection to each repository in the Projects screen.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access could not be verified.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'installation-malformed-rows': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"The selected GitHub connection cannot access the configured repository.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'token-full': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"This project uses verified GitHub token connections, so GitHub App permissions do not apply.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'token-no-access': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"This project uses verified GitHub token connections, so GitHub App permissions do not apply.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"The selected GitHub connection cannot access the configured repository.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'app-no-installation-id': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub App installation permissions could not be verified.","action":"Reconnect the GitHub App on the Integrations screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'permissions-error': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub App installation permissions could not be verified.","action":"Reconnect the GitHub App on the Integrations screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'permissions-missing-one': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub App installation is missing these required permissions: Workflows: write.","action":"Grant Workflows: write to the GitHub App installation in GitHub settings.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'permissions-missing-many': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub App installation is missing these required permissions: Actions: read, Administration: write, Checks: read, Contents: write, Pull requests: write, Statuses: read, Workflows: write.","action":"Grant Actions: read, Administration: write, Checks: read, Contents: write, Pull requests: write, Statuses: read, Workflows: write to the GitHub App installation in GitHub settings.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'base-unconfigured': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"A base branch is not configured for this repository.","action":"Configure a base branch for this repository in the Projects screen.","affects_work":true}]',
    'repository-inaccessible': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"The selected GitHub connection cannot access the configured repository.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'base-missing': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch does not exist in the repository.","action":"Create the configured base branch in the repository and rerun the System check.","affects_work":true}]',
    'base-error': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked.","action":"Restore the selected GitHub connection and rerun the System check.","affects_work":true}]',
    'multiple-app-complete': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"ready","detail":"Every selected GitHub App installation has the permissions AI Flow requires.","action":"","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true},{"id":"repository-access:beta/other","name":"Repository access · beta/other","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:beta/other","name":"Base branch · beta/other","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'multiple-app-one-permission-error': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub App installation permissions could not be verified.","action":"Reconnect the GitHub App on the Integrations screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true},{"id":"repository-access:beta/other","name":"Repository access · beta/other","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:beta/other","name":"Base branch · beta/other","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'multiple-app-missing-one': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub App installation is missing these required permissions: Contents: write.","action":"Grant Contents: write to each selected GitHub App installation in GitHub settings.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true},{"id":"repository-access:beta/other","name":"Repository access · beta/other","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:beta/other","name":"Base branch · beta/other","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
    'assignments-error-discards-connections': '[{"id":"github-installation","name":"GitHub connection","result":"needs_action","detail":"A GitHub connection for this project could not be verified.","action":"Connect GitHub or assign a working connection to each repository in the Projects screen.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"GitHub App permissions could not be verified for the selected connection.","action":"Reconnect or assign a GitHub App connection in the Projects screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access could not be verified.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'unknown-assignment-blocks-owner-fallback': '[{"id":"github-installation","name":"GitHub connection","result":"needs_action","detail":"A GitHub connection for this project could not be verified.","action":"Connect GitHub or assign a working connection to each repository in the Projects screen.","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"GitHub App permissions could not be verified for the selected connection.","action":"Reconnect or assign a GitHub App connection in the Projects screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"needs_action","detail":"Repository access could not be verified.","action":"Assign a GitHub connection with access to this repository in the Projects screen.","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"needs_action","detail":"The configured base branch could not be checked without repository access.","action":"Assign a GitHub connection with access to this repository and rerun the System check.","affects_work":true}]',
    'invalid-installation-id': '[{"id":"github-installation","name":"GitHub connection","result":"ready","detail":"AI Flow verified the GitHub connection selected for every configured repository.","action":"","affects_work":true},{"id":"github-app-permissions","name":"GitHub App permissions","result":"needs_action","detail":"The GitHub App installation permissions could not be verified.","action":"Reconnect the GitHub App on the Integrations screen and rerun the System check.","affects_work":true},{"id":"repository-access:acme/repo","name":"Repository access · acme/repo","result":"ready","detail":"The selected GitHub connection can access the configured repository.","action":"","affects_work":true},{"id":"base-branch:acme/repo","name":"Base branch · acme/repo","result":"ready","detail":"The configured base branch exists in the repository.","action":"","affects_work":true}]',
}


class _Gateway:
    def __init__(self, spec: dict):
        self.spec = spec
        self.kind = spec.get("kind", "app")
        self.meta = spec.get("meta", {"installation_id": "1"})

    def installation_repositories(self):
        if self.spec.get("repo_error"):
            raise RuntimeError("PRIVATE provider response")
        return self.spec.get("repos", [])

    def installation_details(self, installation):
        if self.spec.get("permission_error"):
            raise RuntimeError("PRIVATE provider response")
        return self.spec.get("details", {"permissions": self.spec.get("permissions", _REQUIRED_PERMISSIONS)})

    def branch_exists(self, repository, base):
        if repository in self.spec.get("branch_errors", []):
            raise RuntimeError("PRIVATE provider response")
        return self.spec.get("branches", {}).get((repository, base), True)


def _prepare_case(case: dict, monkeypatch):
    connections = []
    for item in case.get("connections", []):
        spec = dict(item["spec"])
        if case.get("connection_spec_patch") and item["id"] == "app-1":
            spec.update(case["connection_spec_patch"])
        connections.append(SimpleNamespace(id=item["id"], account=item["account"], gateway=_Gateway(spec)))

    def connection_list(_ctx):
        if case.get("list_error"):
            raise RuntimeError("PRIVATE connection list")
        return connections

    def registry(_ctx):
        def assignments():
            if case.get("assignments_error"):
                raise RuntimeError("PRIVATE assignments")
            return case.get("assignments", {})
        return SimpleNamespace(assignments=assignments)

    def gateway(_ctx, repository):
        selected = case.get("routes", {}).get(repository, "raise")
        if selected == "raise":
            raise RuntimeError("PRIVATE route detail")
        if selected is None:
            return None
        connection = next(item for item in connections if item.id == selected)
        route_deps.github_connection_used.set(connection.id)
        return connection.gateway

    monkeypatch.setattr(gateway_helpers, "github_connection_list", connection_list, raising=False)
    monkeypatch.setattr(gateway_helpers, "github_connections", registry, raising=False)
    monkeypatch.setattr(gateway_helpers, "github_gateway", gateway, raising=False)
    bases = case.get("bases", {})
    project = SimpleNamespace(
        configured_repositories=case.get("repos", []),
        settings_for_repo=lambda repository: {"base": bases.get(repository, "")},
    )
    ctx = SimpleNamespace(case=case, connections=connections, system_check_integration_cache={})
    return ctx, project


@pytest.mark.parametrize("case", _CASES, ids=lambda case: case["name"])
def test_github_project_conditions_match_pre_refactor_output(case, monkeypatch):
    """Keep literal output for pending, routing, provider, permission and branch paths."""
    ctx, project = _prepare_case(case, monkeypatch)

    actual = system_check._github_project_conditions(ctx, project, "manual", case.get("provider", True))

    assert actual == json.loads(_EXPECTED[case["name"]])
_CONDITION_BUILDER_CASES = [
    pytest.param(system_check._github_pending_installation_condition,
                 ("Wait for the provider-backed System check to finish before starting work.",),
                 "pending-one", 0, id="pending-installation"),
    pytest.param(system_check._github_pending_permissions_condition,
                 ("Wait for the provider-backed System check to finish before starting work.",),
                 "pending-one", 1, id="pending-permissions"),
    pytest.param(system_check._github_pending_repository_access_condition,
                 ("acme/repo", "Wait for the provider-backed System check to finish before starting work."),
                 "pending-one", 2, id="pending-repository-access"),
    pytest.param(system_check._github_pending_base_branch_condition,
                 ("acme/repo", "Wait for the provider-backed System check to finish before starting work."),
                 "pending-one", 3, id="pending-base-branch"),
    pytest.param(system_check._github_verified_installation_condition, (),
                 "assignments-error", 0, id="verified-installation"),
    pytest.param(system_check._github_available_connection_condition, (),
                 "empty-repos-connection", 0, id="available-connection"),
    pytest.param(system_check._github_unverified_connection_condition, (),
                 "list-error", 0, id="unverified-connection"),
    pytest.param(system_check._github_token_permissions_condition, (),
                 "token-full", 1, id="token-permissions"),
    pytest.param(system_check._github_no_repository_permissions_condition, (),
                 "empty-repos-no-connection", 1, id="no-repository-permissions"),
    pytest.param(system_check._github_unavailable_app_permissions_condition, (),
                 "route-failure-no-fallback", 1, id="unavailable-app-permissions"),
    pytest.param(system_check._github_unverified_app_permissions_condition, (),
                 "permissions-error", 1, id="unverified-app-permissions"),
    pytest.param(system_check._github_missing_permissions_condition,
                 ({("Workflows", "write")}, 1), "permissions-missing-one", 1, id="missing-one-permission"),
    pytest.param(system_check._github_missing_permissions_condition,
                 ({("Contents", "write")}, 2), "multiple-app-missing-one", 1, id="missing-permission-multiple-apps"),
    pytest.param(system_check._github_verified_app_permissions_condition, (),
                 "assignments-error", 1, id="verified-app-permissions"),
    pytest.param(system_check._github_verified_repository_access_condition, ("acme/repo",),
                 "assignments-error", 2, id="verified-repository-access"),
    pytest.param(system_check._github_unverified_repository_access_condition, ("acme/repo", None),
                 "list-error", 2, id="unverified-repository-access"),
    pytest.param(system_check._github_unverified_repository_access_condition, ("acme/repo", set()),
                 "installation-malformed-rows", 2, id="inaccessible-repository"),
    pytest.param(system_check._github_unconfigured_base_branch_condition, ("acme/repo",),
                 "base-unconfigured", 3, id="unconfigured-base-branch"),
    pytest.param(system_check._github_unreachable_base_branch_condition, ("acme/repo",),
                 "list-error", 3, id="unreachable-base-branch"),
    pytest.param(system_check._github_existing_base_branch_condition, ("acme/repo",),
                 "assignments-error", 3, id="existing-base-branch"),
    pytest.param(system_check._github_missing_base_branch_condition, ("acme/repo",),
                 "base-missing", 3, id="missing-base-branch"),
    pytest.param(system_check._github_unverified_base_branch_condition, ("acme/repo",),
                 "base-error", 3, id="unverified-base-branch"),
]


@pytest.mark.parametrize(("builder", "args", "case_name", "condition_index"), _CONDITION_BUILDER_CASES)
def test_github_condition_builders_match_literal_conditions(builder, args, case_name, condition_index):
    assert builder(*args) == json.loads(_EXPECTED[case_name])[condition_index]


def test_github_workflow_helpers_accept_fakes_without_services():
    gateway = _Gateway({"repos": [{"full_name": "acme/repo"}], "permissions": _REQUIRED_PERMISSIONS})
    connection = SimpleNamespace(id="app-1", account="acme", gateway=gateway)
    ctx = SimpleNamespace(system_check_integration_cache={})
    registry = SimpleNamespace(assignments=lambda: {"acme/repo": "app-1"})
    assert system_check._github_connection_data(ctx, lambda _ctx: [connection], lambda _ctx: registry) == (
        [connection], {"acme/repo": "app-1"})
    assert system_check._github_connection_fallback("acme/repo", [connection], {}, {"app-1": connection},
                                                    {"acme/repo": "acme"}) == (gateway, "app-1")
    assert system_check._github_connection_fallback("other/repo", [connection], {}, {"app-1": connection},
                                                    {"other/repo": "other"}) == (None, "")
    route_deps.github_connection_used.set("app-1")
    assert system_check._github_repository_route(
        ctx, "acme/repo", [connection], {}, {"app-1": connection}, {"acme/repo": "acme"},
        lambda _ctx, _repo: gateway, route_deps.github_connection_used) == (gateway, "app-1")
    assert system_check._read_github_repositories(gateway) == {"acme/repo"}
    selected = system_check._github_selected_repositories(
        ctx, ["acme/repo"], "manual", [connection], {}, lambda _ctx, _repo: gateway,
        route_deps.github_connection_used)
    app_connections = system_check._github_application_connections(selected)
    checks = system_check._github_permission_checks(ctx, "manual", app_connections)
    assert system_check._read_github_installation_permissions(gateway, "1") == _REQUIRED_PERMISSIONS
    assert checks == [_REQUIRED_PERMISSIONS]
    assert system_check._github_installation_condition(["acme/repo"], [connection], selected) == \
        json.loads(_EXPECTED["assignments-error"])[0]
    assert system_check._github_app_permissions_condition(
        ["acme/repo"], selected, app_connections, checks) == json.loads(_EXPECTED["assignments-error"])[1]
    assert system_check._check_github_base_branch(gateway, "acme/repo", "main") is True
    project = SimpleNamespace(settings_for_repo=lambda _repo: {"base": "main"})
    assert system_check._github_repository_conditions(ctx, project, "acme/repo", selected, "manual") == \
        json.loads(_EXPECTED["assignments-error"])[2:]
    assert system_check._github_pending_conditions(["acme/repo"]) == json.loads(_EXPECTED["pending-one"])
