# Dashboard, reconciliation, and history

Run `diffbutler dashboard` (or `diffbutler` in an interactive terminal). The dashboard
uses the same async core as the CLI. `--config FILE` selects configuration;
`--offline` loads local records without starting periodic forge sync. Explicit remote
search, refresh, and native session actions still do their requested I/O.

The dashboard defaults to the `ansi-dark` theme. Use the command palette's Theme
command to select a different theme for the current session. The palette's Keys
command opens keyboard help; press Escape to close that panel.

The managed native presets support **Codex CLI or Claude Code on Linux and macOS**
without version checks.
[Native review setup and limitations](native-reviews.md) apply unchanged. Every AI
run requires an explicit review action. Discovery, periodic sync, new commits,
renewed requests, and returning from a session never request another run.

## Working through the queue

PRs authored by the reviewing account are hidden from all local queue views by
default, even with direct or team review requests, following, or all-PR monitoring.
Author matching ignores letter case and uses each row's forge account.
To review PRs created by an agent using your account, set this in your configuration:

```toml
[ui]
include_own_prs = true
```

This also applies to CLI local queue listings. The setting only changes visibility;
saved review state and monitoring are retained. History and accessible-PR search
continue to include your PRs. Following a search result does not override this setting.

PRs assigned to the reviewing account are also hidden by default, even with direct
or team review requests. Set `include_assigned_prs = true` in `[ui]` to include them.
This independent filter uses the same case-insensitive, per-account matching and
applies to all dashboard and CLI local queue views. Both settings must be enabled
for PRs you authored and are assigned to. Existing saved PRs gain assignee data on
the next successful forge refresh; history and accessible-PR search are unaffected.

The default columns are PR, Title, Author, and State.
The selected PR's repository, AI status, findings count, and age appear in the preview
and full details. Age is the time since the PR was created, shown in hours. Headers use
Title Case; the `number` column is labeled PR. To apply this layout to an existing
configuration with explicit `ui.columns`, set:

```toml
[ui]
columns = ["number", "title", "author", "state"]
```

State labels are `pending` (awaiting AI review), `queued`, `running`, `approve`
(awaiting human review), `check` (review needs inspection), `author` (awaiting author
changes), `done` (completed cycle), and `done+` (new commits after your approval).
The `approve` and `check` labels indicate the next human step; they do not trigger
publication or approval.
A trailing `-` marks a pending forge refresh (for example, `check -`).

When an approved, still-open PR receives new commits, it reappears as `done+`.
These rows sort after all other visible PRs, regardless of sort column or direction,
and stay `done+` across further pushes and refreshes. The completed approval cycle
remains in history. No AI run starts automatically; press `r` to review the new
changes or `o` to continue in the browser. A renewed review request returns any
open PR to `pending`, including when new commits arrive in the same refresh.
A new approval, closure, or merge completes it.

At widths of 80 terminal columns or more, the PR table fits the available width
and hides its scrollbars; keyboard navigation still reaches every row. Smaller
windows can show scrollbars. Author names show at most 15 characters plus an
ellipsis, and titles use the remaining width with an ellipsis when needed.
The details preview occupies four rows, leaving the remaining space for the PR table.
Long lines wrap without a horizontal scrollbar; scroll vertically to read more details.

| Key | Action |
|---|---|
| `space` | Start review, discuss findings, or open the PR according to its state |
| `r` | Request a background native review of the selected active PR |
| `n` | Change the current AI reviewer globally for subsequent runs |
| `c` | Resume the exact saved conversation in the same terminal |
| `o` / `ctrl+o` | Choose pending human review and open the browser |
| `w` | Choose pending author changes |
| `m` | Record your completed approval locally and dispatch cleanup |
| `k` | Stop the selected PR's review, reset its local tracking, and dispatch cleanup |
| `enter` | Full orientation, findings, run/session identities, receipts, and cleanup |
| `D` | Inspect the last run's pinned diff from the configured local repository |
| `L` | Inspect captured run output, or its deleted/unavailable indication |
| `h` | Toggle offline completed-cycle history |
| `C` | Retry due cleanup jobs, respecting persisted backoff |
| `a` | Toggle monitoring all open PRs in configured repositories; saved across restarts |
| `b` | Search accessible open PRs; select one with Enter to follow it |
| `R` | Refresh forge evidence now |
| `/` | Filter the local queue by repository, number, title, or author |
| `f` | Cycle active, requested, following, triage, waiting/posted, and history views |
| `s` / `S` | Cycle displayed sort columns / reverse direction |
| `1`–`9`, header click | Sort by a displayed column |
| `P` | Persistently pause/resume AI dispatch; current runs finish and polling continues |
| `ctrl+c` | Cancel the selected active review; live ownership lasts until exit |
| `y` | Copy the PR URL using terminal clipboard support |
| `?` / `q` | Help / quit and settle reviews launched by this dashboard |

