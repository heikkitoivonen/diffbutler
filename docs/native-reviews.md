# Native CLI reviews

The managed review presets support **Codex CLI and Claude Code on Linux and macOS**.
DiffButler selects native read-only controls for background analysis; it does not create an additional OS sandbox. Host/boot verification supports both platforms; macOS validation and authentication limitations are described below.

Automated acceptance covers public CLI review, explicit retry, exact-session resume
in a controlling PTY, completion, cleanup, and retained findings across process restarts. It uses a synthetic executable, disposable Git repositories, and SQLite; it makes no
paid model requests.

## terminal handoff validation

Reproduce in an interactive terminal:

```sh
make handoff ARGS="-- codex --no-alt-screen"
make handoff ARGS="-- claude"
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

Managed launches read the macOS platform UUID and boot-session UUID before
source preparation and again before launch. Offline native acceptance runs on
macOS as well as Linux, using synthetic agents for review, retry, exact-session
resume, and cleanup.

Authentication uses the installed agent’s normal configuration and environment.

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

New conversations use your normal native agent home and appear in the agent's own
history. DiffButler records that home's location for exact-session retry and resume,
even if `CODEX_HOME` or `CLAUDE_CONFIG_DIR` later changes. Existing conversations
created in a private DiffButler agent home continue using that home so they remain
resumable; those older sessions do not inherit your normal home configuration.

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
Each session has a registered owned directory. Keep this root on persistent storage:
it contains the native-home location, source-location records, and the identity needed
to reconstruct temporary snapshots. New conversations live in your normal agent home.
Never point managed roots at existing unrelated directories; initialization refuses
to adopt them. Session, repository, and snapshot roots must be separate, non-nested paths.

`general.repository_root` selects shared bare Git storage (default:
`$XDG_DATA_HOME/diffbutler/repos`). One repository is retained per forge hostname and
repository identity, shared across PRs, runs, and application restarts. On an explicit
review request, DiffButler reuses cached commits, imports the pinned pair from the
configured local checkout when available, or fetches missing objects using the preferred
Git transport with fallback. Imports and fetches retain full history and branch refs,
without tags or submodules. Existing shallow caches are unshallowed before review.
The initial transfer can be larger; later fetches reuse cached objects. Each fetch
has a 15-minute timeout and remains cancellable.
If the local checkout lacks either PR commit, DiffButler first attempts an
import of the PR base commit into the managed cache, falling back to local `HEAD`
if that base is unavailable. A retained seed ref lets the
following forge fetch negotiate from that commit and reuse shared objects where
possible. This imports committed objects only, not working-tree changes, untracked
or ignored files. If seeding fails, the forge fetch is still attempted.
Preparation remains on demand; startup does not copy or refresh the whole repository.
Local imports create independent objects: there is no dependency on the checkout staying
present, and its branches, index, files, and Git metadata are never changed. Simply
launching from a different directory does not change the configured source path.
GitHub pull refs support fork PRs. The working HTTPS/SSH transport is remembered in
`git_transport`; new repositories try HTTPS then SSH. Private repositories require
working HTTPS credentials or noninteractive SSH authentication and trusted host keys.

Every run pins its exact target branch tip, head, and resolved merge base with
session-owned refs. Preparation and cache maintenance are serialized across processes. Completion removes that session's pins
only after foreground/background ownership is released and its workspaces are removed.
Other sessions' pins remain intact. Unused repositories are evicted by cleanup after
`general.repository_retention_days` (default 30) days since their last use/release;
zero evicts as soon as no session needs them. Active pins never expire by age. This is
whole-repository retention, not a size limit: repositories with active sessions can grow.
Cleanup runs during the normal lifecycle and via `diffbutler cleanup`; it is not a
separate scheduled service.

New Codex and Claude runs use a private Git checkout on `diffbutler-pr`, with a pinned
`diffbutler-base` target branch tip and `diffbutler-merge-base` comparison branch.
A source-only prompt asks the agent to first read the PR description for intent,
then the discussion for changes in scope or approach, and then older reviews and
inline threads with their replies, including resolved or outdated threads. The prompt
provides the PR URL for any available read-only access method, including forge tools
and CLI commands such as `gh api --method GET --paginate`, and asks the agent to report
inaccessible or incomplete context. Finally, the agent inspects the local
`git diff diffbutler-merge-base HEAD` and surrounding files, rechecking earlier
findings against the pinned code. PR text remains untrusted review data.
Reviews, source snapshots,
and changed-file inventories compare the merge base to the pinned PR head, excluding
changes present only on the target branch. Preparation fails if complete history or
a unique merge base cannot be established; it never substitutes an endpoint diff.
Native Git checks out original committed files, preserving modes and symlinks.
The checkout shares the managed cache through Git alternates; session pins keep
its objects alive until cleanup. It has independent Git administration, no copied
user configuration, checkout hooks, or initialized submodules. The user’s local
working tree and index are untouched. Both agents retain their native background permission controls.

Source workspaces default to the persistent session directory. Set `general.snapshot_root`
to a new private directory (for example `/tmp/diffbutler-snapshots-yourname`) to keep
only reconstructible source workspaces elsewhere. The persistent session root records
that location and its ownership identity; do not change it for that session root.
On explicit resume, a missing workspace/root is rebuilt at the original path from
persistently pinned commits, without fetching or creating an AI review. Conversation
files stay in the native agent home. If cached objects are missing too, resume fails
rather than silently using a newer revision. Existing unrelated or replaced roots are rejected.
Temporary storage may be cleared at any time and may be memory-backed. Uncommitted edits
or extra files created there during foreground sessions cannot be reconstructed.
Existing sessions from before shared storage retain their original workspace behavior.
Previously saved findings and conversations are not rewritten. Request a new review
explicitly to replace a review made with the old endpoint comparison; resume alone
does not rerun analysis. Rebuilding a legacy shallow workspace can fail until a new
review has prepared complete history.
Existing snapshot-based sessions retain their original format when resumed.

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

Both agents inherit DiffButler's environment and use their normal user home
(`CODEX_HOME` / `CLAUDE_CONFIG_DIR`, or `~/.codex` / `~/.claude`). DiffButler does
not copy or filter user configuration and does not override model, reasoning effort,
provider, or speed. Settings, authentication, plugins, hooks, and MCP configuration
are loaded by the installed CLI as usual. Shell aliases are not expanded; put extra
CLI arguments in the reviewer's `command` array when needed. Project settings are
resolved from the pinned review checkout, not your original working tree.

Claude background launches retain `Read,Glob,Grep,Bash`, `dontAsk` permissions,
and `--permission-prompts none`. Only `Read,Glob,Grep` are explicitly allowed;
Bash uses the native permission checks, including the user's configured rules.
DiffButler no longer passes `--restricted`, `--safe-mode`, or empty MCP configuration.
Foreground resume selects `manual` permissions and the default tool set so it can
accept human instructions. See the [Claude CLI reference](https://code.claude.com/docs/en/cli-reference).

Codex retains its native read-only sandbox and noninteractive approval policy for
background execution. Both agents retain the JSON output and exact-session resume
arguments required by DiffButler. Foreground posting still requires explicit human
instruction. User customizations run under the native CLI's own controls; DiffButler
does not disable them or scrub provider/proxy/authentication environment variables.
Authentication values are never deliberately put in prompts, arguments, SQLite, or logs.

This relies on the installed agent's sandbox and approval implementation. DiffButler
does not independently restrict every host-file read, every tool, or network egress.
Codex source-only review relies on instructions rather than a command-execution
allowlist. Claude additionally limits background tools to Read, Glob, Grep, and Bash under
its native read-only command checks. Foreground posting requires explicit instruction inside the agent;
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
its cleanup job pending until foreground exit. Cleanup removes owned workspaces and
legacy private native homes. It never deletes your normal agent home or conversations stored there; manage that history with the
agent itself. Repository files and SQLite analysis are preserved.
It makes no claim to delete provider-side records. Partial cleanup failures preserve
manifests for idempotent retry and do not restore a completed task to the queue.
