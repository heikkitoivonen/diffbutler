# Review scheduling foundations (M2)

The internal `core.review.ReviewService` supports explicit review requests, durable
worker claims, cancellation, and failed-worker release through the `ReviewStore`
protocol. `SQLiteReviewStore` implements these as short serialized transactions.
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
attempt becomes failed and routes to human review; stale/cancelled attempts retain
their state and cannot overwrite newer queue attention. Successful result parsing
and finalization are deferred. Cancellation timestamps record invalidation, not
proof of process exit.

Claims survive restart and do not expire automatically. There is no orphan-worker
recovery, process termination, foreground workspace lease, or cleanup worker yet.
A future executor must enforce source/credential/filesystem/network isolation before
launch, observe cancellation, terminate its process tree, and establish exit before
releasing ownership. A future cleanup worker must honor outstanding ownership.
These database foundations do not themselves enforce process isolation or delete
session resources. No real backend or forge credentials are used in the tests.
