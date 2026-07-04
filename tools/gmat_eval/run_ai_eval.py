# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Honest evaluation of the GMAT AI features (Friday deliverable: LLM evals).

Two parts, both grounded in labelled data and reproducible from a committed
response cache (so the report regenerates offline, without a key):

  A. AI term grader (`gmat_ai.grade`) vs a human-reviewed gold set of typed
     recall answers -> verdict accuracy, false-pass/false-fail, rating agreement.
  B. AI peer features (`peer_flawed_solution`, `critique_check`, `peer_explain`)
     vs their contracts, using real GMAT MCQs + a labelled critique set.

The AI functions live in qt/aqt/gmat_ai.py (pure stdlib). We load that file
directly (NOT `import aqt.gmat_ai`, which would drag in Qt via aqt/__init__).

Run (populate/refresh the cache — needs OPENAI_API_KEY):

    OPENAI_API_KEY=... PYTHONPATH=out/pylib out/pyenv/bin/python \
        tools/gmat_eval/run_ai_eval.py --refresh

Re-run offline (from the committed cache, no key needed):

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/gmat_eval/run_ai_eval.py

Writes a markdown report to docs/gmat/AI-EVAL-RESULTS.md.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import hashlib
import importlib.util
import json
import os
import random
import sys
import urllib.request

# --- config (change here; report records these) ---------------------------
SEED = 20260701
FLAWED_SAMPLE = 40  # MCQs drawn for the peer_flawed_solution contract test
EXPLAIN_SAMPLE = 15  # MCQs drawn for the peer_explain sanity check
VERDICTS = ("correct", "partial", "incorrect")

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.normpath(os.path.join(_HERE, "..", ".."))
GMAT_AI_PATH = os.path.join(_ROOT, "qt", "aqt", "gmat_ai.py")
CACHE_PATH = os.path.join(_HERE, "ai_eval_cache.json")
TERM_CSV = os.path.join(_ROOT, "data", "gmat", "term_grading_eval.csv")
CRITIQUE_CSV = os.path.join(_ROOT, "data", "gmat", "peer_critique_eval.csv")
MCQ_CSV = os.path.join(_ROOT, "data", "gmat", "gmat_prep_mcq.csv")
REPORT = os.path.join(_ROOT, "docs", "gmat", "AI-EVAL-RESULTS.md")


def load_gmat_ai():
    """Load qt/aqt/gmat_ai.py as a standalone module (no aqt/Qt import)."""
    spec = importlib.util.spec_from_file_location("gmat_ai_standalone", GMAT_AI_PATH)
    mod = importlib.util.module_from_spec(spec)
    # Register before exec so @dataclass can resolve the module by name.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- response cache (keyed by the request body hash) ----------------------
class _Replay:
    """Minimal stand-in for the urlopen context manager."""

    def __init__(self, data: bytes):
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def install_cache(refresh: bool) -> tuple[dict, list[int]]:
    """Patch urllib so OpenAI calls are served from / recorded to a JSON cache.
    Returns (cache, miss_counter) where miss_counter[0] counts live API calls."""
    cache: dict = {}
    if os.path.exists(CACHE_PATH):
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
    misses = [0]
    real_urlopen = urllib.request.urlopen

    def patched(req, timeout=None):
        body = req.data or b""
        key = hashlib.sha256(body).hexdigest()
        if not refresh and key in cache:
            return _Replay(cache[key].encode("utf-8"))
        resp = real_urlopen(req, timeout=timeout)
        data = resp.read()
        cache[key] = data.decode("utf-8")
        misses[0] += 1
        return _Replay(data)

    urllib.request.urlopen = patched  # type: ignore[assignment]
    return cache, misses


def save_cache(cache: dict) -> None:
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, indent=0, sort_keys=True)


# --- data loading ----------------------------------------------------------
def read_csv(path: str) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return list(csv.DictReader(f))


def parse_gmat_prep(path: str) -> list[dict]:
    """Anki text-import file: skip '#' directives, positional 9 columns."""
    with open(path, encoding="utf-8") as f:
        data_lines = [ln for ln in f if not ln.startswith("#")]
    rows = []
    for cols in csv.reader(data_lines):
        if len(cols) < 8:
            continue
        rows.append(
            {
                "Question": cols[0],
                "A": cols[1],
                "B": cols[2],
                "C": cols[3],
                "D": cols[4],
                "E": cols[5],
                "Answer": cols[6].strip(),
                "Explanation": cols[7],
            }
        )
    return rows


def options_of(row: dict) -> list[tuple[str, str]]:
    return [(x, row[x]) for x in ("A", "B", "C", "D", "E") if row.get(x, "").strip()]


