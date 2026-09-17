# Local source context (M2 foundation)

`sources.git.GitSourceReader` implements the reviewer `SourceReader` protocol for
dashboard-mediated reads from a configured, trusted local Git repository. It is
an internal adapter; no CLI command or review dispatcher uses it yet.

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
and cancellation. It preserves mediated access to surrounding source and records
the absence of a checkout and of executed project checks. Instructions and schema
are caller-supplied trusted inputs; source and PR metadata remain untrusted data.

This is an internal assembly function, not a dispatcher or authorization check.
The caller must load the PR belonging to the task and recheck durable worker
ownership, freshness, and policy before execution. No reviewer is launched and no
source leaves the machine. Source redaction and prompt rendering are still deferred;
the assembled context is not yet suitable for sending to a real backend.

Git runs asynchronously with argv execution, no terminal input, captured output,
bounded byte counts, a timeout, and process-group termination on cancellation or
failure. Reads use `ls-tree`, `cat-file`, and `diff-tree`, without checkout, filters, textconv,
replacement objects, or project code execution. Git receives an explicit environment
without inherited tokens, SSH-agent access, or global/system Git configuration.
Lazy fetch is disabled and all Git transport protocols are denied. The implementation
uses the documented [Git environment controls](https://git-scm.com/docs/git) and
[NUL-delimited tree format](https://git-scm.com/docs/git-ls-tree).

The local Git executable and repository administration (including `.git` configuration
and object storage) are trusted. This is a source mediation boundary, **not an OS
sandbox for a reviewer process**. Do not pass the local repository or this in-process
adapter as evidence that an arbitrary CLI agent is isolated. Enforced filesystem,
credential, and network isolation is still required before any background reviewer
launch. No reviewer executable is connected, including for previously foreground
conversations. Source redaction, remote retrieval, owned snapshots, durable manifests,
foreground leases, and cleanup remain future work.

Offline tests use disposable Git repositories and synthetic executables. They cover
pinned reads with a dirty index/worktree, surrounding files, links, binary and unusual
filenames, mode changes, submodules, replacement refs, filters, missing promisor objects,
environment filtering, safe diagnostics, output limits, timeout, and cancellation.
