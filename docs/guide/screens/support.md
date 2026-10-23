# Support

**Assumes:** you are signed in as an Owner or Super Administrator, the roles with `console.manage`, and are using the installed v4 release.

Support (`/support`) builds a diagnostic bundle that helps the AI Flow team assist you without remote access to your
machines.

## What you can do here

- Press **Build support bundle** to collect versions, selected non-secret settings, machine and account states, up to
  100 incidents from the last 7 days, and control-service logs from the last 24 hours. Each log file is capped at 5 MB.
- Choose one ticket first to build a ticket bundle. It also includes logs from that ticket's runs from the last 24 hours.
- Read the inventory of every file in the bundle before you download it.
- Download the ZIP and open it yourself before sending it to anyone. AI Flow never sends it for you.

Before a bundle is offered it is scanned for secrets, tokens, keys and personal data such as emails, phone numbers,
IP addresses and names. If the scan finds anything, no bundle is offered. Free-form text, such as incident details and
log messages, is replaced with a redaction marker. Bundles are temporary (about ten minutes) and only the user who
built a bundle can download it.
