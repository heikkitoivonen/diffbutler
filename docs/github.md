# GitHub discovery and persistent queues (M1)

The async `ReadForge` adapter provides identity checks, direct/team discovery, PR
lookup, search, and request/review activity. Shared core services persist the queue
and reconcile followed PRs; Typer exposes these services without starting AI work.

## Configure and check an account

```toml
[[forges]]
kind = "github"
base_url = "https://api.github.com"
token_env = "GITHUB_TOKEN"
include_team_requests = true
login = "your-login"  # optional expected authenticated identity
```

```sh
uv run diffbutler doctor
uv run diffbutler doctor --offline
```

Normal doctor makes read-only HTTP requests to configured GitHub instances. With no
forge configured it remains local. `--offline` skips all forge checks and credential
resolution. Neither mode launches an AI reviewer or posts anything.

When `token_env` is set, auth reads the configured environment variable first, then calls
`gh auth token --hostname HOST` with captured output and a ten-second timeout. The host
is `github.com` for GitHub.com, or the configured API hostname for Enterprise. Doctor
never prompts or saves tokens. The spec's keyring/prompt fallback is deferred; use the
environment or an existing `gh` login. First-run setup asks for an environment variable
name and validates that variable directly; it never asks for a token value. Tokens and raw HTTP/auth error bodies never enter
diagnostics or persistence.

For public repositories, omit `token_env` to select anonymous access (`None` internally):

```toml
[[forges]]
kind = "github"
base_url = "https://api.github.com"
login = "your-login"
repositories = ["owner/repo"]
include_team_requests = false
```

