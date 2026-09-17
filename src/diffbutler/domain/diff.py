"""Forge-neutral anchors and validation outcomes; no source text is retained."""

import re
from dataclasses import dataclass, replace
from typing import Literal

from diffbutler.domain.review import Side, SourceFile


@dataclass(frozen=True)
class DiffAnchor:
    path: str
    line: int
    side: Side
    start_line: int | None = None
    old_path: str | None = None
    base_sha: str = ""
    head_sha: str = ""

    def __post_init__(self) -> None:
        SourceFile(self.path, self.side)
        if self.old_path is not None:
            SourceFile(self.old_path, Side.BASE)
        if self.line < 1 or (self.start_line is not None and not 1 <= self.start_line <= self.line):
            raise ValueError("anchor lines must be positive and ordered")


@dataclass(frozen=True)
class AnchorValidation:
    original: DiffAnchor
    anchor: DiffAnchor | None
    status: Literal["exact", "snapped", "demoted"]


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?: .*)?")


class InvalidDiff(ValueError):
    def __init__(self) -> None:
        super().__init__("Malformed or unsupported unified diff")


def _path(value: str, prefix: str) -> str | None:
    if value == "/dev/null":
        return None
    # Git quotes tabs, newlines, non-ASCII bytes and backslashes using C escapes.
    if value.startswith('"'):
        if not value.endswith('"'):
            raise InvalidDiff()
        raw = bytearray()
        text = value[1:-1]
        offset = 0
        escapes = {"a": 7, "b": 8, "t": 9, "n": 10, "v": 11, "f": 12, "r": 13, '"': 34, "\\": 92}
        while offset < len(text):
            char = text[offset]
            offset += 1
            if char != "\\":
                raw.extend(char.encode("utf-8"))
                continue
            if offset == len(text):
                raise InvalidDiff()
            char = text[offset]
            if char in escapes:
                raw.append(escapes[char])
                offset += 1
            elif re.fullmatch(r"[0-3][0-7]{2}", text[offset : offset + 3]):
                raw.append(int(text[offset : offset + 3], 8))
                offset += 3
            else:
                raise InvalidDiff()
        try:
            value = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidDiff() from None
    else:
        value = value.removesuffix("\t")
    if not value.startswith(prefix):
        raise InvalidDiff()
    path = value[len(prefix) :]
    try:
        SourceFile(path, Side.HEAD)
    except ValueError:
        raise InvalidDiff() from None
    return path


@dataclass(frozen=True)
class _Hunk:
    path: str
    old_path: str | None
    base_start: int
    base_count: int
    head_start: int
    head_count: int

    def bounds(self, side: Side) -> tuple[int, int]:
        start, count = (
            (self.base_start, self.base_count)
            if side == Side.BASE
            else (self.head_start, self.head_count)
        )
        return start, start + count - 1