At startup, the top message area shows `Current AI reviewer: <name> - n to change`.
The next status message replaces it; queue refreshes do not restore it.
Press `n` to cycle configured Codex/Claude reviewers in configuration order, starting
from the global default (or the first native reviewer if no native default is set).
The selection applies to every PR, overriding repository reviewer defaults, and lasts
until the dashboard closes; configuration defaults are unchanged. It also works in
empty and history views. Cycling does not start a review or change a running review.
Press `r` (or `space` for a pending PR) to start the current reviewer. `c` still resumes
the saved conversation from the latest run, regardless of the current selection.
A new run replaces the current review details, including when switching reviewers;
earlier results remain in full details and history. Configure multiple `[[reviewers]]`
entries to choose between agents; with one entry, `n` explains that another native
reviewer must be configured to switch.

Accessible-PR search is paginated (`n`); `f` in that screen chooses the next configured
forge. Following a result does not start AI. Return to the queue and press `r`.
The CLI also supports repository/author/state search filters through `ls --view all`.

The preview and full details show the saved Markdown review drafts. Each issue
includes a copyable comment for human evaluation. Native reviews show “Read review
drafts” instead of an inferred finding count.
A successful review moves from `running` to `approve` only when its first nonblank
line is exactly `Review outcome: no_findings`, with no other outcome lines.
All other successful Markdown reviews move to `check`, including findings,
inconclusive reviews, and missing or unrecognized outcomes. If the agent exits
normally but DiffButler cannot parse its output, the task also moves to `check`.
The attempt retains its failed status and diagnostic; inspect the saved conversation
with `c` when available, or captured output with `L`. Nonzero exits and execution
failures return to `pending`. Press `o` after checking the review to choose `approve`
and open the PR, or `w` to wait for author changes. Existing saved review states are unchanged. Severity
and confidence thresholds filter only legacy structured findings; any reported finding
keeps a newly completed structured review in `check`. Full details
retain all findings. Analyzed and current SHAs are shown separately. Table cell updates preserve
selection; changed ordering preserves selection by task identity where possible.
Forge and model text is displayed literally, with terminal control characters removed.

`o` records `opened_at` and pending human review before browser dispatch, then advances
the cursor. A browser failure retains that choice and shows a copyable URL. SSH or a
missing graphical browser environment uses the same URL/clipboard fallback. Local macOS
sessions use the default browser without requiring Linux display variables. The optional
`ui.browser_command` is an **argv list**; the URL is appended as one argument, with no
shell interpolation. History browser actions only open URLs and never change task state.

`m` is a local completion record, not a forge approval submission. Approval on GitHub
remains the human's browser action. Completed rows disappear immediately; cleanup
may remain pending while a foreground or background owner is still alive.

Use `k` to recover a PR from a failed or stuck review. It cancels queued work, stops
the selected PR's review launched by this dashboard, and removes its request membership
and followed-PR tracking. Owned workspaces, snapshots, captured output, and repository
pins are cleaned up after the review exits. Other PRs and user-owned repositories and
agent history are preserved. The old cycle remains in History with reason `local_reset`;
this does not record an approval or write to the forge.

The PR disappears from the queue. The next forge refresh brings it back as a fresh
cycle if it is still awaiting your review (or included in all-PR monitoring). A PR
that was only followed stays absent until you follow it again. No AI review starts
automatically. `k` does nothing in History. Cleanup failures remain available for
retry with `C`; ownership left by an earlier crashed instance still defers deletion
until process exit can be established, as described in the
[session lifecycle guide](session-lifecycle.md).

## Return reconciliation

Entering a native session records a durable refresh-pending flag and resumes the saved
conversation without sending a prompt. The human enters questions or explicit posting
instructions inside that session. Foreground Codex uses
its configured sandbox with on-request approvals; Claude uses default tools with auto permissions.
DiffButler does not execute or retry any posting operation.

On verified foreground exit, the core refreshes that PR using its configured account
and reads paginated request events, submitted reviews, inline comments, and discussion
comments. Successful polling repeats this reconciliation for followed PRs independently
of current request membership. Agent output claiming publication is never evidence.
Successful agent exits show no status message; nonzero exits remain visible.

- Closure/merge or a newly observed own approval completes the cycle and schedules cleanup.
- A renewed review request returns any open PR to `pending`, taking precedence over
  new commits. Otherwise, new commits after approval start a visible `done+` cycle
  at the end of the queue; other new heads return attention to pending AI review.
  Feedback on an older head cannot hide that work.
- A newly observed own submitted COMMENTED/CHANGES_REQUESTED review on the current head
  defaults to waiting for author changes and shows the observed inline-comment count.
- Standalone comments and other people's reviews are retained and surfaced as activity;
  they do not establish waiting or completion. No new feedback preserves the prior state.
- Incomplete activity reads preserve previous activity evidence and defer feedback
  transitions. A failed return refresh leaves a visible refresh-pending indication.

Stable event IDs deduplicate receipts across polling and cycles. Receipts retain author,
body, head association where available, and timestamps. A local `o`/`w` choice increments
the account's sync generation and saves an event/time watermark. In-flight refreshes
must retry; older feedback cannot repeatedly undo that choice. Nothing attempts to map
similar comment text onto finding agreement or rejection.

