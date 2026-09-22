# Native CLI reviews

The managed review presets support **Codex CLI and Claude Code on Linux and macOS**
without version checks. DiffButler selects native read-only controls for background
analysis; it does not create an additional OS sandbox. Host/boot verification supports both platforms; macOS validation and
authentication limitations are described below.
Claude's installed version and help were checked locally. Automated acceptance
covers public CLI review, explicit retry, exact-session resume in a controlling PTY,
completion, cleanup, and retained findings across process restarts. It uses a
synthetic executable, disposable Git repositories, and SQLite; it makes no
paid model requests. The earlier Linux/Codex terminal experiment was manually
confirmed. The macOS managed-review fix was subsequently verified manually by the user.

## macOS terminal handoff validation

On macOS 26.6.2, the handoff probe successfully opened Codex CLI 0.155.1 and
Claude Code 2.1.267, returned to the dashboard on agent exit, and opened each
agent again. Codex was tested with `/quit` and Ctrl+D; Claude with `/exit` and
Ctrl+C twice. The user also confirmed successful handoff with both CLIs in their
own terminal. No model prompts were submitted in the automated-agent smoke check.
Reproduce in an interactive terminal:

```sh
make handoff ARGS="-- codex --no-alt-screen"
make handoff ARGS="-- claude --safe-mode"
```

Press `v` to open the agent, exit the agent to return, then press `v` again.
Press `q` in the dashboard to quit. These commands open fresh interactive sessions;
they do not validate resuming a saved managed review. Switching back currently
requires exiting the foreground agent.

The offline PTY tests also cover repeated conversation resume, selection retention,
background output isolation, resize, interruptions, crashes, and launch failures.
Their terminal-mode check runs before the session leader exits because macOS
revokes the slave terminal at that point.
The PTY harness rechecks completion when process exit races its wait condition.
Git cleanup retries macOS's transient permission error while exited processes
are being reaped; persistent permission errors still fail cleanup. These regression
checks run as part of `make check` on Linux and macOS.

Managed launches now read the macOS platform UUID and boot-session UUID before
source preparation and again before launch. Offline native acceptance runs on
macOS as well as Linux, using synthetic agents for review, retry, exact-session
resume, and cleanup. The user manually verified the fix for review launch and
session resume; this confirmation does not establish every backend/authentication combination.
Claude authentication bridging still assumes Linux's `.credentials.json` or an
explicit authentication environment variable; macOS Keychain bridging is not
implemented.

