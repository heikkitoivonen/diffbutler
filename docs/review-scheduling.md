# Review scheduling foundations

The internal `core.review.ReviewService` supports explicit review requests, durable
worker claims, cancellation, result finalization, and failed-worker release through the `ReviewStore`
protocol. It also reads durable sessions, checkpoints backend resume IDs under
worker ownership, and marks idle sessions suspended. `SQLiteReviewStore` implements
these as short serialized transactions.
There is no CLI review command, dispatcher, or reviewer process yet. Existing queue
commands never create sessions or attempts.

`request_review` takes a task/head/reviewer/prompt/config identity. It validates the
current open task and account-specific head snapshot, then atomically creates or
reuses a conversation, creates an attempt, and moves the task to `QUEUED`. Repeated
starts for the same active analysis return that attempt; a different analysis is
rejected while one is active. After an attempt ends, a deliberate request creates
the next attempt even at the same SHA. Cleaned conversations get a new generation.
The service requires an explicit `ReviewPolicy` containing allowed reviewer IDs and
disabled repository names. Future application wiring must derive this from settings;
the scheduling service is not yet connected to configured CLI operations.

`claim_review` changes one queued attempt to `running` and its task to `AI_RUNNING`.
Only one caller can claim an attempt. A paused caller obtains no claim; disabled
repositories/reviewers cannot be claimed. The supplied capacity bounds held claims
across the database, including cancelled workers that have not acknowledged exit.
Callers sharing the ledger must supply the same configured capacity and pause policy;
there is no persistent global scheduler configuration yet.

Migration `0004` stores a worker's opaque claim token separately from run status.
Pushes and renewed requests mark queued/running attempts stale and return attention
to pending AI review. Completion cancels attempts and schedules cleanup in the same
transaction. Neither event creates a replacement attempt. These changes preserve
worker ownership until exit is acknowledged, preventing a retry from overlapping
an old worker in the same task. Local review actions advance the account's sync
generation so an older refresh must retry instead of overwriting the action.

`get_run` reads durable status; cancellation is idempotent. `release_review` requires
the exact claim token and acknowledges that the worker has exited. A still-running
attempt becomes failed and returns to pending AI review for an explicit retry;
stale/cancelled attempts retain their state and cannot overwrite newer queue attention.
Use `finalize_review` below
when captured output is available. Cancellation timestamps record invalidation, not
proof of process exit.

Claims survive restart and do not expire automatically. There is no orphan-worker
recovery, process termination, foreground workspace lease, or cleanup worker yet.
The native executor selects native agent permissions, observes cancellation,
terminates its owned process group, and waits before releasing ownership. The cleanup
worker honors outstanding ownership. See [native execution limits](native-reviews.md).
These database foundations do not themselves enforce process isolation or delete
session resources. No real backend or forge credentials are used in the tests.

## Conversation identity checkpoints

`get_session` returns an immutable DTO with the PR display name, task/reviewer,
generation, status, and opaque backend resume ID. Reading this metadata never
launches or resumes an agent. The display name is not the backend identity.

`bind_backend_session` requires a held worker claim token and selects the session
through that claim's run. The first ID is persisted; repeating the same ID is
idempotent, but replacing it with a different ID is rejected. Released or unknown
tokens cannot write. IDs must be nonblank, at most 1024 characters, and contain no
C0/C1 control characters. They remain opaque data, never paths or shell templates.
A backend adapter must validate its own ID format before use.

A cancelled or stale worker can still checkpoint its conversation before releasing
ownership, including after task completion. This preserves the identity needed by
future cleanup without reviving the run, changing queue attention, or releasing its
claim. A cleaned session rejects all such checkpoints. Future cleanup must continue
to wait for held claims; writing a resume ID is not evidence that the worker exited.

`suspend_session` marks an idle, current, open-task conversation suspended. It
rejects queued/running attempts, held claims (even cancelled ones), completed tasks,
and sessions that are cleaned or awaiting cleanup. It preserves the resume ID and
queue state and performs no backend operation. A later explicit request can reuse
the conversation; successfully claiming that request returns the session to ready.
Claims reject sessions that have entered cleanup since the request was queued.

