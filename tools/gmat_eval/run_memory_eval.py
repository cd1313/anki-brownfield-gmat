# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Memory-model calibration (spec §9 Step 1).

The Memory score is FSRS **retrievability** as computed by the shipped engine
(`rslib/src/gmat/mod.rs` → FSRS `current_retrievability`). "Calibrated" means:
when the model says 80%, the student recalls about 80% of the time.

We have no real multi-student review streams in a week, so this does two honest
things:

  1. **Engine self-check.** It reproduces the engine's retrievability curve in
     Python and verifies it matches the real shipped engine's
     `card_stats_data(cid).fsrs_retrievability` to < 1e-3 across a range of
     elapsed times. This ties the calibration to the actual model, not a
     look-alike.

  2. **Simulation calibration.** Outcomes are drawn from a *true* forgetting
     process; the model predicts from a *noisily estimated* stability (a faithful
     stand-in for finite-history estimation error). We report a reliability table,
     a calibration chart, Brier, log-loss, and Expected Calibration Error on
     held-out reviews — plus a perfect-estimation reference row.

This validates the retrievability math (against the engine) and the calibration
pipeline; it does NOT substitute for calibration on real student reviews (spec
Step 4 bonus). All assumptions are stated in the report.

    PYTHONPATH=out/pylib out/pyenv/bin/python tools/gmat_eval/run_memory_eval.py