If a review fails before creating a conversation, `v` explains that another review
is needed. Failed attempts return to pending AI review; only pressing `r` (or the
pending task's next action) retries. They do not imply readiness to approve.

## Configure and review

Live dashboard startup asks for a default Codex or Claude reviewer when none is
configured, including existing forge-only configurations. It checks Git, resolves the
selected executable, and saves without launching the reviewer or checking its version.
No AI review or model authentication request is made during setup. Sign in
with the chosen agent CLI before reviewing. Complete configurations skip these prompts;
`--offline` skips setup entirely.

You can also configure a native reviewer manually. A local repository containing the
base/head Git objects is optional:

```toml
[review]
default_reviewer = "codex"

[[reviewers]]
id = "codex"
preset = "codex"
command = ["codex"]
timeout_s = 900

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
`findings` prints retained Markdown review drafts locally without invoking a reviewer.
Each issue has a location and a self-contained comment for the human to evaluate,
post, or copy and paste. Older structured reviews retain their JSON representation.
`session` resumes the exact backend ID in the current terminal, retaining it on exit.
Claude launches also pass `--name diffbutler-pr-<number>` on initial review,
explicit retry, and foreground resume, including for existing DiffButler sessions.
This gives the conversation a recognizable name in Claude's own session picker and
terminal title. Resume still uses the saved session ID, so matching PR numbers in
different repositories do not select the wrong conversation. Codex's documented
launch options do not include session naming; its interactive `/rename` remains
available for manual naming.

Names stay in the session's managed native home. Opening the agent outside
DiffButler requires that same `CLAUDE_CONFIG_DIR` or `CODEX_HOME` to see its sessions;
DiffButler does not copy them into the user's default agent home.

`approved` records the user's completed approval locally; it does not submit a
forge approval. Completion dispatches cleanup automatically, including when observed
by `sync`. `cleanup` retries due cleanup jobs after their persisted backoff.
Both `approved` and `cleanup` report counts of succeeded, pending, running, and
failed cleanup jobs across all local history. Failed jobs produce exit status 1;
completion remains recorded even if deletion fails. Pending/running counts do not
claim deletion succeeded. Retries respect the stored backoff.
The [dashboard](dashboard.md) adds periodic polling, native return reconciliation,
state overrides and offline history.

`general.workspace_root` selects the private persistent session root (default:
`$XDG_DATA_HOME/diffbutler/reviews`, normally `~/.local/share/diffbutler/reviews`).
Each session has a registered owned directory and dedicated `CODEX_HOME` or
`CLAUDE_CONFIG_DIR`. Keep this root on persistent storage: it contains conversations,
source-location records, and the identity needed to reconstruct temporary snapshots.
Never point managed roots at existing unrelated directories; initialization refuses
to adopt them. Session, repository, and snapshot roots must be separate, non-nested paths.

`general.repository_root` selects shared bare Git storage (default:
`$XDG_DATA_HOME/diffbutler/repos`). One repository is retained per forge hostname and
repository identity, shared across PRs, runs, and application restarts. On an explicit
review request, DiffButler reuses cached commits, imports the pinned pair from the
configured local checkout when available, or fetches missing objects using the preferred
Git transport with fallback. Imports and fetches are shallow, without tags or submodules.
Local imports create independent objects: there is no dependency on the checkout staying
present, and its branches, index, files, and Git metadata are never changed. Simply
launching from a different directory does not change the configured source path.
GitHub pull refs support fork PRs. The working HTTPS/SSH transport is remembered in
`git_transport`; new repositories try HTTPS then SSH. Private repositories require
working HTTPS credentials or noninteractive SSH authentication and trusted host keys.

Every run pins its exact base and head with session-owned refs. Preparation and cache
maintenance are serialized across processes. Completion removes that session's pins
only after foreground/background ownership is released and its workspaces are removed.
Other sessions' pins remain intact. Unused repositories are evicted by cleanup after
`general.repository_retention_days` (default 30) days since their last use/release;
zero evicts as soon as no session needs them. Active pins never expire by age. This is
whole-repository retention, not a size limit: repositories with active sessions can grow.
Cleanup runs during the normal lifecycle and via `diffbutler cleanup`; it is not a
separate scheduled service.

Per-run `base/` and `head/` snapshots contain complete pinned, redacted Git blobs,
including unchanged source, with no checkout hooks, Git administration, or project
execution. They ignore working/index changes. Symlinks become regular files containing
link text. Missing objects, submodules, and unsupported/binary diffs fail with a diagnostic.
Each snapshot still stores its own files; Git object sharing does not deduplicate these
copies. Worktrees and large-diff splitting remain unimplemented.

Snapshots default to the persistent session directory. Set `general.snapshot_root`
to a new private directory (for example `/tmp/diffbutler-snapshots-yourname`) to keep
only reconstructible source workspaces elsewhere. The persistent session root records
that location and its ownership identity; do not change it for that session root.
On explicit resume, a missing snapshot/root is rebuilt at the original path from
persistently pinned commits, without fetching or creating an AI review. Conversation
files stay persistent. If cached objects are missing too, resume fails rather than
silently using a newer revision. Existing unrelated or replaced roots are rejected.
Temporary storage may be cleared at any time and may be memory-backed. Uncommitted edits
or extra files created there during foreground sessions cannot be reconstructed.
Existing sessions from before shared storage retain their original workspace behavior.

## Native permissions and authentication

The shipped Codex TOML preset selects `read-only` and `never` approval for background
execution, and `danger-full-access` with `on-request` approval for foreground resume. Each
launch passes these settings again. DiffButler assumes the installed CLI supports
the preset arguments; unknown parameters or other CLI errors fail through normal
review or session error handling. Foreground full access permits forge
commands to post on the human's explicit instruction; background runs remain read-only.
Command and session behavior is covered by synthetic tests. The presets were
originally based on Codex CLI 0.155.1 and Claude Code 2.1.278; these are reference
versions, not requirements.
[Codex documents JSON events, exact-ID resume, and read-only execution](https://learn.chatgpt.com/docs/non-interactive-mode).

Claude background launches use `--restricted` with only `Read,Glob,Grep`,
`dontAsk` permissions and `--permission-prompts none`. Safe mode disables
customizations, and strict empty MCP configuration disables external tools.
The same controls apply on explicit retries. Foreground resume selects `manual`
permissions and the default tool set so human-authorized forge commands can prompt;
foreground does not pass safe mode or the empty MCP override. Background flags were checked
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
opening a session grants no publication approval. Foreground resume sends no prompt;
the human enters further instructions manually. On return, the
core observes actual forge activity and retains a refresh-pending flag when reads fail. Credential redaction is heuristic,
and a resumed conversation can contain material from earlier foreground discussion.

## Ownership, output, and cleanup

Background stdout/stderr are captured pipes, never terminal output. The actual
`thread.started` (Codex) or `system/init` (Claude) identity is checkpointed while the worker holds its claim. Only
completed Codex agent-message items or a successful Claude terminal `result` count
as answers; tool output cannot replace them.
A successful terminal event and a backend identity are required. The final answer
must be nonempty UTF-8 text; it is retained as Markdown review drafts. The native
event stream is still JSON, but the agent is not asked to emit a JSON review.
Completed reviews await human review; no finding counts, severity/confidence
filters, or automatic anchors are inferred from prose. Legacy structured results
remain readable. Raw capture is retained until cleanup; review drafts survive deletion.

The runner terminates/reaps the owned process group before finalizing or releasing
ownership, including timeout and cancellation. It trusts the native CLI
not to deliberately detach descendants; it is not a hostile-process supervisor.
Launch intent is durable before spawn. Cleanup also records host/boot intent before
filesystem deletion, so interrupted cleanup can be retried after a verified reboot.
Reboot recovery uses the same local host's boot identity; unknown and same-boot orphan ownership stays held for verification,
never inferred from age or pipe EOF. The database must be local and nonshared, on a
host with a unique machine identity: Linux machine-id or macOS platform UUID.
macOS reboot recovery uses `kern.bootsessionuuid`, not the boot volume UUID or a timestamp.

Foreground leases exclude review and cleanup. The terminal layer restores ownership
and modes after exit, failure, or cancellation. Completion during attachment keeps
its cleanup job pending until foreground exit. Cleanup removes the entire owned
native home and snapshots, including local conversation indexes, while preserving
unrelated native sessions, the original login, repository files, and SQLite analysis.
It makes no claim to delete provider-side records. Partial cleanup failures preserve
manifests for idempotent retry and do not restore a completed task to the queue.
