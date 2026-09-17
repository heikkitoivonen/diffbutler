# DiffButler — Design Spec

Code review is the bottleneck now. AI writes code far faster than humans can review
it, so the queue of PRs waiting on your eyes grows faster than you can drain it.
Reviewing is the part that did not get automated, and it is drowning.

DiffButler watches for reviews assigned to you and shows what each PR is waiting
for: AI review, your review, or author changes. AI review starts only when you press
the review action. When that run finishes, it sorts the result into two piles:

- **Nothing found** → the PR is flagged as ready for you, with the AI's orientation
  notes on what changed and where the risk is. One keypress opens it in your browser
  and you do the human pass.
- **Something found** → the PR is flagged for triage. You agree, disagree, or edit
  findings with the resumed agent and tell it which ones to post.

The AI is never the reviewer of record. It is a filter that runs before you and
tells you where to look.

> **Name:** DiffButler. Python package and CLI command: `diffbutler`.

**Status:** M0 and M1 are implemented: configuration, SQLite migrations, doctor,
GitHub discovery/search/activity reads, persistent account-scoped queues, followed-PR
sync, and the Typer CLI. See [GitHub usage and limitations](docs/github.md) and
[current configuration/storage scope](docs/configuration.md). The
[terminal handoff experiment](docs/terminal-handoff.md) remains separate from the
future production dashboard. Managed AI reviews, sessions, history UI, periodic
polling, and the cleanup worker remain unimplemented; the design below is the target.

---

## 1. Goals

1. **Drain the queue.** The point of the tool is fewer PRs demanding your full
   attention and a shorter path to the ones that do.
2. **Never miss a review request or follow-up.** Include direct and team requests,
   and keep following reviewed PRs even when authors forget to request another review.
3. **Start AI reviews deliberately.** A discovered PR is pending AI review. One
   button/key press starts it; discovery, pushes, and review requests never launch AI.
4. **Route by outcome, and make the clean path the fastest thing in the app.**
   Clean AI pass → oriented and ready for your browser. Findings → triage.
5. **Separate background and interactive permissions.** Background analysis is
   read-only; the foreground agent can post on your explicit instruction (§13.1).
6. **The human decides what is posted.** No unattended posting; entering a session
   is not permission to post its findings.
7. **The TUI is the first UI, not the only one.** A web UI or a scripting CLI must be
   buildable against the same core with no changes to it.
8. **GitHub is the first forge, not the only one.** Adding GitLab must be writing one
   adapter, not editing the core.
9. **Keep the review conversation.** Name sessions by PR, resume them across runs,
   discuss findings and source questions, and automatically clean up on completion.
10. **Browse beyond the queue.** Switch to any accessible PR and start a review
    manually without needing a review request.

### 1.1 The metric is human review-minutes

Not findings-per-PR. A change that surfaces more issues but costs you more attention
per PR is a regression, and this is the tie-breaker for several decisions below:

- The AI is instructed toward **precision over recall** (§7.4). A false positive costs
  you a minute of reading and a bit of trust; a missed nit costs approximately
  nothing, because you are still reviewing the PR yourself.
- The AI produces an **orientation summary even on a clean pass** (§7.3). On the hot
  path there are no findings to show, so the summary *is* the product — it is what
  turns "read 400 lines cold" into "check these three things."
- The dashboard **orders your queue by how much attention each PR needs** (§10.3), so
  the cheap ones can be cleared in a batch.
- `diffbutler stats` reports queue depth, clean-pass count, and time-to-first-review
  (§16.1). If those numbers do not move, the tool is not working, however good the
  findings look.

## 2. Non-goals (v1)

- Being a general git client. No authoring, rebase, or merge management. Preparing
  PR sources from a local repository and cleaning up review workspaces are in scope (§7.2).
- Replacing human judgement. AI analysis never authorizes publication or approval.
- Reviewing on the human's behalf. A clean AI pass means *"your turn"*, not *"ship
  it."* Forge approval happens in the browser; marking approved in the dashboard
  records the human's completion, never an AI verdict.
- Multi-user / hosted service. The schema and core are designed so this stays
  *possible* (§9.5, §12), but v1 ships single-user local.
- Building an embedded agent chat interface. Use the native agent CLI via terminal
  handoff; tmux is an optional alternative. A dashboard comment composer is deferred.
- Webhooks / push delivery. v1 polls (§6.4).

---

## 3. Primary workflow

### 3.1 Existing manual workflow — the reference experience

The user starts from GitHub's review dashboard with a search such as
`is:pr state:open review-requested:@me`. The required queue includes both explicit
requests to the user and requests to groups/teams they belong to. A separate view
must allow browsing any accessible PR, regardless of who was asked to review it.

For each PR, the user launches Codex or Claude from the repository's home directory,
names the session after the PR number, and asks for a **source-only review**. The
agent can obtain the PR and inspect the full source tree and surrounding code.
The user reads the findings, discusses them with the agent as needed, and explicitly
asks it to post all or a chosen subset. A session can stay open or be quit and later
resumed by name; it remains useful for questions during the user's manual review.

After posting, the user waits for another review request or checks for changes.
Authors sometimes forget to request review again; sometimes someone else approves
and the PR merges. If the PR remains open and there are no findings the user agrees
with, the user opens GitHub and reviews manually. When the user approves, or the PR
merges, they ask the agent to clean up its temporary structures, quit it, and remove
its session data.

DiffButler must preserve this conversation and follow-up loop. In the managed
workflow, the background agent supplies analysis, the native interactive session
handles discussion and human-instructed posting, and DiffButler reconciles forge
activity and automatically cleans up completed reviews (§7.7, §8, §13).

### 3.2 Managed first pass

```
poll forge → PENDING_AI_REVIEW
                    │ one press: start AI review
                    ▼
                AI_RUNNING
                    ├─ clean → PENDING_HUMAN_REVIEW → one press: open PR in browser
                    └─ findings → FINDINGS_TRIAGE → discuss / agree / edit / reject
                                                      │ confirm selected comments
                                                      ▼
                                             PENDING_AUTHOR_CHANGES
                                                      │ new commits / re-request
                                                      ▼
                                              PENDING_AI_REVIEW

mark approved / observe own approval / PR no longer open
    → DONE → remove from active dashboard + automatic session/resource cleanup
    → HISTORY (SQLite records retained)
```

Rejecting all findings also leads to pending human review. Posting feedback can
lead to waiting for author changes; the user can open the browser at any time.
The AI's job is to change what the human knows before their manual pass.

The left branch is expected to be the common one, so it gets the shortest path:
from the default view, `o` is one keypress with no intermediate screen.

Two things the human always controls: **whether a review is posted at all**, and
**what its text says**. There is no automatic posting: the user tells the
interactive agent which findings to post. DiffButler observes the result on GitHub.

### 3.3 Follow-up, discussion, and completion

- A clean pass, or rejecting every finding, makes the PR ready for manual review.
- Findings can be discussed in the same named session before agreeing, editing,
  rejecting, or posting a subset. Questions about the code remain available while
  doing the manual GitHub review. Discussion alone never authorizes posting.
- After posting actionable feedback, enter `PENDING_AUTHOR_CHANGES`; the user can still
  open the PR or continue the manual pass immediately.
- Poll every followed open PR independently of the requested-review queue. New
  commits or a renewed request bring it back for attention, including when no new
  review request was sent. Show new discussion activity without treating it as a push.
- Marking the PR approved, observing the user's approval on the forge, or finding
  the PR no longer open completes the cycle. Another person's approval alone does
  not finish it while it remains open. Remove completed PRs from the active dashboard
  immediately and automatically clean up their sessions/resources, preserving SQLite
  history. New commits or renewed requests can create a new pending cycle for a
  followed open PR; they never automatically start AI review.

---

## 4. Architecture

### 4.1 Layers

```
diffbutler/
├── domain/          entities, enums, value objects, events    (pure Python, no I/O)
├── core/            application services — THE PUBLIC API      (async, UI-agnostic)
│   ├── services/      sync, review, sessions, reconcile, cleanup (publish later)
│   ├── dto/           pydantic models crossing the boundary
│   └── events.py      domain event bus
├── forges/          forge adapters
│   ├── base.py        Forge protocol + shared diff machinery
│   ├── github/
│   └── gitlab/        (M5 — stub + docs from M0)
├── reviewers/       AI reviewer adapters
│   ├── base.py        Reviewer protocol, ReviewContext, findings parser
│   ├── cli.py         subprocess backend (claude, codex, anything)
│   ├── mcp.py         MCP client backend
│   └── api.py         direct Anthropic API backend (optional extra)
├── store/           SQLAlchemy models, repositories, Alembic migrations
├── config/          pydantic-settings, TOML loading, presets
└── ui/
    ├── tui/           Textual app — the only place Textual is imported
    └── cli/           Typer commands — the second interface, from day one
```

**The load-bearing rule:** `ui/` depends on `core/`. `core/` depends on `domain/`,
`forges/`, `reviewers/`, `store/` — all through protocols. Nothing depends on `ui/`.

