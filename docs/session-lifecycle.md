# Session ownership and cleanup

`LifecycleService` and `SQLiteLifecycleStore` add durable foreground ownership and
an internal cleanup worker step. The [native CLI integration](native-reviews.md) supplies launch and owned-home
cleanup. The [dashboard](dashboard.md) polls forge activity and dispatches due cleanup.

An explicit resume action can acquire a foreground lease only for a current open
review task with a saved backend identity. A queued/running attempt or retained
worker claim prevents acquisition. A foreground lease blocks another attachment,
background requests/claims, and suspension for that task. Revision and task-version
baselines are saved with the exact backend ID; a push cannot replace the conversation
under its owner. Acquisition does not create an AI run or authorize publication.
The monitor separately retains the last complete forge-activity baseline; the dashboard marks
refresh pending on entry and reconciles actual reviews/comments on verified return.

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
the native path deletes owned workspaces and legacy private agent homes, while
preserving conversations in the user’s normal agent home.

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
The native path creates pinned Git checkouts for Codex and Claude,
optionally under a separate temporary root whose identity is recorded in the persistent session root before creation. Its
session directories derive their ownership from the registered home manifests.
Cleanup deletes those workspaces before the owned session directory and releases only that session's
shared-repository pins afterward. Missing temporary roots are already clean; replaced
roots fail validation. Explicit resume reconstructs missing workspaces under its foreground
lease, preserving the original workspace format. Whole inactive repositories have
configurable retention.

`OwnedSessionCleaner` validates all manifest entries before deletion and uses a
reviewer-specific `BackendSessionDeleter` for opaque backend identities. Missing
backend deletion support, unknown manifests, mismatched ownership, or external
transcript references fail cleanup without claiming success. The native integration additionally supports deleting an entire manifest-owned Codex home. Transcript files inside owned directories are removed with
them. Backend deletion occurs first and must be idempotent, since later filesystem
failure can require retry. Normalized SQLite analysis remains intact after success.

## Launch intent and reboot recovery

Migration `0008` saves a host/boot identity before spawn under an existing background,
foreground, or cleanup claim. A second launch intent for the same token is rejected,
including after a crash before PID checkpointing. PID checkpoints cannot replace an
already bound launcher. Historical metadata remains after ownership is released.

`ProcessRecovery` can release local owners after a verified reboot of the same host.
It never signals a saved PID, creates a review, or infers descendant death from EOF,
a missing launcher, or age. Same-boot crashes and older claims without launch metadata
remain held pending supervisor/operator verification. Linux host/boot reads require
a unique machine-id and a local, nonshared database; moved databases do not prove exit.
On macOS, the corresponding identities are `IOPlatformUUID` from `ioreg` and
[`kern.bootsessionuuid`](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_sysctl.c)
from `sysctl`. Missing, ambiguous, invalid, or zero identities
fail closed before launching the reviewer. Neither timestamps nor boot volume IDs
are used to infer a reboot.
The Linux [boot identifier](https://www.kernel.org/doc/html/v6.13/admin-guide/sysctl/kernel.html#random)
is stable during a boot. Unsupported identity reads fail closed. This mechanism is
independent of native agent sandboxing and does not validate kernel confinement.

The configured core handoff context owns foreground acquisition, launch intent, PID
checkpointing, release, and completion cleanup. The CLI and TUI attach the terminal and report uncertain exit; the core refreshes
forge activity after verified return. Cleanup records host/boot intent before filesystem I/O;
cancellation retains its claim, and verified reboot recovery schedules a retry.
`approved` and `cleanup` show all-history job counts and return status 1 while failed
jobs remain. Pending and running jobs remain visible without implying deletion.