Offline tests use a synthetic CLI to create a conversation, checkpoint its ID,
close/reopen the database, and resume the same synthetic conversation on an explicit
retry. These older protocol tests are supplemented by the managed native CLI acceptance
test covering pinned snapshots, restart, exact resume, and owned home cleanup.
The native agent and platform limits are in the native review guide.


## Result finalization

`finalize_review(token, output)` acknowledges verified worker exit, parses already-
extracted assistant text, and atomically stores the result, updates current queue
attention, checkpoints an optional backend session ID, and deletes the worker claim.
The caller must first establish that the entire process tree has exited. A final
message or EOF alone is not proof. This internal method performs no process control;
the native worker supplies the required process-group exit ordering. A synthetic executable test captures both pipes,
waits for exit, then finalizes its result.

The transaction rechecks the held token, task, account-specific open PR snapshot,
base/head revisions, and session cleanup state. Current successful runs route to
`FINDINGS_TRIAGE` when any finding meets both thresholds, otherwise to
`PENDING_HUMAN_REVIEW`. Malformed output or a nonzero exit records a failed run and
returns to pending AI review with a persisted diagnostic. Retries require explicit
user action. No outcome completes or approves
a review cycle. Local routing advances the sync generation so an older refresh must
retry. Cancelled/stale runs can retain late results but cannot change queue attention
or their invalidation timestamps, including after completion and a new review cycle.
A base mismatch detected at finalization marks a running attempt stale and restores
pending AI attention without launching replacement work.

The defaults are confidence `0.5` and severity `minor`; severity order is
`question < nit < minor < important < blocking`. The managed native workflow supplies configured overrides. The full normalized document is retained, including every
low-confidence/nit/question finding. Zero-based `selected_indices` record the subset
that met both thresholds, alongside the applied thresholds and normalization notes.
Finalization accepts the pinned context DiffIndex and retains [anchor outcomes](diff-anchors.md)
for every finding, including demoted file notes. Missing indexes remain explicitly
unvalidated. Per-finding disposition records are not implemented.

Migration `0005` adds `review_result`, with one immutable-by-API result per run.
Raw stdout/stderr and exit code occupy the existing `ai_run` fields. `get_result`
returns a frozen DTO with the normalized document and raw bytes; JSON serialization
uses base64 for bytes. A known run without a finalized result returns `None`.
Analysis survives automatic cleanup of raw output, represented by null byte fields. Storage currently keeps
output inline; file spillover is not implemented.

Captured output has no DiffButler byte limit. Invalid thresholds,
invalid/replaced backend IDs, and cleaned sessions also reject the operation without
partially saving a result or releasing ownership. A repeated finalization with a
consumed token is rejected; after an uncertain commit, read the run/result to determine
whether it succeeded. Concurrent finalizers cannot overwrite each other's result.

[Lifecycle services](session-lifecycle.md) now serialize foreground ownership with
background work and defer completion cleanup until ownership is released.

## Internal worker orchestration

`ReviewWorker.execute_requested` consumes one explicitly requested run under the
existing capacity/pause-aware claim. It does not discover/enqueue/retry work and
is connected to the native Codex factory; unit tests also inject in-memory executions.
Context preparation is followed by identity/index checks and a transactional
`claim_is_current` recheck of ownership, current base/head, session status and review
policy. A moved base invalidates the attempt before launch, retaining its claim
until exit acknowledgement; no replacement review is scheduled.

During execution the worker checks validity periodically and enforces the context
timeout. Backend identities can be checkpointed while the claim is held; a later
explicit attempt receives the exact saved identity, including after database
restart. Every exit path calls the execution adapter's `stop_and_wait`, even after
successful output. Only after that establishes whole-process-tree exit may output
be finalized or a failed/stale/cancelled run released. Repeated cancellation cannot
interrupt that stop operation. Stop failure leaves ownership held for recovery.
An uncertain finalization commit is never blindly retried or overwritten.

This protocol is a trusted adapter obligation, not an independent OS isolation
implementation. The [native factory](native-reviews.md) provides captured I/O,
per-launch permission selection, durable launch intent, and pinned snapshots.
It relies on the installed agent's sandbox and ordinary process-group lifecycle;
deliberately detached descendants and same-boot orphan recovery remain limitations.
