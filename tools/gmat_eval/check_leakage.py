# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Real data-leakage scan (spec §7e).

Context / honest framing: this fork trains **no** model on its data. The memory
model is FSRS (pretrained upstream); the performance model's IRT item parameters
are *assumed*, not fitted (see docs/gmat/EVAL-RESULTS.md); the AI grader is an
off-the-shelf `gpt-4o-mini` scored against a hand-labelled gold set. So the
classic "test row leaked into the training set" failure cannot occur — there is
no training set.

What *would* still matter is adjacency: if an item used to **evaluate** the AI
(the hand-labelled gold sets) also appears in the **practice** content a student
is exposed to, the evaluation would no longer be independent of the deck. This
script checks exactly that, for real, over the actual banks — exact-normalised
matches AND near-duplicates (token Jaccard) — and exits non-zero if any gold
item leaks into the practice corpora.

Method (scales to the 549k-row Quant bank): the small gold sets are indexed by
token; the large practice banks are streamed once, and only rows sharing tokens
with a gold item are compared. No external dependencies.

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/gmat_eval/check_leakage.py

Writes docs/gmat/LEAKAGE-CHECK.md; exit code 1 if any gold<->practice overlap.
"""

from __future__ import annotations

import csv
import hashlib
import html
import os
import re
import sys
from collections import defaultdict
from typing import Any

from _md import format_md  # type: ignore[import-not-found]

csv.field_size_limit(10_000_000)  # the AQuA bank has long rationale fields

DATA = os.path.join("data", "gmat")
REPORT = os.path.join("docs", "gmat", "LEAKAGE-CHECK.md")

# Near-duplicate threshold on token-set Jaccard. 1.0 = identical token bag;
# 0.85 catches light rewording (a few words changed) without flagging merely
# same-topic items.
NEAR_DUP_JACCARD = 0.85
# Only tokens of length >= 2 count, and tokens appearing in more than this many
# distinct gold items are treated as too common to block on (avoid O(n^2)).
MIN_TOKEN_LEN = 2
MAX_GOLD_DF = 40
# Near-duplicate Jaccard is only meaningful for prompts with enough distinct
# tokens; short arithmetic prompts ("what is 15 percent of 68") collapse to a
# handful of tokens and would trivially match a 549k-row bank. Below this size we
# rely on exact-hash matching only. (Exact matches are always reported.)
MIN_TOKENS_FOR_NEAR = 8

_TAG_RE = re.compile(r"<[^>]+>")
_NONWORD_RE = re.compile(r"[^a-z0-9\s]+")
_WS_RE = re.compile(r"\s+")


def normalize(text: str) -> str:
    """Lowercase, unescape HTML entities, strip tags/punctuation, collapse ws."""
    text = html.unescape(text or "")
    text = _TAG_RE.sub(" ", text)
    text = text.lower()
    text = _NONWORD_RE.sub(" ", text)
    return _WS_RE.sub(" ", text).strip()


def tokens(norm: str) -> set[str]:
    return {t for t in norm.split(" ") if len(t) >= MIN_TOKEN_LEN}


def norm_hash(norm: str) -> str:
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()


class GoldSet:
    """A small evaluation/gold corpus, held in memory and token-indexed."""

    def __init__(self, name: str):
        self.name = name
        self.norms: list[str] = []
        self.hashes: list[str] = []
        self.tok: list[set[str]] = []
        self.raw: list[str] = []

    def add(self, text: str) -> None:
        n = normalize(text)
        if not n:
            return
        self.norms.append(n)
        self.hashes.append(norm_hash(n))
        self.tok.append(tokens(n))
        self.raw.append(text)

    def build_index(self) -> None:
        self.hashset = set(self.hashes)
        self.index: dict[str, list[int]] = defaultdict(list)
        df: dict[str, int] = defaultdict(int)
        for tset in self.tok:
            for t in tset:
                df[t] += 1
        for i, tset in enumerate(self.tok):
            for t in tset:
                if df[t] <= MAX_GOLD_DF:
                    self.index[t].append(i)

    def __len__(self) -> int:
        return len(self.norms)


def load_gold(path: str, fields: list[str], name: str) -> GoldSet:
    gs = GoldSet(name)
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            gs.add(" ".join(row.get(fld, "") or "" for fld in fields))
    gs.build_index()
    return gs


def iter_practice_questions(path: str, is_ankiimport: bool):
    """Yield the Question text of each practice row.

    is_ankiimport=True: gmat_prep_mcq.csv (leading #directives, no header,
    Question is column 0). Else: aqua_mcq.csv (has a header, Question column).
    """
    with open(path, newline="", encoding="utf-8") as f:
        if is_ankiimport:
            for row in csv.reader(f):
                if not row or row[0].startswith("#"):
                    continue
                yield row[0]
        else:
            for drow in csv.DictReader(f):
                yield drow.get("Question", "")


def scan(
    gold_sets: list[GoldSet],
    practice_path: str,
    practice_name: str,
    is_ankiimport: bool,
):
    """Stream a practice bank once; return per-gold {exact, near, rows, examples}."""
    results: dict[str, dict[str, Any]] = {
        gs.name: {"exact": 0, "near": 0, "examples": []} for gs in gold_sets
    }
    rows = 0
    seen_hashes: dict[str, int] = defaultdict(int)  # intra-bank dup counter
    for q in iter_practice_questions(practice_path, is_ankiimport):
        rows += 1
        n = normalize(q)
        if not n:
            continue
        h = norm_hash(n)
        seen_hashes[h] += 1
        qtok = tokens(n)
        for gs in gold_sets:
            # exact
            if h in gs.hashset:
                results[gs.name]["exact"] += 1
                if len(results[gs.name]["examples"]) < 3:
                    results[gs.name]["examples"].append(("exact", q[:120]))
                continue
            # near-dup via token blocking (only for prompts long enough that
            # Jaccard is meaningful — see MIN_TOKENS_FOR_NEAR)
            if len(qtok) < MIN_TOKENS_FOR_NEAR:
                continue
            cand: set[int] = set()
            for t in qtok:
                cand.update(gs.index.get(t, ()))
            best = 0.0
            for i in cand:
                gt = gs.tok[i]
                if len(gt) < MIN_TOKENS_FOR_NEAR:
                    continue
                inter = len(qtok & gt)
                if inter == 0:
                    continue
                jac = inter / len(qtok | gt)
                best = max(best, jac)
            if best >= NEAR_DUP_JACCARD:
                results[gs.name]["near"] += 1
                if len(results[gs.name]["examples"]) < 3:
                    results[gs.name]["examples"].append((f"near({best:.2f})", q[:120]))
    intra_dups = sum(c - 1 for c in seen_hashes.values() if c > 1)
    return results, rows, intra_dups


def main() -> int:
    term_gold = load_gold(
        os.path.join(DATA, "term_grading_eval.csv"),
        ["term", "expected_definition"],
        "term-grader gold",
    )
    critique_gold = load_gold(
        os.path.join(DATA, "peer_critique_eval.csv"),
        ["question"],
        "peer-critique gold",
    )
    gold_sets = [term_gold, critique_gold]

    banks = [
        ("gmat_prep_mcq.csv (practice: GMAT-prep MCQs)", "gmat_prep_mcq.csv", True),
        ("aqua_mcq.csv (practice: AQuA Quant bank)", "aqua_mcq.csv", False),
    ]

    lines: list[str] = []
    w = lines.append
    w("# GMAT data-leakage scan (spec §7e)\n")
    w(
        "_Generated by `tools/gmat_eval/check_leakage.py`. No model is trained on this "
        "data (FSRS is pretrained; IRT item parameters are assumed, not fitted; the AI "
        "grader is off-the-shelf scored against a gold set), so classic train/test "
        "contamination cannot occur. This scan verifies the stronger, still-relevant "
        "property: the hand-labelled **evaluation gold sets** do not appear (exactly or "
        "as near-duplicates) in the **practice** banks a student is exposed to._\n"
    )
    w(
        f"Gold sets scanned: **{len(term_gold)}** term-grader items, "
        f"**{len(critique_gold)}** peer-critique items. "
        f"Near-duplicate threshold: token-set Jaccard ≥ {NEAR_DUP_JACCARD:.2f}.\n"
    )

    total_leaks = 0
    for pretty, fname, is_import in banks:
        path = os.path.join(DATA, fname)
        if not os.path.exists(path):
            w(f"## {pretty}\n\n_Not present (gitignored); skipped._\n")
            continue
        results, rows, intra = scan(gold_sets, path, pretty, is_import)
        w(f"## {pretty}\n")
        w(f"Rows scanned: **{rows:,}**. Intra-bank exact duplicate rows: {intra:,}.\n")
        w("| Gold set | Exact matches | Near-duplicates |")
        w("|---|---|---|")
        for gs in gold_sets:
            r = results[gs.name]
            total_leaks += r["exact"] + r["near"]
            w(f"| {gs.name} | {r['exact']} | {r['near']} |")
        w("")
        for gs in gold_sets:
            for kind, ex in results[gs.name]["examples"]:
                w(f"  - LEAK [{kind}] vs {gs.name}: `{ex}…`")
        if any(results[gs.name]["examples"] for gs in gold_sets):
            w("")

    verdict = (
        "✅ CLEAN — no gold item leaks into the practice banks."
        if total_leaks == 0
        else f"❌ LEAKAGE — {total_leaks} gold/practice overlap(s) found."
    )
    w("## Verdict\n")
    w(f"**{verdict}**\n")

    report = format_md("\n".join(lines))
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write(report)
    sys.stdout.write(report)
    sys.stderr.write(f"\n[wrote {REPORT}]\n")
    return 1 if total_leaks else 0


if __name__ == "__main__":
    sys.exit(main())
