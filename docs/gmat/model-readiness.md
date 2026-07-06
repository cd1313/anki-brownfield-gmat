# Model 3 — Readiness (what section score would the student get today?)

_One-pager for the submission (spec §12). Engine: `estimate_readiness_impl` /
`percentile_to_section_score` in `rslib/src/gmat/mod.rs`; surfaced by `qt/aqt/gmat.py`._

## What it measures

A projected **GMAT Focus section score (60–90)** with a range and a confidence label, per
section — the "how ready am I, and how sure are you?" question. It is a **mapping** of the
Performance ability onto the real score scale, adjusted for pacing; it is never a blended
single "% ready" number.

## Method

Combines two separately-measured factors:

- **Accuracy:** ability θ → percentile via `Φ(θ)` → section score via a documented
  percentile→score table (`percentile_to_section_score`). The **range** comes from mapping
  `θ̂ ± SE` through the table.
- **Pacing:** from recorded latencies — `% within budget` and a projected section time
  (median s/item × section length) against the **45-minute** limit; a documented penalty
  reduces the score as projected time exceeds the limit.
- **Confidence** (low / medium / high) is derived from SE(θ) and coverage and shown next to
  the range.

## Give-up rule

> **Abstain unless (a) the Performance gate is met (≥ 20 MCQs, SE(θ) ≤ 0.7) AND enough timed
> responses exist to estimate pacing, AND (b) the section's official-outline coverage ≥ 50%.**
> Below the coverage line the section shows the coverage % and missing topics instead of a
> score — a deck that skips a whole high-weight type must not read as "ready" (spec §7c).

## Every score ships with

point estimate · likely range · % outline coverage · confidence indicator · last-updated
time · the main factors (accuracy + pacing) · the give-up rule above. (Rendered on the
dashboard, `qt/aqt/gmat.py`.)

## Evidence

- Inherits the Performance validation (`docs/gmat/EVAL-RESULTS.md`) for the θ it maps from.
- Coverage abstention is demonstrated in `docs/gmat/COVERAGE.md`: Data Insights (20% of the
  official outline covered) correctly refuses a readiness score.

## Honesty / limits

- The **θ→section-score table is an approximate placeholder**, to be replaced with the exact
  published GMAC percentile curve. The projected score is **not** validated against real exam
  outcomes — that needs students with both study history and practice-test scores (spec Step 4
  bonus). The dashboard says so (low confidence) and so do we.
- This is the model we back **least** strongly, by design: "we calibrated memory but cannot yet
  prove the projected score is right" is the honest position the spec rewards.
