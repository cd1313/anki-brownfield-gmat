# Model 1 — Memory (can the student recall this fact right now?)

_One-pager for the submission (spec §12). Engine: `compute_topic_mastery` in
`rslib/src/gmat/mod.rs`; surfaced by `qt/aqt/gmat.py`._

## What it measures

The probability the student **recalls a term flashcard** they have studied, per section.
This is the "Memory" question only — it is deliberately scoped to term cards (`GMAT::Terms`)
and **excludes** MCQ practice, so answering questions never inflates it.

## Method

- Uses **FSRS retrievability** — the shipped spaced-repetition model's current probability of
  recall for each card, computed from that card's own review history (stability + decay +
  time since last review). No new memory model is invented; we read the engine's FSRS state.
- Reported two ways, never blended:
  - **Practiced** — mean retrievability over cards actually reviewed, with a range = the
    10th–90th percentile across those cards.
  - **Category** — coverage-aware recall over the whole section, unreviewed cards counted as
    0 (`category = practiced × coverage`, `coverage = reviewed/total`). Shown **with a range**
    (`practiced_low/high × coverage`) so studying 5 of 500 cards can't read as "100% ready."
- **Time-gated mastery:** a card counts as "mastered" only when retrievability ≥ **0.8** _and_
  the most recent rated review was answered within **20 s** (a speed gate, like the real exam).

## Give-up rule

> **Abstain (show "Not enough data yet") unless the section has ≥ 10 graded reviews AND ≥ 5
> distinct reviewed cards.**

## Evidence

- **Calibration:** `docs/gmat/MEMORY-CALIBRATION.md`. Our retrievability computation is
  validated against the real engine (max abs error ~1e-3), then calibrated on 24,000 held-out
  simulated reviews: **Expected Calibration Error ≈ 0.008**, with observed recall ≈ predicted
  across every probability bin. Reproduce with `just eval-memory`.

## Honesty / limits

- FSRS is pretrained upstream (not fitted on this deck); we do not claim per-user optimization.
- Calibration is shown on **simulated** reviews (validating the retrievability math + the
  calibration pipeline), not real student review streams — that is the honest next step
  (spec Step 4 bonus).
- Coverage is deck-relative; how much of the official outline the deck spans is a separate
  score (see `docs/gmat/COVERAGE.md`).
