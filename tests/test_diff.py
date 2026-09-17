"""Offline side-aware anchoring; malformed inputs never become inline findings."""

from dataclasses import replace

import pytest

from diffbutler.domain.diff import DiffAnchor, DiffIndex, InvalidDiff
from diffbutler.domain.review import Side

DIFF = """diff --git a/file.py b/file.py
index abc..def 100644
--- a/file.py
+++ b/file.py
@@ -10,3 +10,4 @@ function
 context
-old
+new
+extra
 end
@@ -30 +31 @@
-old
+new
"""


def anchor(line: int, side: Side = Side.HEAD, start: int | None = None) -> DiffAnchor:
    return DiffAnchor("file.py", line, side, start)


def test_side_context_and_single_hunk_ranges() -> None:
    index = DiffIndex.parse(DIFF, base_sha="base", head_sha="head")
    for side in Side:
        assert index.is_commentable(anchor(10, side))
        assert index.is_commentable(anchor(12, side, 10))
    assert index.is_commentable(anchor(13))
    assert not index.is_commentable(anchor(13, Side.BASE))
    assert not index.is_commentable(anchor(31, start=10))
    assert not index.is_commentable(anchor(30))
    assert not index.is_commentable(replace(anchor(10), head_sha="stale"))
    exact = index.validate(anchor(12))
    assert exact.status == "exact" and exact.anchor is not None
    assert exact.anchor.head_sha == "head" and exact.anchor.old_path == "file.py"
    assert exact.original == anchor(12)


def test_bounded_snapping_preserves_original_and_demotes_without_guessing() -> None:
    index = DiffIndex.parse(DIFF)
    result = index.validate(anchor(15, start=9))
    assert result.status == "snapped" and result.anchor is not None
    assert (result.anchor.start_line, result.anchor.line) == (10, 13)
    assert result.original == anchor(15, start=9)
    for invalid in (anchor(15), anchor(10, start=1), anchor(31, start=13), anchor(10**30)):
        result = index.validate(invalid)
        assert result.status == "demoted" and result.anchor is None
        assert result.original == invalid
    assert index.snap(anchor(15, start=9), window=1) is None
    with pytest.raises(ValueError):
        index.snap(anchor(10), window=-1)


@pytest.mark.parametrize("added", [False, True])
def test_additions_and_deletions_have_only_one_side(added: bool) -> None:
    paths = "--- /dev/null\n+++ b/file.py" if added else "--- a/file.py\n+++ /dev/null"
    body = "@@ -0,0 +1 @@\n+new\n" if added else "@@ -1 +0,0 @@\n-old\n"
    index = DiffIndex.parse("diff --git a/file.py b/file.py\n" + paths + "\n" + body)
    assert index.is_commentable(anchor(1, Side.HEAD if added else Side.BASE))
    assert not index.is_commentable(anchor(1, Side.BASE if added else Side.HEAD))


def test_rename_paths_quoted_utf8_no_newline_and_mode_only() -> None:
    index = DiffIndex.parse(
        "diff --git a/old b/new\nsimilarity index 50%\nrename from old\nrename to new\n"
        '--- a/old\n+++ "b/caf\\303\\251"\n@@ -1 +1 @@\n-old\n'
        "\\ No newline at end of file\n+new\n\\ No newline at end of file\n"
    )
    result = index.validate(DiffAnchor("café", 1, Side.BASE))
    assert result.anchor is not None and result.anchor.old_path == "old"
    assert not index.is_commentable(DiffAnchor("old", 1, Side.BASE))
    assert not DiffIndex.parse("diff --git a/a b/a\nold mode 100644\nnew mode 100755\n").hunks
    assert not DiffIndex.parse("").hunks


@pytest.mark.parametrize(
    "diff",
    [
        "junk",
        DIFF.replace("-10,3", "-10,4"),
        DIFF.replace("+10,4", "+10,3"),
        DIFF.replace("@@ -10,3", "@@ -0,3"),
        DIFF + "+extra\n",
        DIFF + "unexpected\n",
        DIFF.replace("+++ b/file.py", "+++ b/../file.py"),
        DIFF.replace("+++ b/file.py", "+++ /dev/null"),
        DIFF.replace("@@ -30 +31 @@", "@@ -11 +11 @@"),
        DIFF + DIFF,
        "diff --cc file.py\n",
        "diff --git a/a b/a\nBinary files a/a and b/a differ\n",
        DIFF.replace(" context", "\x00context"),
        DIFF.replace("+++ b/file.py", '+++ "b/\\q"'),
    ],
)
def test_malformed_or_unsupported_diffs_fail_closed(diff: str) -> None:
    with pytest.raises(InvalidDiff, match="Malformed or unsupported"):
        DiffIndex.parse(diff)
