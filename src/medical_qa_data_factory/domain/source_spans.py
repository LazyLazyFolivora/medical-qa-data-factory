"""Offsets for sentence-like units of original text."""

import re
from collections.abc import Iterator


def sentence_spans(text: str) -> Iterator[tuple[int, int]]:
    """Return exact non-blank slices without changing internal whitespace."""
    boundaries = [0, *(match.end() for match in re.finditer(r"(?<=[.!?])\s+", text)), len(text)]
    for start, end in zip(boundaries, boundaries[1:]):
        chunk = text[start:end]
        if chunk.strip():
            yield start + len(chunk) - len(chunk.lstrip()), start + len(chunk.rstrip())