def pct(n: int, d: int) -> str:
    return f"{(100.0 * n / d):.1f}%" if d else "n/a"


# --- Part A: term grader ---------------------------------------------------
def eval_term_grader(gmat_ai) -> list[str]:
    rows = read_csv(TERM_CSV)
    ease = gmat_ai._EASE
    confusion = {g: {p: 0 for p in VERDICTS} for g in VERDICTS}
    verdict_hits = 0
    rating_exact = 0
    rating_within1 = 0
    false_pass = fp_total = 0  # gold != correct but graded correct
    false_fail = ff_total = 0  # gold == correct but graded not-correct
    by_band: dict[str, list[int]] = {}
    skipped = 0
    n = 0

    for r in rows:
        res = gmat_ai.grade(r["term"], r["expected_definition"], r["student_answer"])
        if res is None:
            skipped += 1
            continue
        n += 1
        gv, gr = r["gold_verdict"], r["gold_rating"]
        if gv in confusion and res.verdict in confusion[gv]:
            confusion[gv][res.verdict] += 1
        ok = res.verdict == gv
        verdict_hits += ok
        by_band.setdefault(r["band"], []).append(1 if ok else 0)
        rating_exact += res.rating == gr
        rating_within1 += abs(ease.get(res.rating, 3) - ease.get(gr, 3)) <= 1
        gold_pass = gv == "correct"
        if not gold_pass:
            fp_total += 1
            false_pass += res.correct
        else:
            ff_total += 1
            false_fail += not res.correct

    w: list[str] = []
    w.append("## Part A — AI term grader\n")
    w.append(
        f"Graded {n} human-labelled typed-recall answers "
        f"({skipped} skipped: no cached/available response).\n"
    )
    w.append(f"- **Verdict accuracy (3-class):** {pct(verdict_hits, n)}")
    w.append(
        f"- **False-pass** (gold not-correct → graded correct): "
        f"{pct(false_pass, fp_total)} ({false_pass}/{fp_total})"
    )
    w.append(
        f"- **False-fail** (gold correct → graded not-correct): "
        f"{pct(false_fail, ff_total)} ({false_fail}/{ff_total})"
    )
    w.append(f"- **Rating exact match:** {pct(rating_exact, n)}")
    w.append(f"- **Rating within 1 step:** {pct(rating_within1, n)}\n")

    w.append("Confusion matrix (rows = gold, cols = AI):\n")
    w.append("| gold ＼ AI | correct | partial | incorrect |")
    w.append("|---|---|---|---|")
    for g in VERDICTS:
        c = confusion[g]
        w.append(f"| **{g}** | {c['correct']} | {c['partial']} | {c['incorrect']} |")
    w.append("")

    w.append("Accuracy by answer band:\n")
    w.append("| band | verdict accuracy | n |")
    w.append("|---|---|---|")
    for band in sorted(by_band):
        hits = by_band[band]
        w.append(f"| {band} | {pct(sum(hits), len(hits))} | {len(hits)} |")
    w.append("")
    return w


