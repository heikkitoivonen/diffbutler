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
This adapter does not yet produce unified diffs or merge-base comparisons.

Git runs asynchronously with argv execution, no terminal input, captured output,
bounded byte counts, a timeout, and process-group termination on cancellation or
failure. Reads use `ls-tree` and `cat-file`, without checkout, filters, textconv,
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
