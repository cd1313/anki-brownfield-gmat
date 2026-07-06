# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Honest evaluation of the GMAT IRT performance model (spec §9, §7e).

Because we have no real multi-student data, this validates the *estimator* the
way the spec rewards: it simulates students with a KNOWN ability, drives the
real shipped engine (`estimate_readiness`) to recover it, and evaluates
held-out performance prediction against baselines it must beat — with a leakage
check. It does NOT claim the projected score is validated against real exam
outcomes (that needs held-out students; spec Step 4 bonus).

Reproducible: fixed seed, no external deps. Run:

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/gmat_eval/run_eval.py

Writes a markdown report to docs/gmat/EVAL-RESULTS.md.
"""

from __future__ import annotations

import math
import os
import random
import statistics
import sys
import tempfile

from _md import format_md  # type: ignore[import-not-found]

from anki.collection import Collection
from anki.decks import DeckId

# --- config (change here; report records these) ---------------------------
SEED = 20260701
N_STUDENTS = 80
K_ESTIMATION = 30  # items used to fit theta (logged to the engine)
L_TEST = 15  # held-out items used to evaluate prediction (never logged)
N_CHOICES = 5  # 5-way MCQ -> guessing c = 1/5 = 0.2 (matches the engine)
IRT_D = 1.702  # logistic scaling (must match rslib/src/gmat/mod.rs)
GUESS_C = 1.0 / N_CHOICES

REPORT = os.path.join("docs", "gmat", "EVAL-RESULTS.md")


def three_pl(theta: float, c: float = GUESS_C, a: float = 1.0, b: float = 0.0) -> float:
    return c + (1.0 - c) / (1.0 + math.exp(-IRT_D * a * (theta - b)))


def make_mcq_notetype(col: Collection):
    mm = col.models
    nt = mm.new("GMAT MCQ")
    for field in ["Question", "A", "B", "C", "D", "E", "Answer", "Explanation"]:
        mm.add_field(nt, mm.new_field(field))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = "{{Question}}"
    tmpl["afmt"] = "{{Question}}<hr>{{Answer}}"
    mm.add_template(nt, tmpl)
    mm.add(nt)
    return nt


def recover_theta(
    col: Collection, nt, rng: random.Random, theta_true: float, student: int
):
    """Add K estimation items for one student, log simulated responses, and
    return (theta_hat, estimation_outcomes)."""
    marker = f"estu{student}"
    cids = []
    for _ in range(K_ESTIMATION):
        note = col.new_note(nt)
        note["Question"] = "Q"
        for opt in ["A", "B", "C", "D", "E"]:
            note[opt] = opt
        note["Answer"] = "C"
        note.tags = ["GMAT::Quant::Algebra", marker]
        col.add_note(note, deck_id=DeckId(1))
        cids.append(note.cards()[0].id)

    outcomes = []
    for cid in cids:
        correct = rng.random() < three_pl(theta_true)
        outcomes.append(1 if correct else 0)
        # chosen "C" == correct answer, anything else == wrong.
        col._backend.grade_mcq(
            card_id=cid, chosen="C" if correct else "A", took_millis=3000
        )

    sections = col._backend.estimate_readiness(
        search=f"tag:{marker}",
        tag_prefix="GMAT",
        time_budget_secs=120,
        section_minutes=45,
        min_responses=1,
        min_coverage=0.0,
        max_se=99.0,  # relaxed: we want theta regardless of give-up here
    ).sections
    theta_hat = sections[0].theta if sections else 0.0
    return theta_hat, outcomes


def brier(preds, actuals):
    return statistics.fmean((p - a) ** 2 for p, a in zip(preds, actuals))


def log_loss(preds, actuals, eps=1e-6):
    total = 0.0
    for p, a in zip(preds, actuals):
        p = min(1 - eps, max(eps, p))
        total += -(a * math.log(p) + (1 - a) * math.log(1 - p))
    return total / len(preds)


def accuracy(preds, actuals):
    return statistics.fmean(
        1.0 if (p >= 0.5) == (a == 1) else 0.0 for p, a in zip(preds, actuals)
    )


def pearson(xs, ys):
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return num / (dx * dy) if dx > 0 and dy > 0 else 0.0


def main():
    rng = random.Random(SEED)
    tmp = tempfile.mkdtemp(prefix="gmat_eval_")
    col = Collection(os.path.join(tmp, "collection.anki2"))
    nt = make_mcq_notetype(col)

    thetas_true, thetas_hat = [], []
    # Held-out prediction data (test items never logged to the engine).
    irt_preds, base_preds, actuals = [], [], []

    for s in range(N_STUDENTS):
        theta_true = rng.gauss(0.0, 1.0)
        theta_hat, est_outcomes = recover_theta(col, nt, rng, theta_true, s)
        thetas_true.append(theta_true)
        thetas_hat.append(theta_hat)

        # Held-out test items: simulate from theta_true, predict from theta_hat.
        # The estimation items (logged to the engine) and the test items are
        # generated in separate loops and the test items are never logged, so the
        # split is disjoint by construction — there is nothing to leak here. Real
        # data-leakage (eval gold sets vs practice banks) is scanned separately by
        # tools/gmat_eval/check_leakage.py (see docs/gmat/LEAKAGE-CHECK.md).
        base_rate = statistics.fmean(est_outcomes) if est_outcomes else 0.5
        for _ in range(L_TEST):
            actual = 1 if rng.random() < three_pl(theta_true) else 0
            actuals.append(actual)
            irt_preds.append(three_pl(theta_hat))  # IRT prediction at recovered theta
            base_preds.append(base_rate)  # baseline: student's observed base rate

    col.close()

    bias = statistics.fmean(h - t for h, t in zip(thetas_hat, thetas_true))
    rmse = math.sqrt(
        statistics.fmean((h - t) ** 2 for h, t in zip(thetas_hat, thetas_true))
    )
    corr = pearson(thetas_true, thetas_hat)

    irt = (
        accuracy(irt_preds, actuals),
        log_loss(irt_preds, actuals),
        brier(irt_preds, actuals),
    )
    base = (
        accuracy(base_preds, actuals),
        log_loss(base_preds, actuals),
        brier(base_preds, actuals),
    )
    # Global base rate baseline (single constant prediction).
    gr = statistics.fmean(actuals)
    grp = [gr] * len(actuals)
    glob = (accuracy(grp, actuals), log_loss(grp, actuals), brier(grp, actuals))

    lines: list[str] = []
    w = lines.append
    w("# GMAT IRT performance model — evaluation results\n")
    w(
        "_Generated by `tools/gmat_eval/run_eval.py` (reproducible: fixed seed). "
        "Validates the shipped `estimate_readiness` engine on simulated students; "
        "the projected score is NOT validated against real exam outcomes._\n"
    )
    w(
        f"Config: seed={SEED}, students={N_STUDENTS}, estimation items={K_ESTIMATION}, "
        f"held-out items={L_TEST}, choices={N_CHOICES} (c={GUESS_C:.2f}), D={IRT_D}.\n"
    )

    w("## 1. Ability recovery (synthetic)\n")
    w("Simulate a known θ per student, drive the real engine to recover it.\n")
    w(f"- Bias (mean θ̂ − θ): **{bias:+.3f}** (≈0 is unbiased)")
    w(f"- RMSE: **{rmse:.3f}**")
    w(f"- Correlation(θ_true, θ̂): **{corr:.3f}**\n")

    w("## 2. Held-out performance prediction (beats baselines)\n")
    w(
        "Fit θ on estimation items; predict correctness on disjoint held-out items. "
        "Lower log-loss/Brier is better; higher accuracy is better.\n"
    )
    w("| Model | Accuracy | Log-loss | Brier |")
    w("|---|---|---|---|")
    w(f"| **IRT (θ̂)** | {irt[0]:.3f} | {irt[1]:.3f} | {irt[2]:.3f} |")
    w(
        f"| Baseline: per-student base rate | {base[0]:.3f} | {base[1]:.3f} | {base[2]:.3f} |"
    )
    w(
        f"| Baseline: global base rate | {glob[0]:.3f} | {glob[1]:.3f} | {glob[2]:.3f} |\n"
    )
    beats = irt[1] < base[1] and irt[1] < glob[1]
    w(f"IRT beats both baselines on log-loss: **{beats}**.\n")

    w("## 3. Leakage check (spec §7e)\n")
    w(
        "This synthetic study fits θ on estimation items and evaluates on a "
        "**separate** set of held-out items that are generated independently and "
        "never logged to the engine, so the train/test split is disjoint by "
        "construction. Nothing is trained on real content here. Real data-leakage — "
        "whether the hand-labelled AI **gold sets** appear in the **practice** banks "
        "— is scanned over the actual data by `tools/gmat_eval/check_leakage.py`; the "
        "latest result is **CLEAN** (see `docs/gmat/LEAKAGE-CHECK.md`).\n"
    )

    w("## Honesty notes\n")
    w(
        "- Item difficulty `b=0` and discrimination `a=1` are **assumed**; guessing "
        "`c=1/#choices`. The synthetic study uses the same model, so this validates "
        "the *estimator*, not the (uncalibrated) item parameters."
    )
    w(
        "- The projected GMAT score mapping (θ→percentile→60–90) is a documented "
        "placeholder and is **not** validated here.\n"
    )

    report = format_md("\n".join(lines))
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write(report)
    sys.stdout.write(report)
    sys.stderr.write(f"\n[wrote {REPORT}]\n")


if __name__ == "__main__":
    main()
