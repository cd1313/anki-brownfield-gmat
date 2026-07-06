# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Build materials for the 3-user peer-feature ablation study (spec §8).

Draws matched, disjoint question sets from the AQuA Quant bank (which ships worked
rationales — needed by the "Correct the Peer" flawed-solution feature) and writes,
for each of three arms A/B/C:
  - a STUDY deck (what the participant practices during that arm), and
  - a disjoint held-out POST-TEST deck (NEW questions, scored after the arm).

All sets are the same topic (Quant), randomly assigned from one homogeneous pool,
so they are matched in expectation; a fixed seed makes it reproducible. Study decks
live OUTSIDE `GMAT::Practice` (in `GMAT::Study::*`) so the app's practice pool can't
cross-serve cards between arms; post-tests live in `GMAT::PostTest::*`.

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/gmat_eval/make_study_sets.py

Outputs to data/gmat/study_sets/ (Anki-importable .txt) + an answer key CSV, and
writes docs/gmat/study-materials.md with the run instructions.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import random
import re

SEED = 20260701
N_STUDY = 15  # study questions per arm
N_TEST = 10  # held-out post-test questions per arm
SETS = ["A", "B", "C"]
CANDIDATE_POOL = 4000  # reservoir of candidates to sample the final questions from

SRC = os.path.join("data", "gmat", "aqua_mcq.csv")
OUT_DIR = os.path.join("data", "gmat", "study_sets")
DOC = os.path.join("docs", "gmat", "study-materials.md")

csv.field_size_limit(10_000_000)
_WS = re.compile(r"\s+")


def norm(text: str) -> str:
    return _WS.sub(" ", (text or "").lower()).strip()


def qhash(row: dict) -> str:
    return hashlib.sha1(norm(row["Question"]).encode("utf-8")).hexdigest()