@dataclass(frozen=True)
class DiffIndex:
    """Index the original diff before redaction. No source text is stored.

    Paths use the head name, or the base name for a deletion. Context lines are
    commentable on both sides. Ranges must fit a single hunk on one side. Snapping
    clips a range overlapping exactly one hunk, within the endpoint window; it
    never guesses a hunk for a location wholly outside the diff or crosses sides.
    """

    hunks: tuple[_Hunk, ...]
    base_sha: str = ""
    head_sha: str = ""

    @classmethod
    def parse(cls, diff: str, *, base_sha: str = "", head_sha: str = "") -> DiffIndex:
        if "\x00" in diff:
            raise InvalidDiff()
        lines = diff.split("\n")
        if lines[-1] == "":
            lines.pop()
        hunks: list[_Hunk] = []
        old: str | None = None
        new: str | None = None
        in_file = headers = has_hunk = False
        seen: set[str] = set()
        offset = 0
        while offset < len(lines):
            line = lines[offset]
            offset += 1
            if line.startswith("diff --git "):
                if headers and not has_hunk:
                    raise InvalidDiff()
                in_file, headers, has_hunk = True, False, False
                old = new = None
            elif (
                line.startswith(
                    (
                        "index ",
                        "old mode ",
                        "new mode ",
                        "new file mode ",
                        "deleted file mode ",
                        "similarity index ",
                        "rename from ",
                        "rename to ",
                    )
                )
                and in_file
                and not headers
            ):
                continue
            elif line.startswith("--- ") and in_file and not headers:
                if offset >= len(lines) or not lines[offset].startswith("+++ "):
                    raise InvalidDiff()
                old = _path(line[4:], "a/")
                new = _path(lines[offset][4:], "b/")
                offset += 1
                if old is None and new is None:
                    raise InvalidDiff()
                path = new or old
                assert path is not None
                if path in seen:
                    raise InvalidDiff()
                seen.add(path)
                headers = True
            elif (match := _HUNK.fullmatch(line)) is not None and headers:
                bs, bc, hs, hc = (
                    int(match[1]),
                    int(match[2] or 1),
                    int(match[3]),
                    int(match[4] or 1),
                )
                if (bc and bs < 1) or (hc and hs < 1) or not bc + hc:
                    raise InvalidDiff()
                if (old is None and bc) or (new is None and hc):
                    raise InvalidDiff()
                path = new or old
                assert path is not None
                hunk = _Hunk(path, old, bs, bc, hs, hc)
                if hunks and hunks[-1].path == path:
                    prev = hunks[-1]
                    if (
                        bs < prev.base_start + prev.base_count
                        or hs < prev.head_start + prev.head_count
                    ):
                        raise InvalidDiff()
                b = h = 0
                marker_allowed = False
                while b < bc or h < hc:
                    if offset >= len(lines):
                        raise InvalidDiff()
                    body = lines[offset]
                    offset += 1
                    if body == "\\ No newline at end of file" and marker_allowed:
                        marker_allowed = False
                        continue
                    if not body or body[0] not in " +-":
                        raise InvalidDiff()
                    b += body[0] in " -"
                    h += body[0] in " +"
                    if b > bc or h > hc:
                        raise InvalidDiff()
                    marker_allowed = True
                if offset < len(lines) and lines[offset] == "\\ No newline at end of file":
                    offset += 1
                hunks.append(hunk)
                has_hunk = True
            else:
                raise InvalidDiff()
        if headers and not has_hunk:
            raise InvalidDiff()
        return cls(tuple(hunks), base_sha, head_sha)

    def _matching(self, anchor: DiffAnchor) -> tuple[_Hunk, ...]:
        if (anchor.base_sha and anchor.base_sha != self.base_sha) or (
            anchor.head_sha and anchor.head_sha != self.head_sha
        ):
            return ()
        return tuple(
            hunk
            for hunk in self.hunks
            if hunk.path == anchor.path
            and (anchor.old_path is None or anchor.old_path == hunk.old_path)
        )

    def is_commentable(self, anchor: DiffAnchor) -> bool:
        start = anchor.start_line or anchor.line
        return any(
            low <= start <= anchor.line <= high
            for low, high in (hunk.bounds(anchor.side) for hunk in self._matching(anchor))
        )

    def snap(self, anchor: DiffAnchor, *, window: int = 3) -> DiffAnchor | None:
        if window < 0:
            raise ValueError("snapping window must be nonnegative")
        start = anchor.start_line or anchor.line
        matches = [
            hunk
            for hunk in self._matching(anchor)
            if (bounds := hunk.bounds(anchor.side))[0] <= bounds[1]
            and start <= bounds[1]
            and anchor.line >= bounds[0]
        ]
        if len(matches) != 1:
            return None
        hunk = matches[0]
        low, high = hunk.bounds(anchor.side)
        first, last = max(start, low), min(anchor.line, high)
        if first - start > window or anchor.line - last > window:
            return None
        return replace(
            anchor,
            line=last,
            start_line=first if anchor.start_line else None,
            old_path=hunk.old_path,
            base_sha=self.base_sha,
            head_sha=self.head_sha,
        )

    def validate(self, anchor: DiffAnchor, *, window: int = 3) -> AnchorValidation:
        snapped = self.snap(anchor, window=window)
        status = (
            "demoted"
            if snapped is None
            else ("exact" if self.is_commentable(anchor) else "snapped")
        )
        return AnchorValidation(anchor, snapped, status)
