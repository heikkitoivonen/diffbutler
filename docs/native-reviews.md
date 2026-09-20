# Native CLI reviews (M2–M3)

The managed review path currently supports **Codex CLI 0.98.0 and Claude Code 2.1.278 on Linux**. DiffButler
selects the agent's native read-only controls; it does not create an additional OS
sandbox. macOS integration remains deferred.
The installed versions and help were checked locally for both agents. Automated acceptance
covers public CLI review, explicit retry, exact-session resume in a controlling PTY,
completion, cleanup, and retained findings across process restarts. It uses a
synthetic executable, disposable Git repositories, and SQLite; it makes no
paid model requests. The earlier Linux/Codex terminal experiment was manually
confirmed, but no live managed review was run during this implementation.

## Configure and review

Add a native reviewer and a repository containing the full base/head Git objects:

```toml
[review]
default_reviewer = "codex"

[[reviewers]]
id = "codex"
preset = "codex"
command = ["codex"]
timeout_s = 900
max_output_bytes = 4000000

[repos."acme/api"]
path = "~/src/api"
enabled = true
```

To enable Claude alongside Codex, add another reviewer:

```toml
[[reviewers]]
id = "claude"
preset = "claude"
command = ["claude"]
```

Select it with `diffbutler review 'acme/api#1841' --reviewer claude`, or set
`review.default_reviewer = "claude"`. Each reviewer keeps a separate conversation;
`session` resumes the most recent run's reviewer.

Keep your existing forge configuration. Sync or follow the PR first, then:

```sh
diffbutler review 'acme/api#1841'
diffbutler findings 'acme/api#1841'
diffbutler session 'acme/api#1841'
diffbutler approved 'acme/api#1841'
```

Commands also accept the exact stored PR URL and `--login` to disambiguate accounts.
`review` is the explicit AI action and waits for captured execution. A subsequent
explicit review resumes the saved conversation with a fresh prompt and pinned
sources. Discovery, sync, pushes, and session return never request another AI run.
`findings` reads retained results locally without invoking a reviewer. `session`
resumes the exact backend ID in the current terminal, retaining it on exit.
`approved` records the user's completed approval locally; it does not submit a
forge approval. Completion dispatches cleanup automatically, including when observed
by `sync`. `cleanup` retries due cleanup jobs after their persisted backoff.
Both `approved` and `cleanup` report counts of succeeded, pending, running, and
failed cleanup jobs across all local history. Failed jobs produce exit status 1;
completion remains recorded even if deletion fails. Pending/running counts do not
claim deletion succeeded. Retries respect the stored backoff.
The [M3 dashboard](dashboard.md) adds periodic polling, native return reconciliation,
state overrides and offline history.

`general.workspace_root` optionally selects the private managed root (default:
`$XDG_DATA_HOME/diffbutler/reviews`). Never point it at a repository or existing
unrelated directory: initialization refuses to adopt existing directories. Each
session gets a durably registered owned directory, a dedicated `CODEX_HOME` or `CLAUDE_CONFIG_DIR`, and
per-run `base/` and `head/` source snapshots. Snapshots contain pinned, redacted Git
blobs, including unchanged source, with no checkout hooks, Git administration, or
project execution. They ignore local working/index changes. Symlinks become regular
files containing link text. Missing objects, submodules, unsupported/binary diffs,
and byte-budget excesses fail with a diagnostic; no partial review is presented as
complete. Missing objects must be fetched by the user. Endpoint comparison and
64 MB combined snapshot budget are current limitations; worktrees/fetching and
large-diff splitting remain future enhancements.

## Native permissions and authentication

The shipped Codex TOML preset selects `read-only` and `never` approval for background
execution, and `read-only` with `on-request` approval for foreground resume. Each
launch asserts these settings again. Unsupported versions fail before a review
launch rather than guessing at changed flags. No bypass or full-access fallback is
used. [Codex documents JSON events, exact-ID resume, and read-only execution](https://learn.chatgpt.com/docs/non-interactive-mode).

Claude background launches use `--restricted` with only `Read,Glob,Grep`,
`dontAsk` permissions and `--permission-prompts none`. Safe mode disables
customizations, and strict empty MCP configuration disables external tools.
The same controls apply on explicit retries. Foreground resume selects `manual`
permissions and the default tool set so human-authorized forge commands can prompt;
safe mode and empty MCP configuration remain in effect. These flags were checked
against the installed CLI and the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).

Claude background authentication accepts `ANTHROPIC_API_KEY`,
`CLAUDE_CODE_OAUTH_TOKEN`, or the existing Linux `.credentials.json` login, linked
from the user's `CLAUDE_CONFIG_DIR` (default `~/.claude`) into the owned config
folder. User settings and custom providers are not imported. The dedicated config
folder retains conversations for resume and is removed at cleanup. No live Claude
model call or authenticated interactive session was used to validate this integration;
automated tests exercise the launch and output contracts with a fake executable.

Only PATH, HOME, locale, and optional `CODEX_API_KEY` are passed to background Codex;
forge token variables are omitted. The dedicated native home shares the existing
`auth.json` through a symlink when available, without copying user plugins, MCP
servers, rules, or configuration. Authentication values are never put in prompts,
arguments, SQLite, or logs. Foreground resume inherits the user's interactive
environment so the agent can use ordinary human-authorized forge tools. API-key-only
background authentication does not automatically establish an interactive login;
configure the native CLI login before interactive resume. Custom provider/global
Codex configuration is not imported into the managed home.

This relies on the installed agent's sandbox and approval implementation. DiffButler
does not independently restrict every host-file read, every tool, or network egress.
Codex source-only review relies on instructions rather than a command-execution
allowlist. Claude additionally limits background tools to Read, Glob, and Grep. Foreground posting requires explicit instruction inside the agent;
opening a session grants no publication approval. Foreground resume includes a discussion
prompt that permits feedback only after the human explicitly instructs it; on return, the
core observes actual forge activity and retains a refresh-pending flag when reads fail. Credential redaction is heuristic,
and a resumed conversation can contain material from earlier foreground discussion.

## Ownership, output, and cleanup

Background stdout/stderr are bounded pipes, never terminal output. The actual
`thread.started` (Codex) or `system/init` (Claude) identity is checkpointed while the worker holds its claim. Only
completed Codex agent-message items or a successful Claude terminal `result` count
as answers; tool output cannot replace them.
A successful terminal event and a backend identity are required, followed by strict
orientation/findings parsing and original-diff anchor validation. Raw capture is
retained until cleanup; normalized results survive deletion.

The runner terminates/reaps the owned process group before finalizing or releasing
ownership, including overflow, timeout, and cancellation. It trusts the native CLI
not to deliberately detach descendants; it is not a hostile-process supervisor.
Launch intent is durable before spawn. Cleanup also records host/boot intent before
filesystem deletion, so interrupted cleanup can be retried after a verified reboot.
Reboot recovery uses the same local host's boot identity; unknown and same-boot orphan ownership stays held for verification,
never inferred from age or pipe EOF. The database must be local and nonshared, on a
host with a unique Linux machine-id. macOS process recovery is not implemented.

Foreground leases exclude review and cleanup. The terminal layer restores ownership
and modes after exit, failure, or cancellation. Completion during attachment keeps
its cleanup job pending until foreground exit. Cleanup removes the entire owned
native home and snapshots, including local conversation indexes, while preserving
unrelated native sessions, the original login, repository files, and SQLite analysis.
It makes no claim to delete provider-side records. Partial cleanup failures preserve
manifests for idempotent retry and do not restore a completed task to the queue.
