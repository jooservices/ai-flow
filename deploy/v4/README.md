# AI Flow v4 deployment

The v4 control plane runs the API and loops; each node runs `aiflow4-node-agent`, which supervises one
`aiflow4-worker@<account>` unit per account on that node. Install a release with `bin/aiflow4-node-install`
(or `bin/aiflow4-deploy` from the control machine); the scripts live next to this file.

## Tools on a node

Every node needs the AI CLIs used by its accounts, on the `PATH` of the node agent and the workers. Install
them with the vendor's installer, then sign each account in with that account's own `HOME`.

| AI CLI | Binary | Installation |
|---|---|---|
| Claude Code | `claude` | `npm install -g @anthropic-ai/claude-code` (or the native installer from Anthropic) |
| Codex | `codex` | `npm install -g @openai/codex` (or the release binary from GitHub) |
| Grok | `grok` | `npm install -g @vibe-kit/grok-cli` |
| OpenCode | `opencode` | `curl -fsSL https://opencode.ai/install \| bash` (or `npm install -g opencode-ai`) |

Supporting tools reported by the node agent are `git`, `gh`, `node`, `uv` and `docker`; install the ones the
projects and release commands on the node need.

### Codex code-mode host

Codex 0.163 and later cannot run shell commands without the `codex-code-mode-host` binary. The node agent
reports it with the other tool versions, so Command Center shows `missing` when it is absent.

Download `codex-code-mode-host` from the same GitHub release as the installed `codex` version
(<https://github.com/openai/codex/releases>) and put the executable next to the `codex` binary, in the same
directory on `PATH` (for example `/usr/local/bin`). Install both from the same release: a host from a
different release may not speak the protocol of the `codex` it runs beside.