def load_candidates(rng: random.Random) -> list[dict]:
    """Reservoir-sample well-formed AQuA rows (all 5 options, letter answer, a
    non-empty rationale), de-duplicated by question text."""
    seen: set[str] = set()
    reservoir: list[dict] = []
    n = 0
    with open(SRC, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if not row.get("Question", "").strip():
                continue
            if not all(row.get(k, "").strip() for k in "ABCDE"):
                continue
            if row.get("Answer", "").strip().upper() not in {"A", "B", "C", "D", "E"}:
                continue
            if not row.get("Explanation", "").strip():
                continue
            h = qhash(row)
            if h in seen:
                continue
            seen.add(h)
            n += 1
            # reservoir sampling of size CANDIDATE_POOL
            if len(reservoir) < CANDIDATE_POOL:
                reservoir.append(row)
            else:
                j = rng.randint(0, n - 1)
                if j < CANDIDATE_POOL:
                    reservoir[j] = row
    return reservoir


def write_import(path: str, deck: str, tags: str, rows: list[dict]) -> None:
    buf = io.StringIO()
    buf.write("#separator:Comma\n#html:true\n#notetype:GMAT MCQ\n")
    buf.write(f"#deck:{deck}\n#tags column:9\n")
    wtr = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    for r in rows:
        wtr.writerow(
            [
                r["Question"].strip(),
                r["A"].strip(),
                r["B"].strip(),
                r["C"].strip(),
                r["D"].strip(),
                r["E"].strip(),
                r["Answer"].strip().upper(),
                r["Explanation"].strip(),
                tags,
            ]
        )
    with open(path, "w", encoding="utf-8") as f:
        f.write(buf.getvalue())


def main():
    rng = random.Random(SEED)
    cand = load_candidates(rng)
    need = len(SETS) * (N_STUDY + N_TEST)
    if len(cand) < need:
        raise SystemExit(f"only {len(cand)} candidates, need {need}")
    rng.shuffle(cand)
    picked = cand[:need]

    os.makedirs(OUT_DIR, exist_ok=True)
    key_rows: list[tuple[str, str, str, str]] = [
        ("set", "phase", "idx", "correct_answer")
    ]
    per_set = N_STUDY + N_TEST
    for i, s in enumerate(SETS):
        block = picked[i * per_set : (i + 1) * per_set]
        study, test = block[:N_STUDY], block[N_STUDY:]
        write_import(
            os.path.join(OUT_DIR, f"study_{s}.txt"),
            f"GMAT::Study::{s}",
            f"GMAT::Quant GMAT::Quant::Study{s}",
            study,
        )
        write_import(
            os.path.join(OUT_DIR, f"posttest_{s}.txt"),
            f"GMAT::PostTest::{s}",
            f"GMAT::PostTest::{s}",
            test,
        )
        for j, r in enumerate(study, 1):
            key_rows.append((s, "study", str(j), r["Answer"].strip().upper()))
        for j, r in enumerate(test, 1):
            key_rows.append((s, "posttest", str(j), r["Answer"].strip().upper()))

    with open(
        os.path.join(OUT_DIR, "answer_keys.csv"), "w", newline="", encoding="utf-8"
    ) as f:
        csv.writer(f).writerows(key_rows)

    _write_doc()
    # Cross-check: study vs post-test disjoint within every set (by construction).
    print(f"Wrote {len(SETS)} study + {len(SETS)} post-test decks to {OUT_DIR}/")
    print(f"  {N_STUDY} study + {N_TEST} post-test questions per set; seed {SEED}.")
    print(f"Answer key: {os.path.join(OUT_DIR, 'answer_keys.csv')}")
    print(f"Instructions: {DOC}")


def _write_doc() -> None:
    L: list[str] = []
    w = L.append
    w("# Study materials — 3-user peer-feature ablation (spec §8)\n")
    w(
        "Generated by `tools/gmat_eval/make_study_sets.py` (fixed seed). Three matched Quant "
        "question sets (A/B/C), each with a **study** deck and a disjoint **held-out post-test** "
        "deck, drawn from the AQuA bank. Pairs with the protocol in "
        "[`STUDY-FEATURE.md`](STUDY-FEATURE.md) and the scorer `just study-feature`.\n"
    )
    w("## 1. Import the decks\n")
    w(
        "In Anki: **File → Import**, pick each file in `data/gmat/study_sets/`. Study decks "
        f"import to `GMAT::Study::A/B/C` ({N_STUDY} cards each); post-test decks to "
        f"`GMAT::PostTest::A/B/C` ({N_TEST} cards each). (Run the app once first so the "
        "`GMAT MCQ` note type exists.) Study decks sit **outside** `GMAT::Practice` so the "
        "practice pool won't mix sets between arms.\n"
    )
    w("## 2. Assign arms (counterbalanced Latin square)\n")
    w(
        "Within-subjects: each participant does all three arms, one per set, at **equal time** "
        "(recommend 12–15 min/arm). Rotate which set → which arm so topic difficulty cancels:\n"
    )
    w("| Participant | Peer ON | Peer OFF | Plain Anki |")
    w("| ----------- | ------- | -------- | ---------- |")
    w("| 1           | Set A   | Set B    | Set C      |")
    w("| 2           | Set B   | Set C    | Set A      |")
    w("| 3           | Set C   | Set A    | Set B      |")
    w("")
    w("## 3. Run each arm\n")
    w(
        "- **Peer ON:** AI on + **Tools → GMAT: Toggle Peer Feature = ON**. Study that arm's "
        "`GMAT::Study::X` deck (answer the MCQs; on a miss the AI peer explains; optionally play "
        "Correct the Peer) for the fixed time."
    )
    w(
        "- **Peer OFF (ablation):** **Tools → GMAT: Toggle Peer Feature = OFF**. Study the arm's "
        "`GMAT::Study::X` deck the same way for the same time — MCQ grading works, no peer step."
    )
    w(
        "- **Plain Anki:** review the arm's set as ordinary flashcards (no MCQ practice / no "
        "peer) for the same time — the passive baseline."
    )
    w("## 4. Post-test (the measurement)\n")
    w(
        "Immediately after each arm, the participant answers that set's `GMAT::PostTest::X` deck "
        "**once**, with the **peer feature OFF** (it's a test, not study). These are NEW "
        f"questions ({N_TEST}), not seen during study, so they measure transfer. Record the "
        "score."
    )
    w("## 5. Score it\n")
    w(
        "Enter each `(participant, arm, correct, total)` in `data/gmat/study_results.csv` "
        "(`arm` = `peer_on` | `peer_off` | `plain`), then run `just study-feature` to fill the "
        "Results in `STUDY-FEATURE.md`. The answer key is `data/gmat/study_sets/answer_keys.csv` "
        "if you score by hand.\n"
    )
    w("## Honesty notes\n")
    w(
        "- Sets are matched by random draw from one homogeneous Quant source (AQuA has no "
        "difficulty labels); post-tests are disjoint from study by construction. n=3 is a "
        "pilot — report descriptively (see STUDY-FEATURE.md)."
    )
    w(
        "- Keep the timer strict and administer the post-test uniformly (peer off) across all "
        "arms so the only difference between arms is the feature.\n"
    )
    os.makedirs(os.path.dirname(DOC), exist_ok=True)
    with open(DOC, "w", encoding="utf-8") as f:
        f.write(_normalize_md("\n".join(L)))


def _normalize_md(text: str) -> str:
    """Keep generated Markdown dprint-clean: a blank line before every heading,
    no runs of blank lines, single trailing newline."""
    text = re.sub(
        r"([^\n])\n(#{1,6} )", r"\1\n\n\2", text
    )  # blank line before headings
    text = re.sub(r"\n{3,}", "\n\n", text)  # collapse extra blanks
    return text.rstrip("\n") + "\n"


if __name__ == "__main__":
    main()
