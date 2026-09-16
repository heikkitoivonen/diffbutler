# GitHub read adapter (first M1 task)

The GitHub adapter implements identity checks, direct/team review-request discovery,
PR lookup, and paginated search behind the async `ReadForge` protocol. Doctor uses it
for authentication, core quota, and team visibility checks. Persistent queue sync,
followed-PR reconciliation, and Typer browsing/follow commands are the next M1 work;
the adapter alone does not make M1 complete.

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

Auth reads the configured environment variable first, then calls
`gh auth token --hostname HOST` with captured output and a ten-second timeout. The host
is `github.com` for GitHub.com, or the configured API hostname for Enterprise. Doctor
never prompts or saves tokens. The spec's keyring/prompt fallback is deferred; use the
environment or an existing `gh` login. Tokens and raw HTTP/auth error bodies never enter
diagnostics or persistence.

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
`owner/repo#number` or a PR URL on the configured instance. These are adapter APIs;
they do not create tasks, change follow membership, or start AI runs.

GitHub's [search API](https://docs.github.com/en/rest/search/search#search-issues-and-pull-requests)
limits each query to 1,000 results and can report incomplete results. Those limitations
and inaccessible PR details produce warnings and `complete=false`. `next_cursor` indicates
more pages; `complete` means no known coverage failure, not that all pages were consumed.
Discovery consumes all available pages. Incomplete results must never authorize removals.

## Transport and remaining work

Requests are serialized. ETag/Last-Modified validators and response representations are
cached in memory for the adapter lifetime (up to 256 URLs); a 304 reuses the matching
body and pagination. Persistent `sync_cursor` storage is deferred to queue sync.
Pagination stays on the original endpoint rather than sending credentials to link targets.

Core/search quotas are tracked separately. Exhausted quotas and server cooldowns prevent
early requests. Short Retry-After waits can retry; long waits return a cooldown to the
caller. Network/server retries are bounded, with exponential delays and jitter, following
GitHub's [rate-limit guidance](https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api#handle-rate-limit-errors-appropriately).
Doctor reports core quota; search quota becomes available after a search request.

Review activity reconciliation, diffs/source retrieval, publishing, package-entry-point
registration, and GitLab remain future work. This read adapter does not establish
background-agent isolation. Claude and macOS handoff validation remain deferred; see
[terminal-handoff.md](terminal-handoff.md).
