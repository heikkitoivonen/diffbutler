# GitHub discovery and persistent queues

The async `ReadForge` adapter provides identity checks, direct/team discovery, PR
lookup, search, and request/review activity. Shared core services persist the queue
and reconcile followed PRs; Typer exposes these services without starting AI work.

## Authentication options

Use these options in order of preference, including when reviewing public repositories.

1. **Reuse your GitHub CLI login.** If `gh auth login` is already configured, run:

   ```sh
   export GITHUB_TOKEN="$(gh auth token)"
   diffbutler
   ```

   [`gh auth token`](https://cli.github.com/manual/gh_auth_token) uses the default
   host and its active account. Select either explicitly when needed:

   ```sh
   export GITHUB_TOKEN="$(gh auth token --hostname github.com --user YOUR_LOGIN)"
   export GITHUB_ENTERPRISE_TOKEN="$(gh auth token --hostname github.example.com --user YOUR_LOGIN)"
   ```

   To set up a login, use `gh auth login --hostname github.com --git-protocol https`
   or substitute your Enterprise hostname. `--git-protocol` accepts `https` or `ssh`
   and controls Git operations; token retrieval and DiffButler's API requests use
   HTTPS with either choice. See [`gh auth login`](https://cli.github.com/manual/gh_auth_login).
   DiffButler's managed source fetches initially try HTTPS, then SSH if HTTPS fails.
   The working transport is saved as the repository's `git_transport` preference
   and tried first next time; the other transport remains a fallback. You can also
   set it in [configuration](configuration.md#repository-git-transport).
   Private source fetches can use Git HTTPS credential helpers or SSH keys and
   your existing SSH agent (`SSH_AUTH_SOCK`). SSH uses the forge hostname with
   user `git`; `~/.ssh/config` can select the key, port, or destination hostname.
   Fetches do not prompt: unlock passphrase-protected keys in your SSH agent and
   establish host trust outside DiffButler first. Git URL rewrites are honored
   for HTTPS and SSH; custom `GIT_SSH_COMMAND` overrides are not inherited.

   Source-preparation diagnostics record each attempted transport, the failed Git
   operation and exit status, and a safe explanation for recognized authentication,
   SSH host-trust, DNS, network, certificate, or missing-ref errors. If both transports
   fail, both explanations are retained. Normal logs exclude Git/SSH output.
   For unknown errors, enable the [Git debug log](#debugging-source-preparation).
   A missing `git_transport` setting means `auto` (HTTPS followed by SSH), even when
   your existing checkout uses SSH. To try SSH first, set `git_transport = "ssh"`
   under `[repos."owner/repo"]` in the selected configuration file.

2. **Create a fine-grained personal access token.** Open
   [Personal access tokens](https://github.com/settings/personal-access-tokens),
   choose the resource owner and repositories to monitor, and grant the repository
   permission **Pull requests: Read-only**. Set the token in `GITHUB_TOKEN` before
   starting DiffButler. Enter the variable name during setup, never the token itself.
   Organization-owned repositories may require token approval. See GitHub's
   [token creation guide](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).
   Pull-request reads support this permission in the
   [REST API](https://docs.github.com/en/rest/pulls/pulls#list-pull-requests).
   For direct review requests with this minimal token, set `include_team_requests = false`;
   team membership discovery needs separate organization access. This token is for
   reading PRs; private Git source fetches need their own working read credentials.

3. **Anonymous public access, as a last resort.** This works only for public repositories
   and is not recommended for regular use: GitHub.com allows just 60 requests per hour
   per IP, compared with the usual 5,000 per hour for an authenticated user.
   See [GitHub's rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api).
   Choose `anonymous` during public-repository setup, or omit `token_env` in a manual
   configuration. Merely exporting a token does not authenticate an existing account
   configured without `token_env`.

## Configure and check an account

```toml
[[forges]]
kind = "github"
base_url = "https://api.github.com"
token_env = "GITHUB_TOKEN"
include_team_requests = false # Enable if your token also supports team membership discovery.
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
never prompts or saves tokens. Use the environment or an existing `gh` login.
First-run setup recommends token access for public repositories and requires it for
private repositories. It asks for an environment variable name and validates that
variable directly; export the token before starting setup. It never asks for a token value. Tokens and raw HTTP/auth error bodies never enter
diagnostics or persistence.

For the anonymous public fallback, omit `token_env` (`None` internally):

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
head changes and observed closure/merge still apply. The [dashboard](dashboard.md)
also reconciles submitted feedback, inline comments and discussion comments, retaining
receipts and respecting later local manual/waiting choices.

Completion retains the task and SHA in history, removes it from active listing, and
atomically inserts a pending cleanup job. Closed/merged PRs stop routine refresh.
Completed open PRs remain monitored and get a new cycle on a later push/request.
Completion dispatches owned native-session cleanup, while retaining normalized history.
The dashboard polls periodically; `sync` remains a single CLI refresh.

## Transport and remaining work

Requests are serialized within each adapter, with at least 0.1 seconds between request
starts for authenticated access, including search. Anonymous access retains a
one-second gap, or 6.1 seconds between search requests. Redirects and retry attempts
use the same pacing.
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
GitLab are not implemented. Pinned local Git source reads and human-instructed native
agent posting are implemented in the managed workflow. This read adapter does not establish
background-agent isolation. Claude has offline managed handoff coverage; live Claude
and macOS handoff validation remain outstanding. See [native reviews](native-reviews.md).

Startup prints the current validation step. GitHub setup checks have a 30-second
limit per phase; quota pacing waits longer than five seconds return a retry delay
instead of silently blocking setup. No setup choices are saved on validation failure.
Reset removes local files, not GitHub's server-side quota.

## Debugging source preparation

To diagnose a failure when pressing `r`, launch the dashboard with:

```sh
diffbutler --log-level debug
```

Reproduce the failure once, then inspect
`$XDG_STATE_HOME/diffbutler/logs/diffbutler.log`, or
`~/.local/state/diffbutler/logs/diffbutler.log` when `XDG_STATE_HOME` is unset.
Use `--log-level` before any subcommand, for example
`diffbutler --log-level debug dashboard`. Levels are `debug`, `info` (default),
`warning`, `error`, and `critical`, case-insensitively.
Restart without `--log-level debug` to return to normal logging. Existing debug
records remain until log rotation or reset; logs rotate at 5 MiB with three backups.

Debug records include the application version, managed repository and local checkout
paths, PR identity, recorded base/head SHAs, transport preference, actual Git executable
and argv, and the explicitly constructed Git environment. Relevant omitted Git/proxy
variable **names** are recorded without their values. Each Git command has a unique ID,
elapsed time, outcome, exit status when available, and up to 64 KiB of stderr with a
byte count and truncation indicator. This includes initialization, HTTPS/SSH fetches,
local object reads, and pin updates. Timeouts and cancellations retain collected stderr.
Source stdout, blobs, diffs, the full parent environment, and Git configuration dumps
are never logged. Debug logging does not enable Git tracing or verbose SSH output.

Credential masking covers known token patterns, token/password/secret environment
values of at least four characters, URL userinfo and query strings, credential assignments,
authorization headers, and private-key blocks. Masking is heuristic: private repository
names, hostnames, paths, commit IDs, server messages, and unrecognized secrets can remain.
Review debug logs before sharing them. Files remain owner-only and logging never writes
into the interactive agent's terminal. Normal user-facing errors retain safe summaries.
