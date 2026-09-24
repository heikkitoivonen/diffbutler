# Local source context

`sources.git.GitSourceReader` implements the reviewer `SourceReader` protocol for
dashboard-mediated reads from a configured, trusted local Git repository. It is
used by the managed native review workflow and independently testable.

Opening a context requires full base and head commit object IDs already available
locally, with complete history and a unique merge base. The reader inventories
the merge-base and head trees and serves only blobs named in those trees.
The recorded PR base tip remains separate from the resolved comparison base.
Surrounding unchanged files are available, independently of the current branch,
index, and uncommitted or untracked files. Branch names, abbreviated IDs,
and revision expressions are rejected. Missing objects fail without fetching.

Reads return original bytes. Symlinks return the stored link text and are never
followed; submodule entries report unavailable source. Missing paths return `None`.
Non-UTF-8 or lexically unsupported tree paths reject context preparation rather
than silently hiding files. Changed-file inventory includes additions, deletions,
content changes, and mode changes. Renames appear as deletion plus addition.
The adapter provides a unified diff from the resolved merge base to the pinned head.
Base-side reads, snapshots, and changed-file inventory use that same merge base.
Newer commits present only on the target branch are excluded. Missing or shallow
history, unrelated histories, and multiple best merge bases fail preparation;
there is no endpoint-diff fallback.
Renames remain deletion/addition pairs. Diff generation forces text and disables
external drivers and textconv, so working-tree attributes cannot hide changes as
binary. Diffs containing NUL bytes or invalid UTF-8 fail preparation; they are not
silently omitted, decoded lossily, or truncated. Binary-aware diff context is not
implemented.

`core.context.assemble_context` consumes the `ReviewSources` protocol and builds an
immutable `ReviewContext` for a running attempt and an open PR. Both source and PR
base/head identities must match the run before any diff read. Assembly propagates
timeout and cancellation. It builds a [DiffIndex](diff-anchors.md) from the original
diff before masking,
rejecting malformed or unsupported hunks. It preserves redacted mediated access to surrounding source and records
the absence of a checkout and of executed project checks. Instructions and schema
are caller-supplied trusted inputs; source and PR metadata remain untrusted data.

This is an internal assembly function, not a dispatcher or authorization check.
The caller must load the PR belonging to the task and recheck durable worker
ownership, freshness, and policy before execution. No reviewer is launched and no
source leaves the machine in this function. The explicit native review workflow
serializes it and launches the configured agent with native read-only permissions.

Git runs asynchronously with argv execution, no terminal input, captured output,
a timeout, and process-group termination on cancellation or
failure. Reads use `ls-tree`, `cat-file`, and `diff-tree`, without checkout, filters, textconv,
replacement objects, or project code execution. Git receives an explicit environment
without inherited tokens, SSH-agent access, or global/system Git configuration.
Lazy fetch is disabled and all Git transport protocols are denied. The implementation
uses the documented [Git environment controls](https://git-scm.com/docs/git) and
[NUL-delimited tree format](https://git-scm.com/docs/git-ls-tree).

The local Git executable and repository administration (including `.git` configuration
and object storage) are trusted. This is source preparation, not an OS sandbox. The [native reviewer](native-reviews.md)
uses the installed agent CLI’s read-only profile. Codex and Claude receive owned Git
checkouts of original committed files.
DiffButler does not provision an additional sandbox. Managed remote retrieval,
pinned source workspaces, manifests, leases, and cleanup are implemented.

Offline tests use disposable Git repositories and synthetic executables. They cover
pinned reads with a dirty index/worktree, surrounding files, links, binary and unusual
filenames, mode changes, submodules, replacement refs, filters, missing promisor objects,
environment filtering, safe diagnostics, large output, timeout, and cancellation.

For full native snapshots, `read_blobs` uses one streaming `cat-file --batch` process
and groups paths from both trees by object ID. Each unique blob is read once, using
the same environment and protocol restrictions as single-file reads. Batch responses
must match the requested ID, blob type, length, and record boundary. Cancellation,
early iterator closure, and errors terminate and reap the owned process group.
Snapshot callers close the iterator explicitly and publish only after all reads succeed.

## Source redaction

Context assembly masks credential patterns in the diff and wraps all subsequent
mediated file reads with the same redactor, including unchanged files on either
side. The raw Git adapter continues to return original bytes for trusted internal
use. Source bytes and local files are never
modified in place.

The initial heuristic recognizes GitHub-style tokens, AWS access key IDs,
`sk-` and Slack-style tokens, complete PEM private-key blocks, and high-entropy
values assigned to credential-like names (token, password, secret, API/access key).
Assignment values require at least 16 characters and 3.5 bits of Shannon entropy
per character; recognized token prefixes and PEM blocks do not use that threshold.
Matches become same-length asterisks with CR/LF positions preserved, including
non-UTF-8 file content. Overlapping matches count once; repeated occurrences count
separately. Mediated diff/file reads emit an INFO log containing only the count, never
values, hashes, paths, or snippets. Snapshot preparation reports an aggregate count
for unique blobs when complete, avoiding per-file redaction log entries.

This is heuristic masking, not proof that source is secret-free. Encoded, split,
unknown-format, low-entropy, and partial private-key secrets can remain; false
positives are possible. Masking multiline keys can also obscure diff prefixes
inside the block, so the masked text must not be used to construct DiffIndex.
PR metadata, paths, trusted instructions/schema, and future transcripts are outside
this source-only pass and need handling at the prompt serialization boundary below.
The context explicitly records the heuristic limitation. A Python wrapper does
not confine an executable or prevent access to its underlying objects; an isolated
runner must expose only mediated redacted data. Legacy snapshot preparation uses
the same redactor. Native checkouts expose original committed files and cached Git
objects without source redaction.

## Prompt serialization

Codex and Claude use `render_git_prompt`: a short source-only instruction naming
the pinned target tip, merge base, and head refs. It omits the embedded diff and
file list; the agent inspects the checkout with Git. The policy forbids project execution and publication, and requests Markdown
draft findings. The JSON serialization below applies to the snapshot path.

`reviewers.prompt.render_prompt` builds a local UTF-8 payload with a versioned,
precision-first source-only policy and the parser's generated `ReviewDocument` schema.
It requires orientation even for empty findings, directs the reviewer to preserve
limitations in `unverified`, and never treats a clean pass as approval. The legacy
`ReviewContext.schema_json` field is not used by this renderer; the parser defines
its output contract.

PR metadata, changed paths, limitations, and diffs are JSON-quoted inside an explicitly
untrusted data object. Repository content must not be passed as trusted instructions.
Every dynamic string, including trusted instructions, is redacted before JSON escaping
so multiline credential patterns remain recognizable. Host paths, callbacks, PR URLs,
and backend identities are not serialized. Prompts are serialized in full without
a byte limit. No template engine, transmission, or reviewer is invoked.
Repository descriptions/conventions and configurable templates are not implemented.

Framing does not enforce model behavior, and heuristic masking does not guarantee
secret-free output. A future mediated source transport must apply the same pre-escape
redaction to each reply. The original DiffIndex remains local and unchanged.
