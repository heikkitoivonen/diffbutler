# Captured review output parsing (M2)

`reviewers.parsing.parse_review` is an internal, offline parser for already-extracted
assistant text in a `ReviewOutput`. It does not launch a process, unwrap a backend
JSON envelope/JSONL stream, update the queue, or acknowledge worker exit.

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
low-confidence, nit, and question findings; threshold selection remains future work.

`ParsedReview` retains the original stdout, stderr, exit code, and backend identity
on both success and failure. Failure diagnostics are fixed strings without backend
content. Nonzero exits, invalid UTF-8, and a combined stdout/stderr byte-budget overflow
cannot produce a successful document. This parser checks already-captured bytes;
a future executor must also enforce limits while collecting output. Nothing is
logged or persisted by parsing.

Locations are structurally validated only: there is no DiffIndex, source existence
check, line snapping, or commentability guarantee yet. Huge but positive line numbers
remain untrusted until anchor validation. Orientation quality and coverage are not
proved by schema validity. Successful parsing neither approves a PR nor establishes
forge publication, and does not release a worker claim. Durable result storage,
queue routing/finalization, backend extraction, and a production executor remain
unimplemented. Tests use synthetic output and make no real reviewer calls.
