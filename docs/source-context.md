# Local source context (M2 foundation)

`sources.git.GitSourceReader` implements the reviewer `SourceReader` protocol for
dashboard-mediated reads from a configured, trusted local Git repository. It is
used by the managed native review workflow and independently testable.

Opening a context requires full base and head commit object IDs already available
locally. The reader inventories both trees and serves only blobs named in those
trees. Surrounding unchanged files are available, independently of the current
branch, index, and uncommitted or untracked files. Branch names, abbreviated IDs,
and revision expressions are rejected. Missing objects fail without fetching.

Reads return original bytes. Symlinks return the stored link text and are never
followed; submodule entries report unavailable source. Missing paths return `None`.
Non-UTF-8 or lexically unsupported tree paths reject context preparation rather
than silently hiding files. Changed-file inventory includes additions, deletions,
content changes, and mode changes. Renames appear as deletion plus addition.
The adapter also provides a bounded unified diff between those exact two commits.
It compares endpoints, not the merge base; the context records that limitation.
Renames remain deletion/addition pairs. Diff generation forces text and disables
external drivers and textconv, so working-tree attributes cannot hide changes as
binary. Diffs containing NUL bytes or invalid UTF-8 fail preparation; they are not
silently omitted, decoded lossily, or truncated. Binary-aware diff context and
merge-base comparison remain future work.

`core.context.assemble_context` consumes the `ReviewSources` protocol and builds an
immutable `ReviewContext` for a running attempt and an open PR. Both source and PR
base/head identities must match the run before any diff read. Assembly forwards the
diff byte budget, independently checks its UTF-8 byte length, and propagates timeout
and cancellation. It builds a [DiffIndex](diff-anchors.md) from the original diff before masking,
rejecting malformed or unsupported hunks. It preserves redacted mediated access to surrounding source and records
the absence of a checkout and of executed project checks. Instructions and schema
are caller-supplied trusted inputs; source and PR metadata remain untrusted data.

This is an internal assembly function, not a dispatcher or authorization check.
The caller must load the PR belonging to the task and recheck durable worker
ownership, freshness, and policy before execution. No reviewer is launched and no
source leaves the machine in this function. The explicit native review workflow
serializes it and launches the configured agent with native read-only permissions.

Git runs asynchronously with argv execution, no terminal input, captured output,
bounded byte counts, a timeout, and process-group termination on cancellation or
failure. Reads use `ls-tree`, `cat-file`, and `diff-tree`, without checkout, filters, textconv,
replacement objects, or project code execution. Git receives an explicit environment
without inherited tokens, SSH-agent access, or global/system Git configuration.
Lazy fetch is disabled and all Git transport protocols are denied. The implementation
uses the documented [Git environment controls](https://git-scm.com/docs/git) and
[NUL-delimited tree format](https://git-scm.com/docs/git-ls-tree).

The local Git executable and repository administration (including `.git` configuration
and object storage) are trusted. This is source preparation, not an OS sandbox. The [native reviewer](native-reviews.md)
uses the installed agent CLI's read-only profile and owned pinned snapshots of these
blobs. DiffButler does not provision an additional sandbox. Remote retrieval and
worktrees remain future work; owned snapshots, manifests, leases, and cleanup are
implemented in the managed path.

Offline tests use disposable Git repositories and synthetic executables. They cover
pinned reads with a dirty index/worktree, surrounding files, links, binary and unusual
filenames, mode changes, submodules, replacement refs, filters, missing promisor objects,
environment filtering, safe diagnostics, output limits, timeout, and cancellation.

## Source redaction

Context assembly masks credential patterns in the diff and wraps all subsequent
mediated file reads with the same redactor, including unchanged files on either
side. The raw Git adapter continues to return original bytes for trusted internal
use. Diff byte budgets apply before masking. Source bytes and local files are never
modified in place.

The initial heuristic recognizes GitHub-style tokens, AWS access key IDs,
`sk-` and Slack-style tokens, complete PEM private-key blocks, and high-entropy
values assigned to credential-like names (token, password, secret, API/access key).
Assignment values require at least 16 characters and 3.5 bits of Shannon entropy
per character; recognized token prefixes and PEM blocks do not use that threshold.
Matches become same-length asterisks with CR/LF positions preserved, including
non-UTF-8 file content. Overlapping matches count once; repeated occurrences count
separately. Each diff/file pass emits an INFO log containing only the count, never
values, hashes, paths, or snippets.

This is heuristic masking, not proof that source is secret-free. Encoded, split,
unknown-format, low-entropy, and partial private-key secrets can remain; false
positives are possible. Masking multiline keys can also obscure diff prefixes
inside the block, so the masked text must not be used to construct DiffIndex.
PR metadata, paths, trusted instructions/schema, and future transcripts are outside
this source-only pass and need handling at the prompt serialization boundary below.
The context explicitly records the heuristic limitation. A Python wrapper does
not confine an executable or prevent access to its underlying objects; an isolated
runner must expose only mediated redacted data. Native snapshot preparation uses the same redactor.

## Prompt serialization

`reviewers.prompt.render_prompt` builds a bounded local UTF-8 payload with a versioned,
precision-first source-only policy and the parser's generated `ReviewDocument` schema.
It requires orientation even for empty findings, directs the reviewer to preserve
limitations in `unverified`, and never treats a clean pass as approval. The legacy
`ReviewContext.schema_json` field is not used by this renderer; the parser defines
its output contract.

PR metadata, changed paths, limitations, and diffs are JSON-quoted inside an explicitly
untrusted data object. Repository content must not be passed as trusted instructions.
Every dynamic string, including trusted instructions, is redacted before JSON escaping
so multiline credential patterns remain recognizable. Host paths, callbacks, PR URLs,
and backend identities are not serialized. A serialized byte limit rejects oversized
prompts without truncation. No template engine, transmission, or reviewer is invoked.
Repository descriptions/conventions and configurable templates remain future work.

Framing does not enforce model behavior, and heuristic masking does not guarantee
secret-free output. A future mediated source transport must apply the same pre-escape
redaction to each reply. The original DiffIndex remains local and unchanged.
