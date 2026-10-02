"""Unified-diff helpers: filtering, line annotation and chunking."""

from __future__ import annotations

import re
from pathlib import PurePosixPath

_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")

_SKIP_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg", ".pdf", ".zip", ".gz", ".tar", ".7z",
    ".jar", ".class", ".exe", ".dll", ".so", ".dylib", ".woff", ".woff2", ".ttf", ".eot", ".mp3",
    ".mp4", ".mov", ".bin", ".pyc", ".lock", ".map",
}
_SKIP_NAMES = {
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "pipfile.lock", "cargo.lock",
    "go.sum", "composer.lock", "gemfile.lock",
}
# Only clearly vendored/generated directories. Not "build": many repos keep real build scripts there.
_SKIP_DIRS = {"node_modules", "vendor", "dist", ".next", "__pycache__", "venv", ".venv", "third_party"}


def should_review(filename: str) -> bool:
    """Skip generated, vendored, binary and lock files that are not useful to review."""
    path = PurePosixPath(filename.replace("\\", "/"))
    name = path.name.lower()
    if name in _SKIP_NAMES or name.endswith((".min.js", ".min.css")):
        return False
    if path.suffix.lower() in _SKIP_SUFFIXES:
        return False
    return not any(part in _SKIP_DIRS for part in path.parts[:-1])


def annotate_patch(patch: str) -> tuple[str, set[int]]:
    """Prefix each added/context line with its new-file line number.

    Returns the annotated text and the set of new-file line numbers that were added
    (the only lines on which a GitHub review comment can be anchored reliably).
    """
    output: list[str] = []
    added: set[int] = set()
    new_line: int | None = None
    for raw in patch.splitlines():
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            output.append(raw)
            continue
        if new_line is None or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            output.append(f"{new_line:>5} + {raw[1:]}")
            added.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            output.append(f"      - {raw[1:]}")
        else:
            output.append(f"{new_line:>5}   {raw[1:]}")
            new_line += 1
    return "\n".join(output), added


def changed_new_lines(patch: str) -> tuple[set[int], set[tuple[int, int]]]:
    """(added new-file lines, deletion gaps).

    A gap ``(a, b)`` says text was removed between new-file lines ``a`` and ``b``. A removed parameter line leaves no
    added line, so gaps are how a change *inside* a signature (both neighbours in the signature) is told apart from a
    deletion right after it (for example a removed docstring).
    """
    added: set[int] = set()
    gaps: set[tuple[int, int]] = set()
    new_line: int | None = None
    for raw in patch.splitlines():
        hunk = _HUNK_RE.match(raw)
        if hunk:
            new_line = int(hunk.group(1))
            continue
        if new_line is None or raw.startswith("\\"):
            continue
        if raw.startswith("+"):
            added.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            gaps.add((max(0, new_line - 1), new_line))
        else:
            new_line += 1
    return added, gaps


def trim_patch(patch: str, max_chars: int) -> tuple[str, int]:
    """Fit a patch into max_chars by whole hunks, keeping hunks from BOTH ends (risk is not only at the top).

    Returns (patch, number of omitted hunks). A single oversized hunk is cut at a line boundary.
    """
    if len(patch) <= max_chars:
        return patch, 0
    hunks: list[list[str]] = []
    for raw in patch.splitlines():
        if _HUNK_RE.match(raw) or not hunks:
            hunks.append([raw])
        else:
            hunks[-1].append(raw)
    texts = ["\n".join(lines) for lines in hunks]
    keep: set[int] = set()
    used = 0
    lo, hi = 0, len(texts) - 1
    take_front = True
    while lo <= hi:
        index = lo if take_front else hi
        cost = len(texts[index]) + 1
        if used + cost > max_chars:
            if not keep:  # even the first hunk does not fit: cut it
                texts[index] = texts[index][:max_chars].rsplit("\n", 1)[0]
                keep.add(index)
            break
        keep.add(index)
        used += cost
        if take_front:
            lo += 1
        else:
            hi -= 1
        take_front = not take_front
    kept = [texts[i] for i in sorted(keep)]
    return "\n".join(kept), len(texts) - len(keep)


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split on line boundaries so each chunk stays under max_chars (single long lines are cut)."""
    if len(text) <= max_chars:
        return [text] if text else []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in text.splitlines():
        line = line[:max_chars]
        if size + len(line) + 1 > max_chars and current:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks
