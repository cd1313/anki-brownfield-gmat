# Model 2 — Performance (can the student answer a new exam-style question?)

_One-pager for the submission (spec §12). Engine: `estimate_readiness` /
`eap_ability` in `rslib/src/gmat/mod.rs`; surfaced by `qt/aqt/gmat.py`._

## What it measures

The probability the student gets a **new, exam-style MCQ right**, per section — including
questions they have never seen. This is the bridge from memory to application, measured on a
**separate pool** of MCQs (`GMAT::Practice`), not on the term flashcards.

## Method

- **Item Response Theory, 3-parameter logistic (3PL):**
  `P(correct | θ) = c + (1−c)/(1 + e^(−D·a·(θ−b)))`, `D = 1.702`.
  Item parameters are **assumed** (discrimination `a = 1`, difficulty `b = 0`) with guessing
  `c = 1/#choices` per section (Quant 0.20, Verbal 0.25, Data Insights 0.50). Difficulty is not
  calibrated — no per-item labels exist in the source datasets.
- **Estimation:** ability **θ** by **EAP** (expected a posteriori) over a θ grid with an
  `N(0,1)` prior; θ̂ = posterior mean, and the posterior SD is the honest standard error. EAP
  stays finite for all-correct/all-wrong and few responses (unlike MLE).
- **Shown with a range:** the dashboard reports **θ̂ ± SE** and an **accuracy band** obtained by
  mapping `θ̂ ± SE` through the same 3PL — never a bare point number.
- **Accuracy-only:** response times are recorded but do **not** enter θ, so Performance stays
  distinct from pacing (and keeps the memory-vs-performance separation clean).
- MCQ attempts are logged as non-scheduling ("cramming") revlog entries, so they never touch
  FSRS or the Memory score.

## Give-up rule

> **Abstain unless the section has ≥ 20 graded MCQ responses AND the ability standard error
> SE(θ) ≤ 0.7** (a precision floor).

## Evidence

- **Estimator validation (held-out):** `docs/gmat/EVAL-RESULTS.md` (`just eval-perf`). On
  simulated students the engine recovers θ with **correlation 0.93** (bias ≈ 0), and held-out
  correctness prediction **beats per-student and global base-rate baselines** on log-loss
  (0.541 vs 0.548 / 0.682).
- **Not a copy of Memory:** `docs/gmat/PERF-VS-MEMORY.md` (`just perf-vs-memory`). Holding term
  reviews fixed and varying MCQ accuracy, Memory stays flat while Performance tracks accuracy
  (32%→88%) — the two scores are driven by different inputs and diverge.
- **Leakage:** the AI/eval gold sets do not appear in the practice banks
  (`docs/gmat/LEAKAGE-CHECK.md`, `just leak-check`).

## Honesty / limits

- Item difficulty is **assumed, not calibrated** (a=1, b=0); the synthetic validation therefore
  tests the _estimator_, not real item parameters.
- The recommender does refine per-item difficulty empirically (see MODELS.md §4), but the
  performance score itself uses the assumed parameters for transparency.