This is enforced mechanically, not by discipline: an
[import-linter](https://import-linter.readthedocs.io/) layered contract in
`pyproject.toml` fails CI if `core` ever imports `ui`, or if `textual` appears
outside `ui/tui`.

```toml
[[tool.importlinter.contracts]]
name = "Layered architecture"
type = "layers"
layers = ["diffbutler.ui", "diffbutler.core", "diffbutler.forges | diffbutler.reviewers | diffbutler.store", "diffbutler.domain"]

[[tool.importlinter.contracts]]
name = "Textual is confined to the TUI"
type = "forbidden"
source_modules = ["diffbutler.core", "diffbutler.domain", "diffbutler.forges", "diffbutler.reviewers", "diffbutler.store"]
forbidden_modules = ["textual", "rich"]
```

### 4.2 The core API (the seam every UI sits on)

```python
class DiffButler:
    """Everything a UI can do. Async, DTO-in/DTO-out, no ORM objects escape."""

    async def sync(self, *, force: bool = False) -> SyncReport: ...
    async def list_prs(self, f: PRFilter, sort: SortSpec) -> list[PullRequestRow]: ...
    async def get_pr(self, pr_id: PRId) -> PullRequestDetail: ...

    async def run_review(self, pr_id: PRId, reviewer: str | None = None) -> AIRun: ...
    async def cancel_review(self, run_id: RunId) -> None: ...
    async def browse_prs(self, query: PRQuery, cursor: str | None = None) -> PRPage: ...
    async def follow_pr(self, ref: PRRef) -> PullRequestDetail: ...
    async def prepare_handoff(self, pr_id: PRId) -> SessionLaunch: ...
    async def reconcile_handoff(self, handoff_id: HandoffId) -> ReconcileReport: ...
    async def mark_waiting_for_author(self, pr_id: PRId) -> None: ...
    async def continue_manual_review(self, pr_id: PRId) -> None: ...
    async def suspend_session(self, session_id: SessionId) -> None: ...
    async def cleanup_session(self, session_id: SessionId) -> CleanupReport: ...
    async def mark_approved(self, pr_id: PRId) -> PullRequestDetail: ...
    async def list_history(self, f: HistoryFilter, cursor: str | None = None) -> HistoryPage: ...
    async def get_history(self, task_id: TaskId) -> ReviewHistory: ...

    async def snooze(self, pr_id: PRId, until: datetime) -> None: ...
    async def dismiss(self, pr_id: PRId) -> None: ...

    def events(self) -> AsyncIterator[DomainEvent]: ...

    # Optional future dashboard publishing API (not required for the native-agent loop):
    async def set_disposition(
        self, finding_id: FindingId, d: Disposition, *, body: str | None = None
    ) -> Finding: ...

    async def build_draft(
        self, pr_id: PRId, finding_ids: list[FindingId] | None = None
    ) -> ReviewDraft: ...
    async def post_review(self, pr_id: PRId, draft: ReviewDraft) -> PostedReview: ...
```

`SessionLaunch` carries an argv, working directory, controlled environment reference,
and handoff id. The core prepares and records handoffs; the terminal UI owns terminal
suspension/restoration and foreground process attachment. No terminal handles or
Textual objects cross into the core. Browser opening is likewise a UI operation,
paired with `continue_manual_review` for active tasks.

Design notes that make alternate UIs cheap:

- **Everything is async.** Textual's event loop, `httpx.AsyncClient`, SQLAlchemy's
  async session, and `asyncio.create_subprocess_exec` all cooperate. A FastAPI app
  later drops onto the same objects.
- **Progress is an event stream, not a callback.** `events()` yields
  `SyncStarted`, `PRDiscovered`, `RunStarted`, `RunProgress(text)`, `RunFinished`,
  `ReviewPosted`, `SessionProgress`, `SessionSuspended`, `SessionCleaned`, `Error`. The TUI subscribes and repaints; a web UI would pipe the
  same stream to SSE; the CLI renders it as log lines. Long operations never block
  and never own the rendering.
- **DTOs are pydantic v2.** They already serialise to JSON, so `diffbutler --json` and a
  future HTTP API are free.
- **`list_prs` takes filter + sort as data** (§10.3), so sorting/filtering logic is
  shared rather than reimplemented per UI.

### 4.3 Why a second interface exists at M1

Architectural intent decays unless something depends on it. The Typer CLI
(`diffbutler sync`, `diffbutler ls`, `diffbutler review <pr>`, `diffbutler findings <pr>`)
ships alongside the TUI and is built *only* on `DiffButler`. It is the proof that
the core is UI-agnostic, it makes the tool scriptable and CI-usable, and it is the
fastest way to debug the pipeline without a terminal app in the way. Extend it with
`diffbutler ls --view active|requested|following|all`, `diffbutler follow <pr>`, `diffbutler session <pr>`
(resume/discuss), `diffbutler session <pr> --suspend`, and `diffbutler session <pr> --cleanup` as
those capabilities ship. A PR argument accepts a URL or qualified repo/number;
`diffbutler approved <pr>` records local completion, and `diffbutler history` browses SQLite
history. Completion triggers cleanup automatically; the cleanup command retries a
failed cleanup or explicitly cleans an unfinished session.

If a change to the TUI requires a change to `core/`, that is a design smell worth
a second look.

---

## 5. Domain model

### 5.1 Entities

| Entity | Notes |
|---|---|
| `Repository` | forge id + namespace/name; per-repo config overrides |
| `PullRequest` | forge-neutral: id, number, title, author, branches, `head_sha`, `base_sha`, draft, labels, url, updated_at, mergeable/CI status |
| `ReviewTask` | *our* state for (PR × you): lifecycle state, snooze-until, priority, notes |
| `ReviewSession` | durable PR/account/reviewer conversation: display name, backend session id, repository path, status, transcript reference, owned resource manifest, cleanup status |
| `AIRun` | one reviewer execution within a session: reviewer id, prompt version, config hash, `head_sha`, status, timings, token/cost if reported, raw stdout/stderr, parse status |
| `Finding` | one issue: anchor, severity, category, title, body, optional suggestion, confidence |
| `Disposition` | human verdict on a finding: `PENDING | AGREED | DISAGREED | EDITED | DEFERRED`, plus edited body and who/when |
| `PostedReview` | observed or dashboard-posted review: forge review id, body, comment count, timestamp; native posts need no local draft hash |

### 5.2 Lifecycle

```
DISCOVERED → PENDING_AI_REVIEW → QUEUED → AI_RUNNING
                    user press ───┘          ├→ PENDING_HUMAN_REVIEW (clean)
                                            ├→ FINDINGS_TRIAGE
                                            └→ PENDING_HUMAN_REVIEW (failure badge)
FINDINGS_TRIAGE → native session → observed feedback → PENDING_AUTHOR_CHANGES
              └→ PENDING_HUMAN_REVIEW (`o`: take over the human review)
new head / renewed request → PENDING_AI_REVIEW (never auto-enqueue)
mark approved / own forge approval / merge / closure → DONE (history only)
DONE → automatic cleanup: pending → running → succeeded | failed (retryable)
```

Rules:

- **A user action is required for every AI run.** Discovery and follow-up polling
  only update status. `QUEUED` means the user requested a run that is waiting for
  capacity. A new push cancels an outdated run, marks its analysis stale, and returns
  the PR to `PENDING_AI_REVIEW`; it does not start a replacement.
- **Request membership and follow-up are separate.** Reviewed/followed open PRs
  remain monitored after a request disappears. Posting comments does not mean done.
- **Posting is not the end.** Posted feedback leads to `PENDING_AUTHOR_CHANGES`;
  the inferred waiting state is overridable. `w` explicitly marks waiting for author
  changes; `o` marks pending human review and opens the PR without requiring
  per-finding dispositions. The original findings remain available for reference.
- **Completion hides the PR immediately.** `mark_approved` records that the user
  has finished and approved the PR; it is a local workflow action, not a GitHub
  approval submission. Approval itself remains in the browser. Observing the user's
  forge approval or a merged/closed PR also completes it. Another reviewer's approval
  while still open does not. Record reason, SHA, time, and whether completion was
  marked locally or observed on the forge. Opening the browser also stamps `opened_at`; it does not complete the task.
- **Cleanup runs automatically and durably.** Commit completion plus a pending cleanup
  job before removing the active row. Cleanup failure does not resurrect the PR in
  the dashboard; surface it in history and a status notification, and retry after
  restart (§7.6). No per-PR cleanup confirmation is required after completion.
- **Completed open PRs stay out of the active view.** A request that was already
  present at completion cannot rediscover the PR as new. Only a later head change
  or renewed request reactivates a followed PR as a new cycle, pending AI review.
  Preserve the completed cycle in history; closed/merged PRs stop routine polling.
- **Failures stay visible.** Failed AI analysis yields pending human review with a
  warning and an explicit retry action. Retain the conversation until completion.
- **New code needs fresh analysis.** Keep old analysis labelled by SHA. Revalidate
  carried dispositions against the code and diff; surviving anchors are insufficient.
  Renewed same-SHA requests are surfaced once, with previous analysis available;
  users may run AI again or proceed directly to the manual pass. Partial discovery
  failure never implies completion.
- Snoozing temporarily hides an active task until its deadline; completion still
  triggers cleanup while snoozed. Dismissal explicitly stops following and is
  distinct from approval or closure.

### 5.3 Attention rank

Because the goal is draining a queue rather than displaying one, every `ReviewTask`
carries a derived `attention` score used as the default sort (§10.3). It is a plain,
inspectable formula — no model, no magic — computed at read time:

```
attention = severity_weight(max_severity)      # blocking 100, important 40, minor 10, none 0
          + state_weight(state)                # FINDINGS_TRIAGE 50, AI_FAILED 30, PENDING_HUMAN_REVIEW 20
          + age_pressure(first_seen_at)        # grows with time waiting on you
          + size_penalty(additions + deletions)  # big diffs need a bigger slot of your time
          - draft_discount(is_draft)
```

Weights live in `[ui.attention]` so you can retune them. The purpose is not
precision; it is that a clean 12-line PR and a 900-line one with a blocking finding
never sit next to each other competing for the same slot of your attention.

---

## 6. Forge abstraction

### 6.1 The protocol

```python
class Forge(Protocol):
    id: str  # "github", "gitlab", "gitea"

    async def whoami(self) -> Account: ...
    async def list_review_requests(self) -> list[PullRequest]: ...
    async def search_pull_requests(self, query: PRQuery, cursor: str | None = None) -> PRPage: ...
    async def get_review_activity(self, ref: PRRef) -> ReviewActivity: ...
    async def get_pull_request(self, ref: PRRef) -> PullRequest: ...
    async def get_diff(self, ref: PRRef) -> UnifiedDiff: ...
    async def get_file(self, ref: PRRef, path: str, *, side: Side) -> bytes | None: ...
    async def list_existing_comments(self, ref: PRRef) -> list[ExistingComment]: ...
    async def submit_review(self, ref: PRRef, draft: ReviewDraft) -> PostedReview: ...

    def web_url(self, ref: PRRef, anchor: DiffAnchor | None = None) -> str: ...
    def capabilities(self) -> ForgeCapabilities: ...
```

`ForgeCapabilities` is how the core copes with forges that differ rather than
pretending they don't: `supports_multiline_comments`, `supports_suggestions`,
`supports_pending_reviews`, `supports_draft_prs`, `max_comment_length`,
`comment_batch_limit`. The publish service degrades gracefully — e.g. if
`supports_pending_reviews` is false, it posts individual notes and a summary
comment instead of one atomic review.

### 6.2 Vocabulary mapping

The domain uses GitHub-ish names because that is the first target; adapters translate.

| Domain | GitHub | GitLab |
|---|---|---|
| Pull request | Pull request | Merge request |
| Review request | `review_requested` on you/your team | Assigned as Reviewer |
| Review (batch) | Review with `event: COMMENT` | Batch of discussions (no atomic review object) |
| Inline comment | Review comment | Discussion note with `position` |
| Anchor | `path`,`line`,`side`,`start_line`,`start_side` | `position{base_sha, start_sha, head_sha, old_path, new_path, old_line, new_line}` |
| Suggestion | ```` ```suggestion ```` block | ```` ```suggestion:-0+0 ```` block |

### 6.3 `DiffAnchor` — the part that bites

Positioning an inline comment is the single least portable operation, so the value
object carries the **superset** of what any forge needs and adapters project down:

```python
@dataclass(frozen=True)
class DiffAnchor:
    path: str  # path on the RIGHT side (new file)
    old_path: str | None  # differs on renames; GitLab requires it
    line: int  # 1-based line on `side`
    side: Side  # LEFT (base) | RIGHT (head)
    start_line: int | None = None  # multi-line range
    start_side: Side | None = None
    base_sha: str = ""  # GitLab position needs all three SHAs
    start_sha: str = ""
    head_sha: str = ""
```

Both forges **reject comments on lines outside the diff hunks**, and this is where
naive implementations fail loudly and often. So `forges/base.py` owns a shared
`DiffIndex` built by parsing the unified diff once:

- `is_commentable(anchor) -> bool`
- `snap(anchor) -> DiffAnchor | None` — move to the nearest commentable line inside
  the same hunk, within a configurable window (default 3 lines)
- when snapping fails: **demote, never drop.** The finding becomes a file-scoped note
  in the review body, prefixed with `path:line`, and the UI marks it `demoted`.

The AI is a frequent producer of not-quite-right line numbers. Handling that here
once means neither the reviewers nor the forge adapters need to.

### 6.4 GitHub adapter specifics

- REST via `httpx` (`GET /pulls`, `/pulls/{n}/files`, `POST /pulls/{n}/reviews`).
  A GraphQL query is a worthwhile optimisation for the *list* step later, since
  "PRs requesting my review across all orgs" is one query instead of N.
- Discovery must reproduce the user's queue: open PRs requested directly from the
  user **or from any team they belong to**, with team inclusion enabled by default.
  `is:pr state:open review-requested:@me` is the reference user search, not an
  assumption that one API query covers every team case. Verify direct/team discovery
  semantics when implementing the adapter; union and deduplicate by forge/repo/PR,
  preserve request provenance, and surface incomplete team membership access.
- Separately refresh every followed open PR by identity: head SHA, direct/team
  requests, review activity (including the user's approval), merge and closure.
  Removing a PR from discovery results must never silently end follow-up.
- The all-PR view searches accessible PRs with repository, author, state, and text
  filters, pagination, and direct URL/repo+number lookup. Default to open PRs, allow
  closed/merged PRs too. Browsing and following never enqueue AI work; only the
  explicit start-review action does. `all` is not restricted to previously discovered review tasks.
- Auth resolution order: `GITHUB_TOKEN` → `gh auth token` shell-out → OS keyring
  (`keyring` package) → prompt once and store in keyring. Never write a token to
  the config file or the DB.
- Conditional requests: store `ETag`/`Last-Modified` per endpoint in `sync_cursor`;
  a 304 costs no rate limit. Honour `X-RateLimit-Remaining` and `Retry-After` with
  exponential backoff and jitter; surface remaining quota in the TUI status bar.
- GitHub Enterprise: `base_url` config, nothing else changes.

### 6.5 Adding a forge

This is the checklist that keeps the promise real. A new forge is one directory:

1. Implement `Forge` in `forges/<name>/adapter.py`.
2. Map its objects to the domain entities in `forges/<name>/mapping.py`; the domain
   never learns a new field for one forge's sake — put forge-specific data in
   `PullRequest.extra: dict[str, Any]`.
3. Declare honest `ForgeCapabilities`.
4. Register it. Both mechanisms work:
   - in-tree: add to `forges/registry.py`
   - out-of-tree: a `diffbutler.forges` entry point in your own package —
     ```toml
     [project.entry-points."diffbutler.forges"]
     gitea = "my_pkg.gitea:GiteaForge"
     ```
5. Add a config block: `[[forges]] kind = "gitlab"` with `base_url`, `token_env`, etc.
6. Record HTTP fixtures and run the shared conformance suite (§14).

**No core file changes.** If adding a forge requires editing `core/`, the protocol is
wrong — fix the protocol.

GitLab is the reference second forge, and the two friction points are known up
front: no atomic "pending review" object (use `capabilities` + the degraded batch
path), and the three-SHA `position` payload (already in `DiffAnchor`).

---

## 7. AI reviewer abstraction

### 7.1 The protocol

```python
class Reviewer(Protocol):
    id: str

    async def review(self, ctx: ReviewContext) -> AsyncIterator[ReviewEvent]: ...

    #   yields Progress(...) events, terminates with Result(findings, summary, usage)
```

Streaming rather than a single awaitable so the TUI can show live output from a
long-running CLI agent instead of a spinner for eight minutes.

```python
@dataclass(frozen=True)
class ReviewContext:
    """Everything the background reviewer gets. Note what is absent: no forge client,
    no token, no write path. The reviewer reads this and returns findings."""

    pr: PullRequest
    diff: UnifiedDiff
    changed_files: list[FileChange]
    read_file: Callable[[str, Side], Awaitable[bytes | None]]  # mediated, read-only
    repository_path: Path | None  # configured repository home, full source context
    checkout_path: Path | None  # managed snapshot of the reviewed SHA
    session_id: SessionId  # durable conversation, distinct from AIRun
    instructions: str  # rendered prompt template
    schema: dict  # JSON schema the reviewer must emit
    budget: Budget  # timeout, max diff bytes, max cost
```

**The background reviewer is read-only.** `ReviewContext` is the
reviewer's entire world, and it contains no credential and no way to reach the forge.
Whole-file context arrives through `read_file`, a dashboard-mediated callback that
serves repository files at this PR's base/head (including unchanged surrounding
source), and nothing else — no other repo, no other PR,
no write verb. A reviewer backend cannot post a comment, approve a PR, or push a
commit because it runs with enforced read-only capabilities (§13.1). This protocol
describes background analysis, not the foreground native CLI handoff (§7.7).

### 7.2 Backends

**`cli` — subprocess (primary).** Fully declarative, so a new agent CLI is config, not
code:

```toml
[[reviewers]]
id          = "claude"
kind        = "cli"
command     = ["claude", "-p", "{prompt_file}", "--output-format", "json"]
cwd         = "{workdir}"      # managed PR source root; temp dir in diff-only mode
stdin       = "none"           # "none" | "prompt" | "diff"
env_passthrough = ["PATH", "ANTHROPIC_API_KEY"]   # isolated HOME supplied by runner
timeout_s   = 900
parse       = "json_envelope"  # see §7.3
json_path   = "$.result"       # where the model's text lives in the CLI's envelope
```

```toml
[[reviewers]]
id      = "codex"
kind    = "cli"
command = ["codex", "exec", "--json", "-"]
stdin   = "prompt"
parse   = "jsonl_stream"       # last assistant message wins; earlier lines → Progress
```

**Repository context is the normal workflow.** Configure or discover the local
repository home (`repos.<name>.path`); launch the session with that repository as its
logical working context, as in the user's manual workflow. The managed source view
must correspond to the reviewed PR head and expose surrounding files, callers,
tests as text, and project conventions. Do not accidentally review the local branch
or unrelated uncommitted changes. The dashboard obtains missing PR refs and prepares
an isolated worktree/snapshot; the agent can request source retrieval through it.

`review.checkout = "worktree"` is the default when a repository path is configured.
`"none"` remains an explicit diff/mediated-file mode, also used when no repository
is available; surface this limitation in orientation. Preparation must not switch
branches, reset files, or alter the user's working tree. Session-owned snapshots
persist for resume until cleanup (§7.6), with every snapshot tied to its SHA.

**Source-only is the default review mode.** Read code and surrounding sources;
do not run project tests, builds, installation scripts, or application code, and
do not edit the implementation. Reading tests is useful; executing them is outside
this workflow. Fetching source and managing isolated review resources are allowed
through the dashboard. Record checks not executed in `orientation.unverified`.

> **Flags are not hardcoded in the source.** They live in a shipped
> `presets/reviewers.toml` that the user's config can override, and the exact
> invocation for each agent CLI must be verified against the installed version at
> implementation time — these tools change flags between releases. `diffbutler doctor`
> (§11.3) runs each configured reviewer against a tiny synthetic diff and reports
> whether it produced parseable findings. Treat the two blocks above as shape, not
> as verified command lines.

Substitutions available in `command`, `cwd`, and `stdin`: `{prompt_file}`,
`{diff_file}`, `{workdir}`, `{checkout}`, `{repo}`, `{pr_number}`, `{head_sha}`,
`{base_sha}`, `{schema_file}`. `{checkout}` is a config error when
`review.checkout = "none"` — caught at load time, not at run time. Presets resolve
`{workdir}` to the managed PR source root when available, or a per-run directory in
diff-only mode. A missing repository must not yield an unresolved `{checkout}`.
Transient prompt/diff files are per-run; durable conversation and owned source
workspaces are per-session and survive process exit until completion triggers
automatic cleanup, or the user explicitly cleans them earlier (§7.6).

Background process hygiene: `asyncio.create_subprocess_exec` (never `shell=True`), new process
group so cancel/timeout kills the whole tree, stdout and stderr both captured and
stored on the `AIRun` for debugging, output capped at `max_output_bytes`.

**`mcp` — Model Context Protocol client.** Uses the official `mcp` Python SDK, stdio
or streamable-HTTP transport, calls a named tool:

```toml
[[reviewers]]
id        = "house-reviewer"
kind      = "mcp"
transport = "stdio"                       # or "http"
command   = ["uvx", "our-review-server"]  # or url = "https://..."
tool      = "review_diff"
arguments = { diff = "{diff}", instructions = "{instructions}", schema = "{schema}" }
```

The adapter opens a session, `list_tools()` to validate the configured tool exists
(clear error if not), `call_tool()`, then parses findings out of the result content
blocks. MCP notifications map to `Progress` events.

**`api` — direct Anthropic API (optional extra, `pip install diffbutler[api]`).**
The lowest-latency path when you don't need an agent with file access: one
`client.messages.create` against `claude-opus-5` with a structured-output schema, so
findings come back already validated and the parsing tier in §7.3 is bypassed
entirely. Kept optional because the CLI backends cover the stated requirement and
this adds a dependency and an API key.

**Two MCP roles, don't confuse them.** The above is DiffButler as an MCP *client*.
The mirror image — DiffButler exposing an MCP *server* so an agent can drive the
dashboard (`list_pending_reviews`, `get_findings`, `set_disposition`) — is a natural
M5 addition and costs almost nothing precisely because the core is UI-agnostic: an
MCP server is just another consumer of `DiffButler` (§4.2), sitting exactly where
the TUI and the CLI sit.

### 7.3 Findings contract

The reviewer is instructed to emit exactly this:

```jsonc
{
  // The orientation block. Required, and required to be useful even when
  // `findings` is empty — on the hot path this is the entire product (§1.1).
  "orientation": {
    "intent": "What this PR is trying to do, in one sentence, in your words.",
    "shape": "How it's structured: which files carry the real change vs. churn.",
    "look_here": [                     // ordered: where a human's minutes are best spent
      {"path": "src/auth/session.py", "line": 138,
       "why": "The only change to the auth path; everything else is call-site churn."}
    ],
    "low_risk": ["Renames across 22 files", "Regenerated lockfile"],
    "unverified": ["Couldn't tell whether callers of `expire()` handle the new None"]
  },
  "findings": [
    {
      "path": "src/auth/session.py",
      "line": 142,
      "start_line": 138,          // optional, for a range
      "side": "RIGHT",
      "severity": "blocking",     // blocking | important | minor | nit | question
      "category": "correctness",  // correctness | security | performance | api | tests | style | docs
      "title": "Session token compared with ==",
      "body": "Markdown. Explain the failure, not the rule.",
      "suggestion": "if hmac.compare_digest(a, b):",   // optional, becomes a suggestion block
      "confidence": 0.85
    }
  ]
}
```

Parsing is tiered, and **never silently drops output**:

1. Strict `json.loads` of the whole payload (or the configured `json_path`).
2. Extract the last fenced ` ```json ` block and parse that.
3. Bracket-match the outermost `{...}` and parse.
4. Fail → `AIRun.parse_status = FAILED`, raw output preserved and viewable with `L`
   in the TUI, PR routed to `PENDING_HUMAN_REVIEW` with a warning badge.

Post-parse validation: pydantic model; unknown severities/categories coerced to
`minor`/`style` with a note; `confidence < min_confidence` (default `0.5`) filtered
out but retained in the DB and viewable with a toggle; anchors run through
`DiffIndex.snap` (§6.3).

**The orientation block is not optional and not a summary.** A "summary" invites the
model to restate the PR description, which is worth nothing to you. `look_here`,
`low_risk`, and `unverified` are the fields that actually convert into saved minutes:
they tell you where to spend attention, what you may skim, and where the AI knows it
was blind. `unverified` matters most on a clean pass — it is the honest edge of the
filter, and it is the difference between "nothing found" and "nothing found, but I
could not see X." A run whose `orientation.look_here` is empty on a non-trivial diff
is flagged low-quality in `diffbutler stats`.

**"No issues" must be expressible.** `findings: []` is the clean-pass signal, and the
prompt says so explicitly. The failure mode that kills trust in this kind of tool is
an AI that invents nits to look useful, so the prompt is precision-first:
*report nothing rather than something marginal; a nit-only review is a clean pass.*
`nit`/`question` severities can be excluded from the "has findings" decision via
`findings_threshold = "minor"`.

### 7.4 Prompting

`prompts/review.md.j2` (Jinja2), overridable globally and per-repo. Renders: PR title
and description, changed-file list with stats, the diff (budgeted — see below), any
repo `CONTRIBUTING.md`/`CLAUDE.md` review conventions if present, a checklist of what
to look for, the JSON schema, and the precision instruction.

Diff budgeting when the diff exceeds `max_diff_bytes`: drop lockfiles, generated
files, and vendored paths (configurable globs) → truncate the largest files, keeping
hunk headers → if still over, split into multiple runs by file group and merge
findings. Every truncation is recorded on the `AIRun`, shown in the UI, and required
to appear in `orientation.unverified` — because a review of half a diff must not look
like a review of all of it, least of all on the clean path where there are no
findings to hint that anything was skipped.

`prompt_version` is part of the run's analysis key, so editing the prompt naturally
invalidates cached runs.

### 7.5 Scheduling

- Bounded pool, `max_concurrent_reviews` (default 2), containing user-requested work
  only. Discovery, follow, resume, new commits, and renewed requests never enqueue
  an AI review. Resuming a conversation does not silently re-review the PR.
- Deduplicate repeated start presses for an active run. Store the analysis key
  `sha256(pr_id | head_sha | reviewer_id | prompt_version | reviewer_config_hash)`
  and a run attempt number, unique together. A deliberate retry/re-review can create
  a new attempt at the same SHA; completed-run caching must not block user intent.
- New push cancels obsolete in-flight analysis and sets `PENDING_AI_REVIEW`.
- Per-repo `enabled = false` prevents AI review; global pause (`P`) suspends dispatch
  of queued user requests. Completion cancels queued/background work before cleanup; foreground handoff
  holds a workspace lease until exit (§7.7).
- There is no automatic-review setting in v1. Reject `auto_review = true` and
  `rerun_on_push = true` with a clear explanation if found in older config.

### 7.6 Session continuity and cleanup

`ReviewSession` is the durable conversation; `AIRun` is one analysis of one SHA.
Use a visible name containing the PR number (for example `acme/api#1841`), with
forge/repository/account/reviewer in its identity to avoid number collisions.
Persist the backend's opaque resume id separately from this display name.

The CLI adapter supports background start, native interactive resume, suspend,
and delete-session operations, with version-verified commands in presets. Codex and Claude session
continuity is a required workflow capability; do not assume their CLI flags or
storage layouts match. Backends advertise resume/discussion/deletion support and
report unsupported operations clearly. Persist conversation turns or a backend
transcript reference, including which SHA and findings each turn concerns. Serialize
turns within a session; canceling a run or quitting a process does not delete it.
Discussion happens in the native agent CLI, which may post on explicit human
instruction. DiffButler does not parse conversational claims into posting receipts
or per-finding dispositions; it reconciles actual forge activity (§8). A new head can reuse the conversation while requiring fresh analysis.

Cleanup starts automatically after the user marks approval, the user's forge
approval is observed, or the PR is merged/closed. No further button press is needed. Stop active
processes first, then remove only manifest-recorded temporary worktrees, fetched
refs owned by the session, artifacts, and backend session data; remove stored
transcripts and raw conversation output too. Keep run metadata, orientation, findings, dispositions, and posted-review receipts
as the audit ledger (§9.6). Never remove the
repository home, unrelated branches, working changes, or other sessions. Report
partial failures and support idempotent retries; do not claim deletion when a
backend cannot perform it. Persist the cleanup job and retry state in SQLite so
shutdown or a partial failure cannot lose it. A notification links to cleanup errors
in history while the completed PR stays off the active dashboard. Resuming after
cleanup creates a fresh session and says that the old conversation was deleted. Cleanup does not stop following an open PR.

### 7.7 Same-terminal handoff (default)

`r` launches background analysis with captured output; multiple PRs can be reviewed
concurrently within the configured limit. After completion, select a row with the
arrow keys and press `v` to resume its saved session in the native agent CLI.

1. Persist a handoff record with session id, PR head SHA, task state/version, current
   user identity, and known review/comment ids or activity cursor. Refresh the baseline
   when possible; if offline, record that it is incomplete rather than inventing one.
2. Acquire exclusive ownership of that session/workspace. Do not attach a second
   process to a session still doing background analysis; show progress/cancel instead.
3. Suspend TUI rendering and input, restore ordinary terminal modes, and give the
   interactive subprocess foreground terminal ownership with inherited stdin/out/err.
   Resume the exact backend session in its review workspace with the interactive
   permission profile (§13.1). Do not capture its terminal stream as background JSON.
4. Keep DiffButler's async core running: polling and other background reviews continue,
   with output routed to files/events rather than the terminal. Buffer notifications
   until return. Only one foreground handoff may own the terminal.
5. When the agent exits, restore terminal ownership, modes, the dashboard, and selected
   row in a `finally` path, including launch failure or agent crash. Agent exit preserves
   the resumable session. Immediately refresh this PR and reconcile activity (§8).

Ctrl+C belongs to the foreground agent while attached. Quitting the agent returns to
DiffButler rather than quitting the dashboard. Completion observed during attachment
hides the PR from the active queue but defers destructive cleanup of its workspace
until the foreground process exits; then the queued cleanup proceeds automatically.
A head change is recorded but must not replace sources underneath the interactive
process. Show stale-analysis status on return; another background review requires `r`.

An early prototype must prove background-to-interactive session continuity, foreground
terminal/signals, output isolation, and return restoration for both supported CLIs.
Presets must capture the real backend session id and provide a verified resume command;
a display name alone is insufficient. Unsupported resume is reported clearly, never
silently replaced with a new conversation. No embedded chat screen is planned.

Optional tmux mode may open the same resumed session in a pane/window. It uses the same
session lock, activity baseline, exit reconciliation and cleanup rules, with foreground
exit reported back to DiffButler. It is not a dependency for the default workflow.

---

## 8. Publishing and return reconciliation

### 8.1 Native-agent posting (v1)

The user reads and discusses findings in the interactive session and explicitly asks
the agent to post all or a subset. The agent uses the user's forge identity and its
interactive authorization mechanism. Merely pressing `v` never asks it to post.
DiffButler does not implement a second chat or mandatory finding-disposition workflow.
On return it fetches evidence from GitHub; the agent saying "posted" is not evidence.

Refresh the PR, reviews, inline comments, and top-level discussion comments immediately
after handoff, including pagination and author identity. Reconcile by stable forge ids
against the persisted baseline; subsequent polling repeats failed/incomplete refreshes.
Do not treat an empty, failed, or rate-limited response as proof that nothing was posted.
Show "refresh pending" and preserve the previous state until evidence is available.

Apply these rules in order:

- A closed/merged PR or newly observed approval by the user completes the cycle and
  triggers cleanup. Previously handled approval events cannot complete a later cycle.
- A new head or renewed request after the recorded baseline returns the task to
  pending AI review; feedback on an older head must not conceal new work.
- A newly submitted feedback review by the current user defaults to pending author
  changes. This is a workflow inference, not a claim that every comment requires edits.
  Display a notification such as "Posted review detected · 3 comments" and expose `o`
  and `w` to override it. Ignore draft/pending reviews as published feedback.
- Standalone comments/replies, reviews by others, or ambiguous feedback are recorded
  and surfaced but do not by themselves prove the user is waiting for author changes.
  Preserve state and let `w` express that intent. No new feedback also preserves state:
  exiting the agent does not prove that findings were rejected.

Store observed review/comment ids, author, body, head association where available,
and timestamps in SQLite. Deduplicate repeated refreshes. Do not infer exact finding
agreement or edit mappings from similar comment text. A later local `o`/`w` action
wins over an in-flight refresh of older events: persist the action version and event
watermark so subsequent polls cannot repeatedly undo the user's choice. Reconciliation
must never repost comments or retry the agent's write operations automatically.

`o` on an active PR explicitly marks `PENDING_HUMAN_REVIEW` and opens the browser in
one action. It does not bulk-reject findings, post a review, or approve the PR. It is
valid even when findings remain or the previous state was pending author changes.
`w` explicitly marks `PENDING_AUTHOR_CHANGES`. History browser links only open the
URL; they never reactivate completed tasks. Browser-launch failures are surfaced with
a copyable URL while retaining the explicitly chosen pending-human state.

### 8.2 Optional future dashboard publishing

The following structured draft/post path is deferred beyond the initial native-agent
workflow. Its confirmation and idempotency properties apply to dashboard-originated
writes; they cannot be promised for arbitrary native-agent writes.


`build_draft(pr_id)` assembles from dispositions:

- `AGREED` → inline comment, body verbatim from the finding.
- `EDITED` → inline comment, body from the human's edit.
- `DISAGREED` → excluded from the review, retained in the DB (this is the signal
  worth mining later for prompt tuning).
- `DEFERRED` → excluded, PR stays in triage.
- Demoted anchors (§6.3) → appended to the review body as `path:line — title` items.
- Review body = optional preamble + demoted items + attribution footer (configurable,
  default: `_Drafted with an AI reviewer, reviewed and edited by @you._`).
- **The orientation block is not posted.** `look_here` / `low_risk` / `unverified` are
  written for you, the reviewer, and posting them to the author is noise on their PR
  ("here is where I chose to look" is not feedback). It stays in the dashboard. If you
  want any of it to reach the author, the edit box is right there.

Posting a selected subset is supported: `ReviewDraft` carries explicit finding ids,
head SHA, and content versions. Unselected pending/deferred findings stay in triage;
if no unresolved findings remain, posting leads to `PENDING_AUTHOR_CHANGES`. Rejecting
all findings leads to `PENDING_HUMAN_REVIEW`. Re-check the current head and draft versions
before posting; changed code or discussion edits require rebuilding and confirmation.

Then: a confirmation modal showing the exact payload → `submit_review` → record
`PostedReview` → state `POSTED`.

Safety properties:

- No unattended posting. A future dashboard post requires confirmation of the
  exact payload; this does not alter the native agent's own approval mechanism.
- Idempotent: `PostedReview` unique on `(pr_id, head_sha, draft_hash)`; a retry after
  a network failure re-checks the forge for an existing review before posting again.
- Partial-failure handling: if the forge rejects some comments (positions gone stale
  mid-flight), the successfully posted ones are recorded and the rest are returned to
  triage with the forge's error attached — never a silent half-post.
- Dry run: `diffbutler post <pr> --dry-run` prints the payload and exits.

---

## 9. Persistence

### 9.1 Is a database needed? Yes.

You flagged uncertainty here, so the reasoning, since it's the kind of decision
that's cheap now and expensive in three months:

- **Idempotency.** Without durable run state, every poll re-runs the AI on every open
  PR. That's the whole cost model of the tool.
- **Human work must survive a restart.** Dispositions and edited comment bodies are
  the user's actual labour. Losing them to a crash is unacceptable, and they exist
  nowhere else until the moment of posting.
- **Sorting and filtering across a working set** without hammering the forge, plus a
  usable dashboard while offline or rate-limited.
- **Audit trail.** What was posted, when, from which run, on which SHA.
- **Carry-over across pushes** (§5.2) requires the old findings to still exist.
- **It's the seam to the web UI.** A shared Postgres is how a second interface gets
  the same state.

Rejected alternative: JSON files. They lose on concurrent access (poller + UI +
review workers), on querying, and on schema evolution — you'd end up writing a worse
database.

**Boundary condition: the DB is a cache and a workflow ledger, never the source of
truth for PR state.** The forge is authoritative for anything about the PR itself.
On conflict, the forge wins and local rows are refreshed. Only our own workflow
columns (dispositions, run history, snoozes) are authoritative locally. A wiped DB
must be recoverable to a working request queue by re-syncing. Local-only follow
choices, conversation links, dispositions, and workflow history require a backup;
the forge cannot reconstruct them.

### 9.2 Stack

- **SQLAlchemy 2.0**, async (`AsyncSession`, `async_sessionmaker`), fully typed
  `Mapped[...]` declarative models.
- **Alembic** for migrations — still the answer in 2026, and the only one with real
  SQLAlchemy integration. `env.py` sets `render_as_batch=True` so SQLite gets
  table-rebuild migrations instead of failing on `ALTER TABLE`.
- Migrations run automatically on startup if `db.auto_migrate = true` (default for
  SQLite), with a backup copy of the DB file taken first.

### 9.3 Backends

| Backend | URL | Extra |
|---|---|---|
| SQLite (default) | `sqlite+aiosqlite:///~/.local/share/diffbutler/diffbutler.db` | — |
| PostgreSQL | `postgresql+asyncpg://...` | `diffbutler[postgres]` |
| MySQL | `mysql+aiomysql://...` | `diffbutler[mysql]` |

Portability rules, enforced by running the store test suite against all three in CI:

- No dialect-specific column types. `JSON` (not `JSONB`), `Uuid` (SQLAlchemy 2.0's
  native type), `DateTime(timezone=True)` with **UTC always, set in Python** — no
  server-side `now()`.
- No `ON CONFLICT ... DO UPDATE` hand-written per dialect; upserts go through one
  helper that branches on dialect.
- Every string column has an explicit length (MySQL requires it for indexes).
- No stored procedures, no triggers, no partial indexes.
- SQLite gets `PRAGMA journal_mode=WAL` and `busy_timeout` at connect time.

### 9.4 Schema sketch

M0 implements the account, repository, pull-request, review-task, and cleanup-job
foundation below. M1 adds `forge_account` and `pr_monitor` for account-scoped
monitoring, generation checks, and activity watermarks, plus completion SHA storage.
Remaining tables arrive with their workflows; this is the target schema.

```
account(id, forge, login, display_name)                      -- multi-user seam, §9.5
repository(id, forge, namespace, name, default_branch, config_json, enabled)
pull_request(id, repository_id, number, title, author, head_sha, base_sha,
             head_ref, base_ref, is_draft, url, state, created_at, updated_at,
             fetched_at, extra_json)
             UNIQUE(repository_id, number)
review_task(id, pull_request_id, account_id, cycle_number, state, snoozed_until, priority,
            notes, first_seen_at, opened_at, posted_at, updated_at,
            following, request_sources_json, last_request_event, last_activity_cursor,
            last_reviewed_sha, approved_sha, completion_reason, completion_source,
            completion_event_watermark, action_version, state_source,
            state_event_watermark, completed_at)
            -- attention (§5.3) is derived at read time, never stored
             UNIQUE(pull_request_id, account_id, cycle_number)
             -- cycle_number and completion event watermark prevent rediscovery
             -- serialize cycle creation per PR/account; at most one unfinished cycle
review_session(id, review_task_id, reviewer_id, display_name, backend_session_id,
               repository_path, status, transcript_ref, resource_manifest_json,
               cleanup_status, created_at, resumed_at, cleaned_at)
               -- at most one active conversation per task/reviewer
handoff(id, review_session_id, head_sha, task_version, activity_baseline_json,
        baseline_complete, started_at, returned_at, reconcile_status)
forge_activity(id, pull_request_id, forge_object_id, kind, author, head_sha,
               body, occurred_at, observed_at)
               UNIQUE(pull_request_id, kind, forge_object_id)
-- Native transcripts are referenced by review_session; no embedded chat turn store.
cleanup_job(id, review_task_id, status, attempts, next_retry_at, last_error,
            created_at, completed_at)
            UNIQUE(review_task_id)
ai_run(id, review_task_id, review_session_id, reviewer_id, prompt_version, config_hash, head_sha,
       analysis_key, attempt, status, parse_status, started_at, finished_at,
       -- UNIQUE(analysis_key, attempt); one active run per task
       exit_code, stdout_blob, stderr_blob, orientation_json, usage_json, truncation_json)
finding(id, ai_run_id, ordinal, path, old_path, line, start_line, side,
        severity, category, title, body, suggestion, confidence,
        anchor_status,                     -- ok | snapped | demoted
        disposition, edited_body, disposed_by, disposed_at, superseded_by_id)
posted_review(id, review_task_id, head_sha, draft_hash, forge_review_id,
              body, comment_count, posted_at)
              UNIQUE(review_task_id, head_sha, draft_hash) -- nullable for native posts
              -- observed forge ids deduplicated via forge_activity
sync_cursor(id, forge, endpoint, etag, last_modified, last_polled_at)
```

Blobs (`stdout_blob`) are capped and spill to files under the data dir past
`max_inline_bytes`, so the DB stays small.

### 9.5 Multi-user readiness

`account_id` exists from the first migration and defaults to the single local
account. Every workflow query filters on it. A web UI later adds auth and a real
account per user without a painful data migration — which is the entire reason to
pay the small cost now.

### 9.6 Historical PRs

A dedicated history view and `diffbutler history` query the local SQLite database, work
offline, and do not depend on current forge discovery. Retain PR/repository identity,
title and URL, reviewed SHAs, each completed review cycle, orientation, findings,
dispositions, posted-review receipts, completion reason/time, and cleanup result.
Filter by repository, PR number, completion status/reason, and date; open a historical
PR's detail or browser URL without restoring it to the active dashboard or starting AI.
Session transcripts, raw output and temporary sources are deleted by cleanup; history
must distinguish deleted artifacts from the retained review records. Store orientation
explicitly with the run so it survives removal of raw output. No automatic pruning of
history in v1. The same store API works with optional SQL backends.

---

## 10. TUI

**Textual.** It gives async-native widgets, a real `DataTable` with sortable columns,
CSS theming, a modal/screen stack, and `App.run_test()` for headless testing —
all of which we'd otherwise build.

### 10.1 Layout

```
┌ DiffButler ─────────────────────── gh:2340 left ── 4 pending ── ⏸ paused? ─┐
│ ST  REPO            #     TITLE                     AUTHOR   AI    FIND AGE │
│ ▲   acme/api       1841  Add rate limiting to …     dvora    ✓     —    2h  │  ← clean
│ ●   acme/api       1837  Refactor session store     kwame    ⚠     3    5h  │  ← triage
│ ⣾   acme/web        912  Bump deps                  bot      run   —    1d  │  ← running
│ ✗   acme/infra      44   Terraform for staging      lin      err   —    3d  │  ← AI failed
│ ✓   acme/api       1830  Fix flaky test             dvora    ✓     1    4d  │  ← posted
├──────────────────────────────────────────────────────────────────────────────┤
│ #1837 Refactor session store · kwame · +412 −88 · 14 files · CI ✓            │
│                                                                              │
│  Findings (3)                          │ 2/3  security · blocking            │
│  ▸ 1 ⛔ security   session.py:142       │ src/auth/session.py:142             │
│  ▸ 2 ⚠  correctness store.py:88        │                                     │
│  ▸ 3 · nit        store.py:203         │ Session token compared with ==      │
│                                        │                                     │
│                                        │ `==` on secrets is timing-variable… │
│                                        │                                     │
│                                        │ ```suggestion                       │
│                                        │ if hmac.compare_digest(a, b):       │
│                                        │ ```                                 │
│                                        │                                     │
│                                        │ [a]gree  [d]isagree  [e]dit  [x]skip│
└──────────────────────────────────────────────────────────────────────────────┘
 o open  enter detail  r review  p post  s sort  / filter  ? help  q quit
```

That is the triage branch. The **clean branch is the common one**, and it gets its
own preview — no findings list, because there are none; the orientation block is the
pane:

```
┌ DiffButler ──────────────────── gh:2338 left ── 6 ready · 2 triage ── ●live ─┐
│ ST  REPO            #     TITLE                     AUTHOR   AI    FIND AGE │
│ ▲   acme/api       1841  Add rate limiting to …     dvora    ✓     —    2h  │◄
│ ▲   acme/web        912  Bump deps                  bot      ✓     —    1d  │
│ ●   acme/api       1837  Refactor session store     kwame    ⚠     3    5h  │
│ ✗   acme/infra      44   Terraform for staging      lin      err   —    3d  │
├──────────────────────────────────────────────────────────────────────────────┤
│ #1841 Add rate limiting to the public API · dvora · +318 −44 · 9 files · CI ✓│
│                                                                              │
│  ✓ No issues found — your turn.                    reviewed by claude, 2m ago│
│                                                                              │
│  Intent    Per-token rate limiting on the public API, sliding window.        │
│                                                                              │
│  Look here 1. limiter.py:22   the window algorithm — the only real logic     │
│            2. middleware.py:88 failure mode when Redis is down               │
│            3. config.py:14    new default; check it against staging          │
│                                                                              │
│  Skimmable Test fixtures (5 files) · generated client · lockfile             │
│                                                                              │
│  ⚠ Unverified  Couldn't confirm callers handle the new RateLimited exception │
│                                                                              │
│                                          press [o] to open the PR in browser │
└──────────────────────────────────────────────────────────────────────────────┘
 o open  enter detail  r re-review  s sort  / filter  ? help  q quit
```

The whole design of that pane is one question: **what do you need in your head at the
moment you press `o`?** Not "what did the AI do."

Three dashboard screens: **Dashboard**, **PR detail** (structured findings), and
**Diff view** (a finding in context, `D`). `v` hands the terminal to the native agent
conversation (§7.7); it is not a fourth embedded screen.
Show the session name, analyzed/current SHA, follow-up reason, and cleanup status.
Discussion is reachable from triage and from a clean pass during manual review.

Default view is `active`: all unfinished tasks, including pending AI review,
running/queued AI, pending human review, findings triage, and pending author changes.
Show plain-language status text, not just icons. Completed tasks disappear immediately,
even while cleanup is pending. Presets include `requested`, `following`, `triage`,
`posted`, and `snoozed`; `all` separately browses any accessible PR on the forge.
`history` reads completed cycles from SQLite, including cleanup results (§9.6).

The selected row has a visible primary button and the `space` shortcut:

| Status | Primary action |
|---|---|
| Pending AI review | Start AI review immediately; no intermediate screen |
| Pending human review | Open the PR in the browser immediately |
| Pending author changes | Open the PR to inspect author updates |
| Findings triage | Hand terminal to the resumed agent conversation |
| AI queued/running | Show progress, with cancellation available |

`r` starts AI review; `v` resumes the interactive CLI. On active tasks, `o` marks
pending human review and opens the browser. `enter` retains the structured detail
view action. A separate `mark_approved` action records completion and triggers automatic
cleanup; it does not submit an approval to the forge. The history view has no implicit
start-review action. Completion and cleanup do not require opening a detail screen.

### 10.2 Configurable columns

A registry keyed by stable column id, so config names columns rather than
positions:

```python
@dataclass(frozen=True)
class ColumnDef:
    id: str  # "repo", "age", …
    header: str
    get: Callable[[PullRequestRow], Any]  # display value
    sort_key: Callable[[PullRequestRow], Any]  # sortable value (≠ display)
    order_by: str | None  # DB column for server-side sort
    width: int | None = None
    justify: Literal["left", "right", "center"] = "left"
    render: Callable[[Any], RenderableType] | None = None
```

Built-ins: `state`, `attention`, `repo`, `number`, `title`, `author`, `ai_status`,
`findings`, `max_severity`, `age`, `updated`, `additions`, `deletions`, `files`,
`ci`, `labels`, `branch`, `draft`, `reviewers`, `unverified`, `url`.

```toml
[ui]
columns = ["state", "repo", "number", "title", "author", "ai_status", "findings", "age"]
sort_by = "attention"
sort_desc = true
```

Unknown ids fail config validation with a list of valid ones (not a traceback).
Adding a column is one `ColumnDef` registration; third-party UIs read the same
registry.

### 10.3 Sorting

**Every displayed column is sortable** — that's a hard requirement of the registry:
a `ColumnDef` without a working `sort_key` fails a unit test.

- `sort_key` is separate from the display value, so `age` sorts on the timestamp
  (not the string `"2h"`), `max_severity` sorts on severity rank, and `findings`
  sorts numerically with `None` last.
- Sorting is stable and applied in memory over the loaded working set (expected
  ≲ a few thousand rows; a personal review queue is far smaller). `order_by` on the
  `ColumnDef` lets a future web UI push the same sort into SQL for large sets —
  the `SortSpec` DTO is identical either way.
- Default sort is `attention` (§5.3), which is a *derived* column: it need not be
  displayed to be sorted on, so `sort_by` accepts any registered id, not just one in
  `columns`. Sorting by `age` or `repo` remains one keypress away.
- Keys: `s` opens a sort picker over currently displayed columns, `S` reverses,
  `1`–`9` sort by the *n*th displayed column, mouse click on a header sorts.
  The indicator (`▲`/`▼`) renders in the header. Sort state persists across restarts.

### 10.4 Keybindings

Every binding maps to a **named action**, and the name is the stable contract —
config rebinds keys, and other UIs reuse the action vocabulary.

| Key | Action | |
|---|---|---|
| `o` | `open_in_browser` | mark pending human review and open PR; history opens only |
| `O` | `open_finding_in_browser` | deep-links to the file+line |
| `y` | `copy_url` | OSC 52, works over SSH |
| `enter` | `open_detail` | |
| `v` | `resume_session` | same-terminal native agent handoff |
| `D` | `view_diff` | finding in hunk context |
| `r` | `run_review` | re-run AI now |
| `R` | `refresh` | force sync |
| `w` | `mark_waiting_for_author` | mark pending author changes |
| `m` | `mark_approved` | mark completed, hide active row, automatically clean up |
| `h` | `view_history` | browse completed PRs from SQLite |
| `C` | `cleanup_session` | retry failed cleanup from history |
| `space` | `primary_action` | start AI or open browser according to status |
| `L` | `view_run_log` | raw stdout/stderr — how you debug a bad run |
| `s` `S` `1`–`9` | sorting (§10.3) | |
| `/` `f` | `filter` / `cycle_filter_preset` | |
| `z` | `snooze` | |
| `X` | `dismiss` | |
| `P` | `toggle_pause` | pause all AI runs |
| `?` `q` | `help` / `quit` | |

```toml
[keys]
open_in_browser = ["o", "ctrl+o"]
resume_session = "v"
mark_waiting_for_author = "w"
```

`o` is the most-pressed key in the app, so it gets the ergonomics: it works from the
dashboard with no intermediate screen, it advances the cursor to the next row so a
queue can be opened in a run of keypresses. On active tasks it marks pending human
review, without changing finding dispositions, and stamps `opened_at` so
the dashboard can dim what you have already picked up (dimmed, not hidden — `DONE`
requires marked approval or forge completion, §5.2).

**Opening a browser** is `webbrowser.open(url)`, with the failure modes handled
because this is the feature people notice breaking: if `$BROWSER` and `$DISPLAY` are
unset or `SSH_CONNECTION` is set, fall back to copying the URL via OSC 52 and showing
a toast with the URL — over SSH, the *local* browser is the one you want, and OSC 52
reaches it. `[ui] browser_command = "..."` overrides everything.

### 10.5 Responsiveness

No blocking work on the UI thread. Sync, AI runs, and posting are `asyncio` tasks
publishing to the event bus; the app reacts to events. Every long operation is
cancellable (`ctrl+c` cancels the focused operation, not the app). Table updates are
diffed, not full-repaints, so the cursor and selection survive a background sync.

---

## 11. Configuration

### 11.1 Location and precedence

`$XDG_CONFIG_HOME/diffbutler/config.toml` (default `~/.config/diffbutler/`), plus
per-repo overrides. Precedence: CLI flags → env (`DIFFBUTLER_*`) → repo section → user
config → defaults. Loaded and validated by **pydantic-settings**; a bad config
prints the offending key, the reason, and the valid options.

### 11.2 Example

```toml
[general]
poll_interval_s = 180
max_concurrent_reviews = 2
# AI runs start only from an explicit user action; no automatic-review settings.
# Posting occurs only on explicit instruction in the interactive agent session.

[db]
url = "sqlite+aiosqlite:///~/.local/share/diffbutler/diffbutler.db"
auto_migrate = true

[[forges]]
kind = "github"
base_url = "https://api.github.com"
token_env = "GITHUB_TOKEN"       # dashboard read access; interactive write auth is separate
include_team_requests = true

# [[forges]]
# kind = "gitlab"
# base_url = "https://gitlab.example.com/api/v4"
# token_env = "GITLAB_TOKEN"

[review]
default_reviewer = "claude"
findings_threshold = "minor"     # nit/question don't count as "has findings"
min_confidence = 0.5
max_diff_bytes = 400_000
exclude_globs = ["**/*.lock", "**/vendor/**", "**/*.min.js", "**/generated/**"]
mode = "source_only"             # inspect sources; do not execute project code
checkout = "worktree"            # full sources for configured local repositories;
                                 # fall back visibly to mediated files if unavailable

[[reviewers]]
id = "claude"
kind = "cli"
command = ["claude", "-p", "{prompt_file}", "--output-format", "json"]
timeout_s = 900
parse = "json_envelope"
json_path = "$.result"

[[reviewers]]
id = "codex"
kind = "cli"
command = ["codex", "exec", "--json", "-"]
stdin = "prompt"
parse = "jsonl_stream"

[ui]
columns = ["state", "repo", "number", "title", "author", "ai_status", "findings", "age"]
sort_by = "attention"
sort_desc = true
preview_pane = true
default_filter = "active"        # active | requested | following | all | triage | posted | snoozed | history

[ui.attention]                   # retune the queue ordering (§5.3)
blocking = 100
important = 40
age_pressure_per_day = 5
size_penalty_per_100_lines = 2

[keys]
open_in_browser = ["o", "ctrl+o"]

# Per-repo override
[repos."acme/infra"]
enabled = false                  # disable AI review for this repository

[repos."acme/api"]
path = "~/src/acme/api"          # repository home for source context
default_reviewer = "codex"
prompt = "~/.config/diffbutler/prompts/api-review.md.j2"
checkout = "worktree"            # retain session-owned sources until cleanup
```

### 11.3 `diffbutler doctor`

M0 currently validates configuration, initializes/checks SQLite, checks configured
repository/prompt paths, and resolves the browser opener. It passes without a forge
configured. The first M1 task adds GitHub auth, optional expected-login matching,
core rate-limit headers, and team visibility checks. `--offline` skips forge access;
reviewer execution/session capabilities remain deferred. The full target behavior follows.

One command that verifies the whole chain and prints a pass/fail table: config
parses; forge auth works and `whoami` matches; rate limit headroom; DB reachable and
migrations current; each configured reviewer executes against a synthetic 20-line
diff and returns parseable findings; browser opener resolves; repository paths resolve; session resume/cleanup
capabilities are reported for each backend. This is the first thing
to reach for when something doesn't work, and it makes the reviewer-flag drift
problem in §7.2 a 10-second check instead of an archaeology session.

---

## 12. Building another interface

Concretely, what a web UI would need — and what it would *not*:

**Not needed:** any change to `domain/`, `core/`, `forges/`, `reviewers/`, `store/`.

**Needed:**
1. `pip install diffbutler[postgres]`, point `db.url` at Postgres.
2. A FastAPI app wrapping `DiffButler` — routes are thin, since the DTOs already
   serialise and `PRFilter`/`SortSpec` are already request-shaped.
3. `GET /events` → SSE over `DiffButler.events()`.
4. Auth, mapping each logged-in user to an `account` row (§9.5).
5. A frontend reusing the `ColumnDef` registry (exported as JSON) so column
   configuration and sorting behave identically to the TUI.
6. Move the poller and review workers into a separate process; the core is already
   async; transactional run claims and attempt uniqueness prevent duplicate workers.

The one thing to watch: reviewer CLI backends assume local subprocess execution. A
hosted deployment wants the `mcp` or `api` backends, or a sandboxed runner — which
is exactly why the `Reviewer` protocol exists rather than a `subprocess.run` call
buried in the service layer.

---

## 13. Security & privacy

### 13.1 The privilege boundary

There are two execution profiles, with different capabilities:

| Profile | Forge access | Publication authorization |
|---|---|---|
| Background analysis (`r`) | Mediated source reads; no write credential/capability | Cannot post |
| Foreground native session (`v`) | User-authenticated access for reviewing and posting feedback | Explicit instruction from the human inside the agent |

The background `ReviewContext` contains no forge client or token. Its process runs
with an isolated environment, filesystem credential access and network policy;
omitting a token from arguments alone is insufficient. Whole-file reads remain
restricted to this PR's base/head sources. Enforce and test this profile on every
background launch, including when resuming a conversation previously used interactively.

The interactive profile deliberately permits human-instructed review/comment writes.
Supply authorized forge access through the agent's supported authentication mechanism,
not through prompts, transcripts, command-line secrets, or SQLite. Keep credentials
out of shared session artifacts that the background runner can later read. Restoring
a conversation must not restore the foreground process's permissions to a background
run. Source-only review remains the default; posting feedback is not permission to
edit the implementation, push commits, or approve/merge on the human's behalf.

This is a weaker boundary than an agent without write capability: DiffButler cannot
structurally prove each write was authorized inside an arbitrary interactive CLI.
Use its native permission controls and explicit human instructions; do not claim
that background isolation also protects the foreground agent. DiffButler records
actual forge activity on return (§8.1). Separate read and interactive write credentials
are supported; background jobs must never inherit the interactive write credential.

### 13.2 Everything else

- **Diffs leave your machine.** A CLI or MCP reviewer sends your source to a model
  provider. This is stated in the README, in `diffbutler init`, and enforced per-repo:
  `[repos."x".enabled = false]` and a global `require_opt_in = true` mode that never
  sends source to an AI reviewer for a repo not explicitly listed.
- **Secret hygiene.** Tokens live in env or the OS keyring, never in config or DB.
  A pre-send redaction pass strips high-entropy strings matching common credential
  patterns from the diff, and logs how many were redacted.
- **Subprocess safety.** `exec` not `shell`, explicit env allowlist
  (`env_passthrough`), no shell interpolation of PR titles or branch names anywhere
  — a PR title is attacker-controlled input.
- **Prompt injection is expected.** Fence and label source content as untrusted.
  Background reviewers cannot write; interactive agents have the broader capability
  described in §13.1 and rely on their permission controls and human supervision.
  A clean pass means "your turn", never "approved". Returning from an agent triggers
  forge reconciliation, not trust in instructions or receipts found in its output.
- **Repository sources are read-only to the reviewer.** The dashboard owns fetch,
  worktree preparation and cleanup; workspaces live until session cleanup, not just
  run completion. Enforce isolation rather than assuming `git worktree --detach`
  makes files read-only. A local repository or inherited `HOME` can expose git
  credentials and agent configuration: use an isolated agent home and controlled
  credential/network access for background runs while preserving only the intended
  model login and session storage. Foreground forge access is separate (§13.1). Environment token filtering alone cannot enforce §13.1.

---

## 14. Testing

- **Handoff and reconciliation.** Test background-to-native resume using captured
  backend fixtures/fake executables, terminal restoration on exit/crash/launch failure,
  exclusive session ownership, other reviews progressing silently, head changes and
  completion during handoff, and deferred cleanup until release. Test new published
  feedback vs replies/draft reviews, no-post exits, partial refresh/retry, duplicate
  events, action-version races, and `o`/`w` overrides without changing dispositions.

- **Explicit-start and completion acceptance.** Discovery, follow, pushes, re-requests,
  and return reconciliation cause zero background review invocations. Native session
  resume is explicit and does not automatically re-run the initial review. One press on pending AI starts one
  run; one press on pending human opens the PR. Marked approval, observed own approval,
  or closure/merge immediately hides the row and schedules cleanup without another
  confirmation. Verify restart recovery and cleanup failures, suppression of old
  requests, new-cycle reactivation, and offline SQLite history after artifacts are gone.

- **Forge conformance suite.** One parametrised suite every `Forge` implementation
  must pass, run against recorded fixtures (`respx`/`pytest-recording`). This is what
  makes "add GitLab in one directory" true rather than aspirational.
- **`DiffIndex` property tests.** Generated diffs × generated anchors; assert
  `snap()` never returns a non-commentable position and never crashes.
- **Reviewer parsing corpus.** A directory of real captured outputs — valid JSON,
  JSON-in-prose, truncated, empty, hallucinated line numbers, prompt-injection
  attempts — each asserting the right tier of §7.3 handles it.
- **Store suite against all three backends** in CI (SQLite, Postgres, MySQL via
  services), plus an Alembic round-trip test (`upgrade head` → `downgrade base`) and
  a check that models match the latest migration.
- **TUI tests** with `App.run_test()` driving key presses, plus
  `pytest-textual-snapshot` for layout regressions.
- **Fakes, not mocks,** for `Forge` and `Reviewer` — an in-memory forge with
  scriptable PRs makes service-level tests fast and readable.
- **Privilege-boundary tests.** A background reviewer fake that tries to open a socket to the
  forge, or to read a forge token from its environment, fails the run. §13.1 is only
  a property of the design for as long as something checks it.
- **Orientation quality tests.** The parsing corpus asserts that a clean pass still
  yields a populated `orientation`, and that a truncated diff always populates
  `unverified` (§7.4).
- **Workflow coverage.** Direct-only, team-only, and overlapping requests; any-PR
  browsing without auto-enqueue; comment review removing a request; new commits
  without re-request; same-SHA re-request; own approval; another user's approval
  while open; merge/closure; partial discovery failure. Verify follow-up and cleanup
  eligibility independently of requested-review membership.
- **Session coverage.** Resume after process/app restart, PR-number collisions across
  repositories, discussion and subset posting, new-head context refresh, source-only
  operation, cleanup retries, and preservation of unrelated files and sessions.
- No test ever hits a real forge or spends a token.

---

## 15. Tooling

Python 3.14+ · `uv` with committed `uv.lock` and the `uv_build` backend · `ruff`
(lint + format) · `pyright` in strict mode on source, tests, and scripts · `pytest` +
`pytest-asyncio` + `pytest-cov` · `import-linter` (§4.1) · `pip-audit` · one
`pyproject.toml`. `Makefile` exposes setup, checks, packaging, and dependency commands.
GitHub Actions runs checks and wheel installation tests on Linux and macOS.
The project is MIT-licensed; dependencies retain their own licenses (see
[dependency licensing notes](docs/dependencies.md)).

Runtime deps are added only as used: `textual` powers the handoff experiment;
`pydantic`, `pydantic-settings`, `sqlalchemy[asyncio]`, `alembic`, and `aiosqlite`
power M0 configuration and storage. `httpx` powers GitHub reads.
Typer powers M1's CLI. Remaining planned deps: `jinja2`, `keyring`, `mcp`.
Extras: `postgres`, `mysql`, `api`.

Ship as `uv tool install diffbutler` / `pipx install diffbutler`.

---

## 16. Milestones

Ordered so the **hot path ships first**. Triage and posting are the smaller pile;
"AI found nothing, here's where to look, press `o`" is most of the value and does not
need a single line of posting code.

| | Scope | Done when |
|---|---|---|
| **Pre-M0 experiment** | Same-terminal Textual handoff, foreground signals, background progress, native resume verification | Linux/macOS and Codex/Claude evidence supports the tmux decision; see [current results and remaining checks](docs/terminal-handoff.md) |
| **M0** | Skeleton: layers, import-linter contract, config loading, store + first migration, `diffbutler doctor` | `diffbutler doctor` passes with no forge configured |
| **M1** | GitHub read adapter + direct/team discovery + all-PR browsing + followed-PR sync + **the Typer CLI** | Direct and team requests appear once; any PR can be found; followed PRs remain tracked after request removal |
| **M2** | Reviewer protocol + `cli` backend + source-only repository context + named/resumable sessions + parsing + `DiffIndex` | Source-only review returns orientation/findings; the named session resumes after restart and is cleaned up automatically on completion |
| **M3** | TUI dashboard, status actions, same-terminal native session handoff, refresh-on-return, `o`/`w` overrides, automatic cleanup, SQLite history | Start review → discuss/post in agent → return to reconciled dashboard → manual pass → cleanup |
| **M4** | Carry-over across pushes, MCP backend, optional tmux handoff; optional structured dashboard posting if needed | Follow-up preserves context and reliably reconciles revised feedback |
| **M5** | GitLab adapter (proves §6.5), Postgres CI, optional MCP server, `api` backend, `diffbutler stats` | GitLab works with zero core changes |

M1's CLI keeps the TUI dependent on the shared core. The pre-M0 experiment validates
same-terminal handoff early; M2 must integrate and revalidate background-to-interactive
resume with managed sessions before M3 commits to the interaction.
Native-agent posting is supported with M3; a separate dashboard write path is optional
and deferred. Background AI analysis remains read-only at every milestone.

### 16.1 `diffbutler stats` — did it actually help?

The tool makes a throughput claim (§1.1), so it should be falsifiable. All of this is
already in the DB; `diffbutler stats` (and a TUI panel) just reads it:

- **Queue depth over time** — the headline. Is the pile shrinking?
- **Time-to-first-signal** — user starts AI review → AI result available. Track assignment-to-start separately
  as human queue time; deliberate waiting must not count as AI latency.
- **Clean-pass rate** — share of PRs where the AI found nothing. If this is ~100%, the
  reviewer is asleep; if it is ~0%, it is inventing work. Both are actionable.
- **Disposition mix** — agreed / edited / disagreed. The disagree rate is the
  precision measure that matters, and it is measured from your actual behaviour rather
  than asserted.
- **Orientation quality** — share of runs with an empty `look_here` on a non-trivial
  diff (§7.3).
- **Cost per PR**, where the backend reports it.

If queue depth is flat and the disagree rate is high, the honest conclusion is that
the tool is adding work, and the stats should make that obvious rather than hide it
behind a count of findings produced.

---

## 17. Open questions

1. **Team review requests — resolved requirement.** Include direct and team
   requests by default, deduplicate overlaps, and expose request provenance. Team
   membership/API behavior must be verified during adapter implementation (§6.4).
2. **Multiple reviewers per PR** — run `claude` *and* `codex` and diff their findings?
   Interesting signal (agreement ≈ confidence), but doubles cost and complicates
   dedupe. Deferred past M4; the `AIRun`-per-`ReviewTask` schema already allows it.
3. **Learning from disagreement** — `DISAGREED` findings are recorded. Feed them back
   as few-shot negatives in the prompt? Cheap to try at M5, likely valuable.
4. **Approve/request-changes** — the default interactive instruction is to post
   feedback as `COMMENT` reviews; approval remains a human browser action. Should agreeing with
   a `blocking` finding offer `REQUEST_CHANGES`? Leaning yes, behind a config flag.
5. **Cost tracking** — surface tokens/dollars per run when the backend reports them.
   Easy for `api`, backend-dependent for CLI. Worth a column if available.
6. **Comment threading** — replying to existing review discussions from the TUI is a
   natural extension but a different interaction model. Use the native agent for
   these discussions in v1; no built-in discussion composer is needed.
7. **The trust drift problem, and the one I'd worry about most.** As the clean-pass
   rate stays high, the temptation is to skim the PRs the AI cleared — which converts
   the tool from "review faster" into "review less," quietly. v1's answer is weak on
   purpose (a clean pass is worded as *"your turn"*, never *"looks good"*, and forge
   approval remains in the browser). Whether that is enough is an
   empirical question, and `diffbutler stats` should probably grow a time-on-PR distribution
   before-and-after so the drift is at least visible. Worth deciding before M3 ships,
   because it is a wording-and-defaults problem, and those calcify.