# --- Part B: peer features -------------------------------------------------
def eval_peer(gmat_ai) -> list[str]:
    rng = random.Random(SEED)
    mcqs = [
        r for r in parse_gmat_prep(MCQ_CSV) if r["Question"].strip() and options_of(r)
    ]

    # B1: peer_flawed_solution must pick a WRONG option with real reasoning.
    flawed_rows = rng.sample(mcqs, min(FLAWED_SAMPLE, len(mcqs)))
    avail = valid_letter = wrong_option = has_reason = contract = 0
    for r in flawed_rows:
        opts = options_of(r)
        letters = {l for l, _ in opts}
        res = gmat_ai.peer_flawed_solution(
            r["Question"], opts, r["Answer"], r["Explanation"]
        )
        if res is None:
            continue
        avail += 1
        vl = res.choice in letters
        wo = res.choice != r["Answer"].upper()
        hr = bool(res.reasoning.strip())
        valid_letter += vl
        wrong_option += wo
        has_reason += hr
        contract += vl and wo and hr
    nf = len(flawed_rows)

    # B2: critique_check must accept substantive critiques, reject bare ones.
    crit_rows = read_csv(CRITIQUE_CSV)
    c_correct = c_skip = 0
    accept_good = good_total = reject_bare = bare_total = 0
    for r in crit_rows:
        gold = r["gold_found_flaw"].strip().lower() == "true"
        res = gmat_ai.critique_check(
            r["question"],
            r["correct"],
            r["explanation"],
            r["flawed_reasoning"],
            r["critique"],
        )
        if res is None:
            c_skip += 1
            continue
        c_correct += res.found_flaw == gold
        if gold:
            good_total += 1
            accept_good += res.found_flaw
        else:
            bare_total += 1
            reject_bare += not res.found_flaw
    nc = len(crit_rows) - c_skip

    # B3: peer_explain sanity (availability; groundedness NOT auto-validated).
    explain_rows = rng.sample(mcqs, min(EXPLAIN_SAMPLE, len(mcqs)))
    e_avail = e_nonempty = 0
    for r in explain_rows:
        opts = options_of(r)
        wrong = next((l for l, _ in opts if l != r["Answer"].upper()), "A")
        res = gmat_ai.peer_explain(
            r["Question"], opts, r["Answer"], wrong, r["Explanation"]
        )
        if res is None:
            continue
        e_avail += 1
        e_nonempty += bool(res.text.strip())
    ne = len(explain_rows)

    w: list[str] = []
    w.append("## Part B — AI peer features\n")
    w.append(f"### peer_flawed_solution (n={nf} real GMAT MCQs)\n")
    w.append(f"- Available (non-null): {pct(avail, nf)}")
    w.append(f"- Valid option letter: {pct(valid_letter, nf)}")
    w.append(f"- **Picked a WRONG option (core contract): {pct(wrong_option, nf)}**")
    w.append(f"- Non-empty reasoning: {pct(has_reason, nf)}")
    w.append(f"- Full contract honored: {pct(contract, nf)}\n")

    w.append(f"### critique_check (n={nc} labelled critiques)\n")
    w.append(f"- Overall accuracy: {pct(c_correct, nc)}")
    w.append(
        f"- **Accepts substantive critiques:** {pct(accept_good, good_total)} "
        f"({accept_good}/{good_total})"
    )
    w.append(
        f"- **Rejects bare-answer critiques:** {pct(reject_bare, bare_total)} "
        f"({reject_bare}/{bare_total})\n"
    )

    w.append(f"### peer_explain (n={ne} MCQs, sanity only)\n")
    w.append(f"- Available (non-null): {pct(e_avail, ne)}")
    w.append(f"- Non-empty guidance: {pct(e_nonempty, ne)}")
    w.append(
        "- Groundedness (does the guidance faithfully match the reference "
        "explanation) is **not** auto-validated here — see honesty notes.\n"
    )
    return w


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--refresh", action="store_true", help="call the API and rewrite the cache"
    )
    args = ap.parse_args()

    gmat_ai = load_gmat_ai()
    cache, misses = install_cache(args.refresh)

    have_key = bool(gmat_ai.api_key())
    if args.refresh and not have_key:
        sys.stderr.write("error: --refresh needs OPENAI_API_KEY set.\n")
        sys.exit(1)
    if not have_key:
        # No real key: force the graders past their key check so requests are
        # served from the cache. Cache misses will fail (network) -> counted as skips.
        os.environ["OPENAI_API_KEY"] = "cache-only-no-network"

    lines: list[str] = []
    lines.append("# GMAT AI features — evaluation results\n")
    lines.append(
        "_Generated by `tools/gmat_eval/run_ai_eval.py`. Reproducible from a "
        "committed response cache (temperature 0); re-run offline reproduces this "
        "report. Ground truth is a small human-reviewed labelled set, not real "
        "student data or exam outcomes._\n"
    )
    lines.append(
        f"Config: model=`{gmat_ai.model()}`, seed={SEED}, "
        f"generated={datetime.date.today().isoformat()}.\n"
    )

    lines += eval_term_grader(gmat_ai)
    lines += eval_peer(gmat_ai)

    lines.append("## Honesty notes\n")
    lines.append(
        "- The gold labels are **human judgments** on a small, hand-authored set; "
        "the correct/partial and easy/good boundaries are subjective at the margin."
    )
    lines.append(
        "- Grader inputs are representative GMAT terms with authored student answers, "
        "**not** a real distribution of student responses."
    )
    lines.append(
        "- Peer MCQs are real GMAT-prep items; `peer_explain` groundedness is only "
        "sanity-checked (availability), not verified against the reference rationale."
    )
    lines.append(
        f"- `{gmat_ai.model()}` at temperature 0; responses are cached so the numbers "
        "are pinned. Nothing here is validated against real GMAT exam outcomes.\n"
    )

    report = "\n".join(lines) + "\n"
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w", encoding="utf-8") as f:
        f.write(report)
    if args.refresh or misses[0]:
        save_cache(cache)
    sys.stdout.write(report)
    sys.stderr.write(f"\n[wrote {REPORT}; live API calls: {misses[0]}]\n")


if __name__ == "__main__":
    main()