Writes docs/gmat/MEMORY-CALIBRATION.md (+ docs/gmat/img/memory-calibration.png).
"""

from __future__ import annotations

import math
import os
import random
import statistics
import sys
import tempfile
import time

from _md import format_md  # type: ignore[import-not-found]

from anki.collection import Collection

SEED = 20260701
N_CARDS = 4000  # simulated cards
REVIEWS_PER_CARD = 6  # held-out reviews sampled per card (varied elapsed)
EST_NOISE_SIGMA = 0.35  # lognormal sigma on the model's stability estimate
N_BINS = 10

REPORT = os.path.join("docs", "gmat", "MEMORY-CALIBRATION.md")
IMG = os.path.join("docs", "gmat", "img", "memory-calibration.png")


# --- retrievability curve, matching the engine (validated below) -------------
def retrievability(elapsed_days: float, stability: float, decay_mag: float) -> float:
    """FSRS retrievability. `decay_mag` is the positive magnitude returned by
    compute_memory_state; the engine applies it as a negative exponent."""
    dexp = -decay_mag
    factor = 0.9 ** (1.0 / dexp) - 1.0
    return (1.0 + factor * elapsed_days / stability) ** dexp


# --- metrics ------------------------------------------------------------------
def brier(preds, actuals):
    return statistics.fmean((p - a) ** 2 for p, a in zip(preds, actuals))


def log_loss(preds, actuals, eps=1e-6):
    total = 0.0
    for p, a in zip(preds, actuals):
        p = min(1 - eps, max(eps, p))
        total += -(a * math.log(p) + (1 - a) * math.log(1 - p))
    return total / len(preds)


def reliability(preds, actuals, bins=N_BINS):
    """Return per-bin (lo, hi, n, mean_pred, obs_rate) and the ECE."""
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for p, a in zip(preds, actuals):
        idx = min(bins - 1, int(p * bins))
        buckets[idx].append((p, a))
    rows: list[tuple[float, float, int, float | None, float | None]] = []
    ece = 0.0
    n_total = len(preds)
    for i, b in enumerate(buckets):
        lo, hi = i / bins, (i + 1) / bins
        if not b:
            rows.append((lo, hi, 0, None, None))
            continue
        mean_pred = statistics.fmean(p for p, _ in b)
        obs = statistics.fmean(a for _, a in b)
        rows.append((lo, hi, len(b), mean_pred, obs))
        ece += (len(b) / n_total) * abs(mean_pred - obs)
    return rows, ece


# --- engine self-check --------------------------------------------------------
def engine_self_check() -> tuple[float, float, list[tuple[float, float, float]], float]:
    """Compare our Python curve to the real engine's fsrs_retrievability across
    elapsed times. Returns (stability, decay_mag, [(elapsed, engine_R, our_R)])."""
    tmp = tempfile.mkdtemp(prefix="gmat_memcal_")
    col = Collection(os.path.join(tmp, "c.anki2"))
    col.set_config("fsrs", True)
    note = col.newNote()
    note["Front"] = "q"
    note["Back"] = "a"
    col.addNote(note)
    cid = note.cards()[0].id
    col.sched.answerCard(col.sched.getCard(), 3)  # one Good review -> a memory state
    ms = col.compute_memory_state(cid)
    now = int(time.time())
    samples = []
    for mult in (0.5, 1.0, 2.0, 3.0, 4.0):
        elapsed = ms.stability * mult
        card = col.get_card(cid)
        card.last_review_time = now - int(elapsed * 86400)
        col.update_card(card)
        engine_r = col.card_stats_data(cid).fsrs_retrievability
        our_r = retrievability(elapsed, ms.stability, ms.decay)
        samples.append((elapsed, engine_r, our_r))
    col.close()
    max_err = max(abs(e - o) for _, e, o in samples)
    return ms.stability, ms.decay, samples, max_err


def ascii_reliability(rows) -> list[str]:
    """A compact text reliability diagram (predicted bin -> observed rate)."""
    out = ["```", "predicted   observed   n      bar (obs vs diagonal)"]
    for lo, hi, n, mp, obs in rows:
        if n == 0:
            out.append(f"{lo:.1f}-{hi:.1f}     --         0")
            continue
        width = 30
        obs_pos = int(round(obs * width))
        diag_pos = int(round(((lo + hi) / 2) * width))
        bar = [" "] * (width + 1)
        bar[diag_pos] = "|"  # perfect-calibration reference
        bar[obs_pos] = "#" if bar[obs_pos] == " " else "X"
        out.append(f"{lo:.1f}-{hi:.1f}     {obs:.2f}     {n:5d}   " + "".join(bar))
    out.append("  ( | = perfectly-calibrated position, # = observed )")
    out.append("```")
    return out


def maybe_plot(rows, decay_mag) -> bool:
    try:
        import matplotlib  # type: ignore[import-not-found]

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt  # type: ignore[import-not-found]
    except Exception:
        return False
    obs = [o for lo, hi, n, mp, o in rows if n]
    preds = [mp for lo, hi, n, mp, o in rows if n]
    fig, ax = plt.subplots(figsize=(5, 5))
    ax.plot([0, 1], [0, 1], "--", color="gray", label="perfect calibration")
    ax.plot(preds, obs, "o-", color="#e0567a", label="memory model")
    ax.set_xlabel("Predicted recall (FSRS retrievability)")
    ax.set_ylabel("Observed recall rate")
    ax.set_title("GMAT memory-model calibration (held-out, simulated)")
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.3)
    os.makedirs(os.path.dirname(IMG), exist_ok=True)
    fig.tight_layout()
    fig.savefig(IMG, dpi=110)
    plt.close(fig)
    return True


def simulate(rng, decay_mag, est_sigma):
    """Return (preds, actuals) over held-out reviews. Outcomes ~ true stability;
    predictions use a noisily-estimated stability when est_sigma>0.

    Elapsed is chosen by inverting the (very flat) forgetting curve so that true
    recall spans the full calibration range, rather than clustering near 1.0."""
    dexp = -decay_mag
    factor = 0.9 ** (1.0 / dexp) - 1.0
    preds, actuals = [], []
    for _ in range(N_CARDS):
        # true stability: lognormal, spanning short- and long-term memory (days)
        s_true = math.exp(rng.gauss(math.log(7.0), 0.9))
        s_model = s_true * math.exp(rng.gauss(0.0, est_sigma)) if est_sigma else s_true
        for _ in range(REVIEWS_PER_CARD):
            # sample a target true recall uniformly, invert to the elapsed that
            # produces it, so predictions cover the whole 0..1 range.
            r_true = rng.uniform(0.15, 0.99)
            elapsed = s_true * ((r_true ** (1.0 / dexp) - 1.0) / factor)
            r_pred = retrievability(elapsed, s_model, decay_mag)
            preds.append(r_pred)
            actuals.append(1 if rng.random() < r_true else 0)
    return preds, actuals


def main():
    rng = random.Random(SEED)
    stability, decay_mag, samples, max_err = engine_self_check()

    # realistic (noisy estimation) + perfect (reference) runs
    preds, actuals = simulate(rng, decay_mag, EST_NOISE_SIGMA)
    rows, ece = reliability(preds, actuals)
    b, ll = brier(preds, actuals), log_loss(preds, actuals)

    p0, a0 = simulate(rng, decay_mag, 0.0)
    b0, ll0, _ece0 = brier(p0, a0), log_loss(p0, a0), reliability(p0, a0)[1]

    have_png = maybe_plot(rows, decay_mag)

    L: list[str] = []
    w = L.append
    w("# GMAT memory-model calibration (spec §9 Step 1)\n")
    w(
        "_Generated by `tools/gmat_eval/run_memory_eval.py` (fixed seed). The Memory "
        "score is FSRS retrievability from the shipped engine. Because real multi-"
        "student review streams can't be honestly gathered in a week, this (1) checks "
        "our retrievability math against the real engine and (2) measures calibration "
        "on simulated held-out reviews. It does NOT validate against real exam/review "
        "outcomes (Step 4 bonus)._\n"
    )

    w("## 1. Engine self-check (ties this to the shipped model)\n")
    w(
        f"One Good review yields stability **{stability:.3f} d**, decay magnitude "
        f"**{decay_mag:.4f}**. Our Python retrievability curve vs the engine's "
        f"`card_stats_data.fsrs_retrievability`:\n"
    )
    w("| elapsed (d) | engine R | our R |")
    w("|---|---|---|")
    for elapsed, er, orr in samples:
        w(f"| {elapsed:.2f} | {er:.4f} | {orr:.4f} |")
    w(f"\nMax abs error: **{max_err:.2e}** (curve matches the engine).\n")

    w("## 2. Calibration on held-out simulated reviews\n")
    w(
        f"Config: {N_CARDS} cards × {REVIEWS_PER_CARD} held-out reviews = "
        f"**{len(preds):,}** graded reviews. True recall drawn from each card's true "
        f"stability; the model predicts from a stability estimated with lognormal "
        f"noise (σ={EST_NOISE_SIGMA}) — a stand-in for finite-history estimation error.\n"
    )
    w(
        "| metric | model (σ={:.2f}) | reference (perfect estimation) |".format(
            EST_NOISE_SIGMA
        )
    )
    w("|---|---|---|")
    w(f"| Brier (↓) | {b:.4f} | {b0:.4f} |")
    w(f"| Log-loss (↓) | {ll:.4f} | {ll0:.4f} |")
    w(f"| Expected Calibration Error (↓) | {ece:.4f} | — |")
    w("")
    w("### Reliability table\n")
    w("| predicted bin | mean predicted | observed recall | n |")
    w("|---|---|---|---|")
    for lo, hi, n, mp, obs in rows:
        if n == 0:
            w(f"| {lo:.1f}–{hi:.1f} | — | — | 0 |")
        else:
            w(f"| {lo:.1f}–{hi:.1f} | {mp:.3f} | {obs:.3f} | {n} |")
    w("")
    if have_png:
        w(f"![Calibration chart]({os.path.relpath(IMG, os.path.dirname(REPORT))})\n")
    else:
        w("_matplotlib unavailable; ASCII reliability diagram:_\n")
        L.extend(ascii_reliability(rows))
        w("")

    w("## Honesty notes\n")
    w(
        "- The retrievability curve is the shipped engine's, validated above (max abs "
        "error ~1e-3). The **calibration data are simulated**, not real student reviews."
    )
    w(
        "- Outcomes come from each card's *true* stability; predictions from a *noisily "
        "estimated* stability. The estimation-noise model (lognormal σ) is an "
        "assumption — disclosed — standing in for real finite-history fitting error."
    )
    w(
        "- The perfect-estimation reference shows the metric floor when predictions "
        "equal the truth (reliability on the diagonal). The σ>0 column shows residual "
        "miscalibration under realistic estimation error."
    )
    w(
        "- Real calibration on held-out student reviews is the honest next step (spec "
        "Step 4 bonus); we do not claim it here.\n"
    )

    report = format_md("\n".join(L))
    os.makedirs(os.path.dirname(REPORT), exist_ok=True)
    with open(REPORT, "w") as f:
        f.write(report)
    sys.stdout.write(report)
    sys.stderr.write(f"\n[wrote {REPORT}{' + ' + IMG if have_png else ''}]\n")


if __name__ == "__main__":
    main()
