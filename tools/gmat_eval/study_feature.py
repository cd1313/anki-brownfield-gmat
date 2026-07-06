# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Score the real peer-to-peer study-feature ablation (spec §8).

Reads `data/gmat/study_results.csv` (one row per participant × arm) and writes the
Results section of `docs/gmat/STUDY-FEATURE.md`: per-arm accuracy + 95% Wilson CIs,
the pre-registered peer-ON − peer-OFF contrast (per participant + mean), and honest
small-N caveats. If the CSV has no data rows yet it emits "data collection pending"
but still prints the pre-registered design + protocol, so the doc is valid before
the study is run.

CSV schema (header required):
    participant,arm,correct,total
where arm is one of: peer_on | peer_off | plain

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/gmat_eval/study_feature.py

(No Anki import needed; pure stdlib.)
"""

from __future__ import annotations

import csv
import math
import os
import re
import statistics
import sys

DATA = os.path.join("data", "gmat", "study_results.csv")
REPORT = os.path.join("docs", "gmat", "STUDY-FEATURE.md")
ARMS = ["peer_on", "peer_off", "plain"]
LABEL = {"peer_on": "Peer ON", "peer_off": "Peer OFF (ablation)", "plain": "Plain Anki"}


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * math.sqrt(phat * (1 - phat) / n + z * z / (4 * n * n))) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _normalize_md(text: str) -> str:
    """Keep generated Markdown dprint-clean: a blank line before every heading,
    no runs of blank lines, single trailing newline."""
    text = re.sub(
        r"([^\n])\n(#{1,6} )", r"\1\n\n\2", text
    )  # blank line before headings
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse extra blanks
    return text.rstrip("\n") + "\n"


def md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    """Emit a column-aligned Markdown table (matches dprint's formatting, so the
    generated doc stays check-clean without a re-format)."""
    widths = [len(h) for h in headers]
    for r in rows:
        for i, c in enumerate(r):
            widths[i] = max(widths[i], len(c))

    def line(cells: list[str]) -> str:
        return "| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    sep = "| " + " | ".join("-" * w for w in widths) + " |"
    return [line(headers), sep, *[line(r) for r in rows]]


def load_rows() -> list[dict]:
    if not os.path.exists(DATA):
        return []
    rows = []
    with open(DATA, newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            part = (r.get("participant") or "").strip()
            if not r.get("arm") or part.startswith("#"):
                continue  # blank or commented (example) row
            arm = r["arm"].strip()
            if arm not in ARMS:
                continue
            try:
                correct = int(r["correct"])
                total = int(r["total"])
            except (TypeError, ValueError):
                continue
            if total <= 0:
                continue
            rows.append(
                {
                    "participant": r["participant"].strip(),
                    "arm": arm,
                    "correct": correct,
                    "total": total,
                }
            )
    return rows


def preregistration() -> list[str]:
    L: list[str] = []
    w = L.append
    w("# Study feature: peer-to-peer / reciprocal teaching (spec §8)\n")
    w(
        "A short **3-user ablation** of the peer-to-peer / reciprocal-teaching feature. The "
        "hypothesis and analysis below are **pre-registered** (fixed before data collection).\n"
    )
    w("## Pre-registration\n")
    w(
        "**Feature.** _Correct the Peer_ (the AI presents a plausible-but-wrong solution, the "
        "student critiques it, the AI judges the critique) + AI _peer explanations_ of missed "
        "practice MCQs. Learning-science basis: the protégé effect / learning-by-teaching and "
        "self-explanation (Roediger & Karpicke 2006; Bisra et al. 2018; Roscoe & Chi 2007)."
    )
    w(
        "\n**Hypothesis.** At **equal study time**, studying with the peer feature ON yields "
        "higher accuracy on a **held-out post-test of new questions** than the same MCQ "
        "practice with the peer step OFF; both beat plain (passive) Anki review."
    )
    w(
        "\n**Primary outcome:** post-test accuracy per arm. **Primary contrast:** "
        "**peer-ON − peer-OFF** (isolates the feature). Secondary: each arm vs plain Anki."
    )
    w(
        "\n**Failure criterion (pre-registered):** if peer-ON does not exceed peer-OFF, the "
        "feature shows no benefit here — reported honestly. n=3 is a **pilot**: it cannot "
        "establish significance; we report descriptive results + each participant's paired "
        "difference, not a powered inference.\n"
    )
    w("## Protocol (how the 3-user study is run)\n")
    w(
        "- **Design:** within-subjects. Each participant does **all three arms** on three "
        "**matched, disjoint topic sets** (A/B/C, similar difficulty), so each person is their "
        "own control. **Counterbalance** which topic set maps to which arm across the 3 "
        "participants (a 3×3 Latin square) to cancel topic/order effects."
    )
    w(
        "- **Equal study time:** a fixed timer per arm (recommend **12–15 min**), same for all "
        "arms and participants."
    )
    w(
        "- **Arms in the app:** _Peer ON_ = AI on + peer on, study the topic's MCQ practice "
        "deck using Correct-the-Peer / peer explanations. _Peer OFF_ = **Tools → GMAT: Toggle "
        "Peer Feature** (off), same MCQ practice, no peer step. _Plain Anki_ = review that "
        "topic's term flashcards normally (no MCQ practice, no peer)."
    )
    w(
        "- **Post-test:** immediately after each arm, the participant answers a fixed set of "
        "**new** MCQs on that topic (not seen during study). Record correct / total."
    )
    w(
        "- **Record** each (participant, arm, correct, total) in `data/gmat/study_results.csv` "
        "and run `just study-feature` (or this script) to regenerate the Results below.\n"
    )
    return L


def results_section(rows: list[dict]) -> list[str]:
    L: list[str] = []
    w = L.append
    w("## Results\n")
    if not rows:
        w(
            "_Data collection pending — fill `data/gmat/study_results.csv` (one row per "
            "participant × arm) and re-run `just study-feature`. The pre-registered design "
            "above is fixed in advance._\n"
        )
        return L

    # per-arm pooled accuracy
    pooled = {a: [0, 0] for a in ARMS}
    by_part: dict[str, dict[str, float]] = {}
    for r in rows:
        pooled[r["arm"]][0] += r["correct"]
        pooled[r["arm"]][1] += r["total"]
        by_part.setdefault(r["participant"], {})[r["arm"]] = r["correct"] / r["total"]

    n_part = len(by_part)
    w(
        f"Participants: **{n_part}**. Per-arm pooled post-test accuracy (95% Wilson CI):\n"
    )
    arm_rows = []
    for a in ARMS:
        k, n = pooled[a]
        if n == 0:
            arm_rows.append([LABEL[a], "—", "—", "0"])
            continue
        lo, hi = wilson_ci(k, n)
        arm_rows.append(
            [LABEL[a], f"{k / n * 100:.1f}%", f"{lo * 100:.1f}–{hi * 100:.1f}%", str(n)]
        )
    L.extend(md_table(["Arm", "Accuracy", "95% CI", "n items"], arm_rows))
    w("")

    # per-participant paired contrast peer_on - peer_off
    w("### Primary contrast — peer-ON − peer-OFF (per participant)\n")
    part_rows = []
    diffs = []
    on_gt_off = 0
    for p, arms in sorted(by_part.items()):
        on = arms.get("peer_on")
        off = arms.get("peer_off")
        pl = arms.get("plain")
        d = (on - off) if (on is not None and off is not None) else None
        if d is not None:
            diffs.append(d)
            if d > 0:
                on_gt_off += 1
        part_rows.append(
            [
                p,
                "" if on is None else f"{on * 100:.0f}%",
                "" if off is None else f"{off * 100:.0f}%",
                "" if pl is None else f"{pl * 100:.0f}%",
                "" if d is None else f"{d * 100:+.0f} pts",
            ]
        )
    L.extend(
        md_table(["Participant", "Peer ON", "Peer OFF", "Plain", "ON − OFF"], part_rows)
    )
    w("")
    if diffs:
        mean_d = statistics.fmean(diffs)
        w(
            f"**Mean paired difference (peer-ON − peer-OFF): {mean_d * 100:+.1f} pts** "
            f"across {len(diffs)} participant(s); {on_gt_off}/{len(diffs)} had ON > OFF."
        )
        verdict = (
            "peer feature helped in this pilot"
            if mean_d > 0 and on_gt_off == len(diffs)
            else "mixed / no clear benefit in this pilot"
            if mean_d > 0
            else "no benefit (or negative) in this pilot"
        )
        w(f"\nDirectional read: **{verdict}**.")
    w(
        "\n> **n=3 is a pilot.** With three participants there is no statistical power; these "
        "are descriptive results and per-participant differences, reported honestly. The "
        "hypothesis and analysis were pre-registered above before data collection.\n"
    )
    return L


def main():
    rows = load_rows()
    out = preregistration() + results_section(rows)
    report = _normalize_md("\n".join(out))
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write(report)
    sys.stdout.write(report)
    n = len({r["participant"] for r in rows})
    sys.stderr.write(f"\n[wrote {REPORT}; {len(rows)} rows, {n} participants]\n")


if __name__ == "__main__":
    main()
