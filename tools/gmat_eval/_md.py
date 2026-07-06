# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Shared Markdown post-processor for the GMAT eval report generators.

`format_md(text)` reproduces the subset of dprint's Markdown formatting the
generated reports rely on, so a freshly-generated doc passes `dprint check`
(and therefore `just check`) without a manual re-format:

  - column-aligned tables (cells left-justified; separator dashes = column width)
  - single-asterisk emphasis `*x*` -> `_x_` (leaves `**bold**` untouched)
  - a blank line before every heading
  - no runs of 3+ blank lines; exactly one trailing newline

Import from a sibling generator (they run as `python tools/gmat_eval/<x>.py`, so
this module's directory is on sys.path): `from _md import format_md`.
"""

from __future__ import annotations

import re
import unicodedata


def _dw(s: str) -> int:
    """Display width: combining marks (e.g. the hat in θ̂) are zero-width, matching
    how dprint measures Markdown table columns."""
    return sum(0 if unicodedata.combining(ch) else 1 for ch in s)


def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - _dw(s))


_HEADING = re.compile(r"([^\n])\n(#{1,6} )")
_MULTINEWLINE = re.compile(r"\n{3,}")
# `*emphasis*` with no space just inside the markers and not part of `**bold**`.
_EMPHASIS = re.compile(r"(?<!\*)\*(?!\s)([^*\n]+?)(?<!\s)\*(?!\*)")


def _is_row(line: str) -> bool:
    return line.lstrip().startswith("|")


def _is_separator(line: str) -> bool:
    s = line.strip()
    return bool(s) and set(s) <= set("|-: ") and "-" in s


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _align_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        # A table = a row line, then a separator line, then zero+ row lines.
        if i + 1 < n and _is_row(lines[i]) and _is_separator(lines[i + 1]):
            header = _cells(lines[i])
            body_start = i + 2
            j = body_start
            while j < n and _is_row(lines[j]) and not _is_separator(lines[j]):
                j += 1
            rows = [_cells(lines[k]) for k in range(body_start, j)]
            ncol = len(header)
            widths = [_dw(h) for h in header]
            for r in rows:
                for c in range(min(ncol, len(r))):
                    widths[c] = max(widths[c], _dw(r[c]))

            def fmt(cells: list[str]) -> str:
                padded = [
                    _pad(cells[c] if c < len(cells) else "", widths[c])
                    for c in range(ncol)
                ]
                return "| " + " | ".join(padded) + " |"

            out.append(fmt(header))
            out.append("| " + " | ".join("-" * w for w in widths) + " |")
            out.extend(fmt(r) for r in rows)
            i = j
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def format_md(text: str) -> str:
    text = _EMPHASIS.sub(r"_\1_", text)
    text = _align_tables(text)
    text = _HEADING.sub(r"\1\n\n\2", text)
    text = _MULTINEWLINE.sub("\n\n", text)
    return text.strip("\n") + "\n"
