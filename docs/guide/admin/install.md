# Administrator guide: install

**Assumes:** you are a Super Administrator with sudo on a new Ubuntu 22.04 or 24.04 machine, a published release
`vX.Y.Z`, a host name that points at the machine, and an outbound internet connection. Nothing of AI Flow is
installed yet.

Details for developers are in `docs/v4/control-install.md`.

## Install the control plane

On the new machine run, with your release, host name and first administrator email:

```sh
curl -fsSL https://raw.githubusercontent.com/jooservices/ai-flow/vX.Y.Z/deploy/v4/bin/aiflow4-control-install \
  | sudo bash -s -- --version vX.Y.Z --domain aiflow.example.com --admin-email owner@example.com
```

The installer checks the checksum and the signature of the release before it installs anything, then prepares
the operating system, starts the control plane (API, loops, PostgreSQL, Redis, Traefik, OpenObserve, Gatus,
Mailpit) and checks every component. It prints `==> [n/11] step` as it goes. Expect up to 10 minutes.

The first Super Administrator's password is written once to a file on the machine (mode 0600) and is never
printed. Read it as root, sign in at `https://<domain>/login` and change it.

Running the same command again is safe. Exit code `0` means healthy; the other codes are listed in
`docs/v4/control-install.md`.

## Add machines

Open Command Center, press **Add node**, and run the one-use command on the machine. The token is valid for five
minutes. The node installs the tools its projects need and starts one worker per assigned account.

## Check

- Command Center answers at `https://<domain>/cc`.
- Gatus (port 8081) shows every service up.
- No critical incident is open. If one is, use the [runbooks](../runbooks/index.md).

TLS is terminated by your public proxy; Traefik serves plain HTTP on port 80, so restrict access to the origin to that proxy.
