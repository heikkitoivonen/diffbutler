# Captured review output parsing

`reviewers.parsing.parse_review` is an internal, offline parser for already-extracted
assistant text in a `ReviewOutput`. With an explicit `output_format`, it first
extracts a backend JSON envelope or JSONL stream. It does not launch a process,
update the queue, or acknowledge worker exit.

It tries whole-document JSON, the last JSON fenced block, then the first balanced
outer object. Object matching respects quoted braces and escapes. If a final fenced
answer is malformed, recovery stays within that answer instead of falling back to
an earlier clean answer. Once JSON decodes, schema failure rejects the entire
result; it does not search for a different answer or discard invalid findings.
Duplicate object keys and nonfinite JSON numbers are rejected.

The frozen Pydantic contract requires orientation (intent, shape, look-here locations,
low-risk notes, and unverified checks) and an explicit findings array. Empty findings
are supported. Field types, positive line numbers, ordered ranges, relative repository
paths, sides, and confidence bounds are checked. Unknown fields are rejected.
Unknown string severities/categories become `minor`/`style` with index-based
notes that do not echo backend values. All findings remain in the result, including
low-confidence, nit, and question findings. The core applies thresholds during
[result finalization](review-scheduling.md#result-finalization), retaining all findings.

`ParsedReview` retains the original stdout, stderr, exit code, and backend identity
on both success and failure. Failure diagnostics are fixed strings without backend
content. Nonzero exits, invalid UTF-8, and a combined stdout/stderr byte-budget overflow
cannot produce a successful document. This parser checks already-captured bytes;
the native executor also enforces limits while collecting output. Nothing is
logged or persisted by parsing.

The parser validates locations structurally. Context preparation now builds a
[DiffIndex](diff-anchors.md) before redaction; finalization can retain side-aware
anchor outcomes. Huge positive line numbers are demoted when outside the indexed hunks. Orientation quality and coverage are not
proved by schema validity. Successful parsing neither approves a PR nor establishes
forge publication, and does not release a worker claim. The shared core now persists
results and routes queue attention through explicit finalization from the native executor. Tests use synthetic output and make
no real reviewer calls.

## Backend extraction

`EnvelopeFormat` uses tuples of object keys to select assistant text, an optional
opaque session ID, and an optional error flag. `StreamFormat` selects event types
and nested fields; the last complete assistant message wins. Deltas and tool output
are never interpreted as findings. A successful terminal event is required, and
records after it are rejected. Any malformed event, duplicate key, nonfinite number,
failure event, conflicting/invalid session ID, or truncated stream fails the capture
without falling back to an earlier clean answer. Error diagnostics are fixed text.

Raw stdout/stderr remain unchanged, including on extraction or schema failure.
`finalize_review(output_format=...)` persists those raw bytes and checkpoints a
valid extracted backend ID in the same transaction. An invalid final review document
can still preserve its valid conversation identity. Extraction failure does not
checkpoint partially trusted stream identities. No tool/progress text is emitted
as a UI event by this parser. Byte limits apply to the whole captured stream.

Default streaming selectors remain a synthetic test protocol. The native Codex preset
selects completed `agent_message` items, requires the thread ID and `turn.completed`,
and ignores completed tool items. Its launch arguments live in packaged TOML. Claude
selectors remain deferred. See [native review support](native-reviews.md). A terminal success event establishes only protocol completeness, not
process-tree exit, authorization, publication, or approval.