Anonymous access sends no Authorization header and never resolves an environment token
or `gh` credential. The saved login identifies whose direct review requests to discover;
it is checked using the public user endpoint, not authenticated `/user`. GitHub permits
public reads through its [repository API](https://docs.github.com/en/rest/repos/repos#get-a-repository)
and [user API](https://docs.github.com/en/rest/users/users#get-a-user).
Team membership requires authentication; explicitly enabling team requests in public
mode produces an incomplete-coverage warning. Anonymous requests use GitHub's anonymous
quota, which can limit polling frequency.

For Enterprise use `https://YOUR_HOST/api/v3` and a token environment variable specific
to that host. API URLs require HTTPS with no embedded credentials or query string.
Redirects are bounded and confined to the configured API origin and base path.
PR identities include the API instance, repository, and number. Coverage uses synthetic
offline HTTP fixtures; no live GitHub/Enterprise integration run was performed.

## Discovery and search

Discovery paginates `/user/teams`, searches direct requests and each visible team's
requests separately, and deduplicates by PR identity. Results retain `user:LOGIN` and
`team:ORG/SLUG` provenance. Overlapping hits share one PR refresh per discovery pass.
Current PR details verify request membership and open state, so stale search hits do
not reintroduce removed requests or closed PRs.

GitHub documents separate [`review-requested` and `team-review-requested` qualifiers](https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews).
The [`/user/teams` endpoint](https://docs.github.com/en/rest/teams/teams#list-teams-for-the-authenticated-user)
can be limited to one organization for fine-grained personal access tokens. Missing scope
evidence, SSO restrictions, and membership failures produce incomplete-discovery warnings.
Accessible direct requests survive team discovery failure. `include_team_requests = false`
deliberately limits discovery to direct requests.

Search accepts repository, author, state (`open` by default, `closed`, `merged`, or `all`),
and literal text filters, with at most 100 results per page. Lookup accepts
`owner/repo#number` or a PR URL on the configured instance. Browsing does not create
tasks or change follow membership. Explicit follow and
discovery create pending attention; no operation here starts an AI run.

GitHub's [search API](https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests)
limits each query to 1,000 results and can report incomplete results. Those limitations
and inaccessible PR details produce warnings and `complete=false`. `next_cursor` indicates
more pages; `complete` means no known coverage failure, not that all pages were consumed.
Discovery consumes all available pages. Incomplete results must never authorize removals.

## Queue commands

```sh
uv run diffbutler sync                      # all configured accounts, once
uv run diffbutler sync --forge 2            # one-based config entry
uv run diffbutler ls --json                 # local active queue, no forge access
uv run diffbutler ls --view requested       # current request membership
uv run diffbutler ls --view following       # includes completed monitored PRs
uv run diffbutler ls --login your-login     # filter local account records
uv run diffbutler ls --view all --repo acme/api --author contributor
uv run diffbutler ls --view all --state all --text documentation --page-size 10
uv run diffbutler ls --view all --cursor 2  # use the same filters as the first page
uv run diffbutler follow 'acme/api#1841'
uv run diffbutler follow https://github.com/acme/api/pull/1841
```

All commands accept `--config FILE`; workflow commands accept `--json`. Remote
browsing and follow use the first configured forge unless `--forge N` selects
another. `ls` defaults to all locally stored accounts, without resolving tokens.
`sync` and all-PR browsing exit 1 on incomplete coverage, preserving accessible
results and printing warnings (or including them in JSON). Follow persists even
when activity reads fail and includes their warnings. Invalid arguments exit 2.

Discovery and explicit follow retain one monitor per PR/account. Request sources
are independent of following. A complete discovery clears absent membership; a
partial discovery retains old sources and merges accessible results. Every followed
open PR refreshes by identity even after its request disappears. Accounts map by
API-instance identity plus normalized login; they do not reuse the initial local
account. Per-account snapshots prevent another account's refresh from hiding a push.
Concurrent updates use version checks and bounded retries before short atomic commits.

Reconciliation reads paginated [issue request events](https://docs.github.com/en/rest/using-the-rest-api/issue-event-types#review_requested)
and [PR reviews](https://docs.github.com/en/rest/pulls/reviews#list-reviews-for-a-pull-request).
Event identity and timestamp watermarks survive restart. A renewed request at the
same SHA returns a followed PR for attention once. New commits also return it to
pending AI review, without scheduling a run. Existing requests cannot reactivate a
completed cycle. Approval by another person never completes an open PR.

Own approval completes a cycle only on its current SHA, with complete activity,
and after its latest observed request. A later changes-requested or dismissed review
prevents an older approval from completing it. Ambiguous ordering (a request and
approval in the same timestamp) conservatively leaves attention pending. Incomplete
activity, including uncertain team visibility, defers activity-based transitions;
head changes and observed closure/merge still apply. The [M3 dashboard](dashboard.md)
also reconciles submitted feedback, inline comments and discussion comments, retaining
receipts and respecting later local manual/waiting choices.

Completion retains the task and SHA in history, removes it from active listing, and
atomically inserts a pending cleanup job. Closed/merged PRs stop routine refresh.
Completed open PRs remain monitored and get a new cycle on a later push/request.
Completion dispatches owned native-session cleanup, while retaining normalized history.
The dashboard polls periodically; `sync` remains a single CLI refresh.

## Transport and remaining work

Requests are serialized within each adapter, with at least one second between request
starts. Search requests are spaced by at least 6.1 seconds for anonymous access or
2.1 seconds for authenticated access. Redirects and retry attempts use the same pacing.
When a response reports 20% or less of its quota remaining, remaining requests are
spread toward the reported reset time. These waits are asynchronous and cancellable;
refreshes can take substantially longer when the anonymous quota is nearly exhausted.
Pacing cannot restore an already-exhausted quota, and separate application processes
do not share a pacer. GitHub's anonymous quota is 60 requests per hour per IP address,
as described in its [rate-limit documentation](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).

ETag/Last-Modified validators and response representations are
cached in memory for the adapter lifetime (up to 256 URLs); a 304 reuses the matching
body and pagination. HTTP cache persistence remains deferred. After restart, reads
start unconditionally;
no persisted validator can cause a 304 without a usable response body. Activity
watermarks and account-scoped PR snapshots are persisted separately in SQLite.
Pagination stays on the original endpoint rather than sending credentials to link targets.

Core/search quotas are tracked separately. Exhausted quotas and server cooldowns prevent
early requests. Short Retry-After waits can retry; long waits return a cooldown to the
caller. Network/server retries are bounded, with exponential delays and jitter, following
GitHub's [rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#handle-rate-limit-errors-appropriately).
Doctor reports core quota; search quota becomes available after a search request.

Remote diffs/source retrieval, dashboard publishing, forge-plugin registration, and
GitLab remain future work. Pinned local Git source reads and human-instructed native
agent posting are implemented in the managed workflow. This read adapter does not establish
background-agent isolation. Claude has offline managed handoff coverage; live Claude
and macOS handoff validation remain outstanding. See [native reviews](native-reviews.md)
and [terminal handoff](terminal-handoff.md).