Polling runs serially, by default every 60 seconds when all configured accounts use
authentication (`token_env`, resolved from the environment or `gh` login). Anonymous
accounts retain the 600-second / 10-minute default. A configuration containing both
uses 600 seconds because each poll refreshes every account. An explicit
`general.poll_interval_s` overrides either default; remove an existing override to
use the automatic interval. Polling backs off on errors and uses the adapter's
rate-limit handling. The app remains
responsive while reads and native reviews
run. Polling never replays writes or requests AI. Pausing AI dispatch also applies to
configured CLI reviews; an already explicitly requested run may wait queued until resume.

The routine refresh-start and refresh-success status messages each appear only once
per dashboard launch, including manual refreshes. Later refreshes preserve other
status messages; refresh errors remain visible every time and sync activity is logged.

Quitting waits for polling and action workers to finish cleanup as well as cancelling
reviews launched by this dashboard. Database connections finish closing before
cancellation propagates, including when quitting just after returning from an agent.

## Offline history and CLI actions

```sh
diffbutler open 'acme/api#1841'
diffbutler waiting 'acme/api#1841'
diffbutler history --repo acme/api --number 1841
diffbutler history --reason local_approval --since 2026-09-01 --json
diffbutler history --task CYCLE_UUID
```

History reads SQLite without forge credentials or native tools. Each completed cycle
retains its PR snapshot, reviewed SHAs, review drafts (or legacy orientation/findings/anchors),
submitted feedback receipts, completion reason/time, and cleanup result. `--task` selects a specific
cycle, including earlier cycles after the PR receives another push. `--login`, `--until`,
repository, number, reason and date filters are available. Dates without a timezone use UTC.

Cleanup deletes owned native conversations, snapshots and raw output, while these retained
records remain. `D` explicitly rereads pinned objects from the user's configured repository;
it does not recreate a session or fetch missing Git objects. Raw logs are unavailable after
cleanup. No history pruning is performed. Cycles completed before migration `0009` have their retained
SHA/completion records but may use the latest known PR title/URL when no cycle snapshot exists.

Migration `0009` adds cycle snapshots, refresh status, forge receipts and UI preferences.
The existing database upgrade/backup rules apply. Sort and AI-dispatch pause survive restart.
All configured column IDs are validated and sortable; unavailable numeric metadata sorts
last. CI currently displays `unknown` because status/check hydration is not implemented.

## Evidence and remaining scope

Offline tests cover submitted feedback, partial/comment pagination failures, manual override
races, deduplication, history across cycles, headless dashboard actions, browser failure,
sorting, accessible browsing, paused/cancelled queued runs and native handoff reconciliation.
An end-to-end synthetic Codex flow starts from the dashboard, resumes the saved conversation,
observes fake forge feedback on return, completes the cycle, deletes owned resources, and
reads retained history. The production dashboard also passes repeated same-terminal resume
in a controlling PTY, with terminal modes restored. These are synthetic acceptance tests;
no live managed Codex review or GitHub write was performed.

The native discussion/posting loop is implemented. Claude/macOS native integration,
live-provider acceptance, same-boot orphan supervision, automatic fetching, and large-diff
splitting remain outside this supported scope. Optional structured finding dispositions
and dashboard posting, snooze/dismiss conveniences, finding deep links, and CI hydration
are not implemented; this guide lists the supported actions.

The title bar shows the monitoring scope. Press `a` to switch between your review
requests and all open PRs in every configured forge's `repositories` list. Each forge
must have a nonempty repository list before enabling this. The switch is saved in the
local database, alongside other dashboard preferences, and also applies to CLI sync.
Enabling all-PR monitoring refreshes immediately in live mode; offline mode saves the
choice for the next live sync. Switching back shows the saved Active view without
another network request; normal polling updates it. Already followed PRs remain
monitored, including PRs discovered while all-PR monitoring was enabled. Visibility
respects the author and assignee filters.
Requested includes direct/team
review requests; assignees do not affect membership, including stale saved assignment
entries. The default view and return from History show Active. A discovered PR is
automatically followed, so submitting comments or losing its GitHub review request
does not hide an unfinished review. Waiting-for-author reviews remain visible across
polls and restarts, and new commits return them to pending without starting AI.
Use the Requested filter explicitly to see only current review requests.
Refresh failures show the specific safe forge diagnostic
and preserve saved rows. No AI review starts as a result of this switch.

Pressing `r` prepares missing repository sources automatically. The status area shows
queueing, repository preparation, and fetching progress. Fetch failures explain how
to retry; no agent starts until the pinned base/head source context is ready.
You can move to another pending PR and press `r` or `space` while the first review
starts; preview and queue refresh reads run without blocking keyboard actions.

Failed, cancelled, and stale review attempts retain their reason in the preview and
`Enter` detail, even after refresh or restart. `L` shows available partial agent output
for interrupted runs. A pending task still requires `r` to retry; no automatic retry
is scheduled. See [review invalidation and failure handling](native-reviews.md).
