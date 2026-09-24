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
(awaiting human review), `post` (findings to triage), `author` (awaiting author
changes), and `done` (completed cycle). The `approve` and `post` labels indicate
the next human step; they do not trigger publication or approval.
A trailing `-` marks a pending forge refresh (for example, `post -`).

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
| `v` | Resume the exact saved conversation in the same terminal |
| `o` / `ctrl+o` | Choose pending human review and open the browser |
| `w` | Choose pending author changes |
| `m` | Record your completed approval locally and dispatch cleanup |
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

Accessible-PR search is paginated (`n`); `f` in that screen chooses the next configured
forge. Following a result does not start AI. Return to the queue and press `r`.
The CLI also supports repository/author/state search filters through `ls --view all`.

The preview and full details show the saved Markdown review drafts. Each issue
includes a copyable comment for human evaluation. New native reviews await human
review and show “Read review drafts” instead of an inferred finding count. Severity
and confidence thresholds apply only to legacy structured results, whose full details
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

## Return reconciliation

Entering a native session records a durable refresh-pending flag and resumes the saved
conversation without sending a prompt. The human enters questions or explicit posting
instructions inside that session. Foreground Codex uses
full access with on-request approvals; Claude uses default tools with manual approvals.
DiffButler does not execute or retry any posting operation.

On verified foreground exit, the core refreshes that PR using its configured account
and reads paginated request events, submitted reviews, inline comments, and discussion
comments. Successful polling repeats this reconciliation for followed PRs independently
of current request membership. Agent output claiming publication is never evidence.

- Closure/merge or a newly observed own approval completes the cycle and schedules cleanup.
- A new head or renewed request returns attention to pending AI review. Feedback on an
  older head cannot hide that work.
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
choice for the next live sync. Switching back immediately shows the saved Requested
view without another network request; normal polling updates it. Already followed PRs
remain monitored and can be viewed in Active/Following. Requested includes direct/team
review requests; assignees do not affect membership, including stale saved assignment
entries. When all-PR monitoring is off, the default view and return from History show
Requested. Refresh failures show the specific safe forge diagnostic
and preserve saved rows. No AI review starts as a result of this switch.

Pressing `r` prepares missing repository sources automatically. The status area shows
queueing, repository preparation, and fetching progress. Fetch failures explain how
to retry; no agent starts until the pinned base/head source context is ready.
