# Diff anchors (M2)

`forges.base.DiffIndex` exposes a pure domain implementation shared by adapters and
context assembly. It indexes the original pinned unified Git diff before redaction,
retaining only paths and hunk coordinates, never source text. The index records the
base/head pair. Context preparation rejects malformed hunks, overlapping hunks,
invalid paths, binary patches and combined diffs. Empty and mode-only diffs have no
commentable lines. Git quoted UTF-8 paths and no-final-newline markers are supported.
The local source reader represents renames as deletion plus addition; an externally
provided rename diff uses the new path with `old_path` retained.

`is_commentable` checks side and path, revision identity when supplied, and whether
the entire range fits one hunk. Context lines exist on both sides; additions exist
only on the head, deletions only on the base. A zero-length side has no lines.

`snap` conservatively clips a range overlapping exactly one hunk, with at most three
lines of movement at each endpoint by default. It never crosses sides, changes the
path, joins hunks, or guesses a hunk for a point entirely outside the diff. A failed
snap returns no inline anchor. `validate` retains the original location and returns
`exact`, `snapped`, or `demoted`; a demoted finding remains a file-scoped note, never
a missing finding. This conservative policy can demote an off-by-one single-line
location rather than guessing which hunk the model intended.

`ReviewService.finalize_review(diff_index=...)` stores an ordered outcome for every
finding separately from the original model document. Selection thresholds still
retain demoted findings. Migration `0006` adds nullable `anchors_json`: old results
and results finalized without an index report `anchors=None`, explicitly unvalidated.
An empty tuple means a validated result had no findings. The finalization transaction
rejects index revisions that do not match the claimed run without releasing ownership.
Original locations, normalized findings, and validation outcomes survive restart and
raw-output removal. They describe the reviewed revision, not a future PR head.

There is no publication adapter yet. A future publisher must recheck current forge
revisions and forge-specific restrictions before posting; local index validation
alone is not authorization or a guarantee that the forge accepts a comment.
