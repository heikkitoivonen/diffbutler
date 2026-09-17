# Session ownership and cleanup (M2 internal services)

`LifecycleService` and `SQLiteLifecycleStore` add durable foreground ownership and
an internal cleanup worker step. They do not launch an interactive agent, identify
or kill orphan processes, install a periodic worker, or provide a native backend deletion
adapter. Production CLI/dashboard wiring remains pending.

An explicit resume action can acquire a foreground lease only for a current open
review task with a saved backend identity. A queued/running attempt or retained
worker claim prevents acquisition. A foreground lease blocks another attachment,
background requests/claims, and suspension for that task. Revision and task-version
baselines are saved with the exact backend ID; a push cannot replace the conversation
under its owner. Acquisition does not create an AI run or authorize publication.
It is not yet a forge-activity baseline or a complete native handoff record.

Migration `0007` retains foreground leases and cleanup claims. They never expire
based on elapsed time. `held_operations` exposes immutable records after restart.
The caller may release foreground ownership only after establishing process-tree
exit or that launch never occurred. The terminal layer still owns restoration;
merely returning from an agent or receiving EOF is insufficient evidence for an
arbitrary process tree. Terminal serialization remains the UI's responsibility.

Completion already creates a durable cleanup job in the queue transaction. The
internal `cleanup_once` worker step claims pending/due failed jobs and skips any
job with background or foreground ownership. A busy first job does not block later
jobs. No additional human cleanup action is required after completion. Cleanup
failures do not resurrect completed tasks or change followed-PR monitoring.

A caller supplies a trusted `SessionCleaner` implementing idempotent deletion of
only manifest-owned resources and supported backend session data. It receives a
frozen record including the opaque backend ID, transcript reference, and manifest.
Opaque IDs are never interpreted as filesystem paths by the core. Unsupported
backend deletion must raise, rather than claim success. The filesystem adapter described below performs real local deletion in offline tests;
native backend deletion remains unsupported.

On successful deletion, one transaction marks sessions cleaned, removes backend
IDs/transcript references/manifests and raw run stdout/stderr, and completes the
job. Run metadata and normalized analysis (including anchor outcomes) remain.
On failure, manifests and identities remain available for an idempotent retry;
a fixed diagnostic avoids storing backend exception text. Retry backoff is bounded
at one hour. If deletion partly succeeded, the adapter must tolerate those resources
already being absent on retry.

Cancellation or process interruption preserves the claim. Once a trusted supervisor
establishes orphan cleanup-operation exit, `acknowledge_cleanup_exit` schedules a
retry. Process discovery and exit verification are not implemented here: a restarted
worker does not guess that an older owner has died. This is conservative recovery,
not automatic orphan reclamation. All validation uses offline fakes and SQLite.

## Owned local resources

`core.resources.provision_directory` registers an immutable UUID manifest under an
existing worker claim before creating a directory. A crash between registration
and creation leaves an absent resource that cleanup can safely retry. Registration
is idempotent and verifies session ownership. Completion/cancellation may occur while
the claim is held; the manifest remains available and cleanup waits for release.

`reviewers.resources.OwnedDirectories` explicitly initializes a new private root;
it refuses to adopt an existing directory. Its persistent root UUID must match on
restart. Resource names use only session/resource UUIDs, never repository paths or
opaque backend IDs. Opening the root and marker rejects symlinks; descriptor-relative
cleanup requires Python's symlink-safe `rmtree`. Nested symlinks are removed without
following their targets. Other resources and the root remain intact. Existing
resource directories are never adopted by creation. Filesystem I/O runs off the event
loop, and cancellation waits for that I/O to settle before propagating.

The configured root and its parents are trusted administration, must remain private,
and must never be mounted into a reviewer or contain nested filesystem mounts. This
is resource ownership, not a sandbox. Root provisioning is internal, not CLI wiring.
Snapshot/worktree creation and fetched-ref cleanup remain unimplemented.

`OwnedSessionCleaner` validates all manifest entries before deletion and uses a
reviewer-specific `BackendSessionDeleter` for opaque backend identities. Missing
backend deletion support, unknown manifests, mismatched ownership, or external
transcript references fail cleanup without claiming success. No native backend
adapter is supplied yet. Transcript files inside owned directories are removed with
them. Backend deletion occurs first and must be idempotent, since later filesystem
failure can require retry. Normalized SQLite analysis remains intact after success.
