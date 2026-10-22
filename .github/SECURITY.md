# Security policy and coordinated disclosure

AI Flow holds code access and AI credentials, so we take reports of security problems seriously and handle them
with coordinated disclosure.

## Reporting a vulnerability

Report privately through GitHub:
<https://github.com/jooservices/ai-flow/security/advisories/new> ("Report a vulnerability" on the Security tab).

Please do not open a public issue, pull request or discussion for a vulnerability, and do not include real
credentials or customer data in your report. Include what you found, the version (`aiflow4 version`), the steps to
reproduce it and the impact you expect.

## What you can expect

| Step | Target |
|---|---|
| We confirm we received your report | within 3 working days |
| We tell you whether we accept it as a vulnerability and how severe we rate it | within 10 working days |
| We fix confirmed critical and high issues and publish a release | within 30 days |
| We fix confirmed medium and low issues | within 90 days |

We keep you informed at each step. If a fix needs longer, we tell you why and agree a new date with you.

## Disclosure

We ask you to keep the details private until a fixed release is available, or 90 days after your report,
whichever comes first. When the fix ships we publish a GitHub security advisory that names the affected and fixed
versions, and, with your agreement, credit you. Each release carries a security report (`SECURITY-REPORT.md`) with
the checks that were run and their results.

## Scope

In scope: the AI Flow v4 control API and console, the node agent, the installer and the release artifacts and
container images. Out of scope: findings that need physical access, social engineering, denial of service by
volume, and problems in third-party services AI Flow connects to (report those to the service).

## Safe harbour

We will not take legal action against people who follow this policy: they test only against their own installation,
avoid accessing other people's data, and give us reasonable time to fix the problem before disclosure.

## Supported versions

Security fixes are made on the latest release. Operators should upgrade with the fleet upgrade flow
(`docs/v4/fleet-upgrade.md`).
