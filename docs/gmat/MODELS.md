# GMAT model descriptions

Three separate scores, each with a range and a give-up rule (spec §4). Memory ships
Wednesday; performance + readiness are the IRT work. Nothing is blended into one number.

> **Submission one-pagers (spec §12):** [`model-memory.md`](model-memory.md),
> [`model-performance.md`](model-performance.md), [`model-readiness.md`](model-readiness.md) —
> a self-contained page per model with its give-up rule and evidence. This file is the longer
> combined reference.

## 1. Memory (what you can recall now)

- **What:** per-section FSRS **retrievability** of term flashcards (`GMAT::Terms`), i.e. the
  current probability of recall. Engine: `compute_topic_mastery` in `rslib/src/gmat/mod.rs`.
- **Two scores, reported separately (never blended):**
  - **Practiced** — mean recall over the cards you've **reviewed** ("how well you know what you've
    studied"), shown **with a range** = the 10th–90th percentile of per-card retrievability across
    reviewed cards.
  - **Category** — coverage-aware recall over the **whole section** with unreviewed cards counted as
    0 (`= practiced × coverage`). Shown **with a range**: the practiced p10–p90 band scaled by the
    same coverage factor (`category_low/high = practiced_low/high × coverage`), so the displayed
    uncertainty tracks the studied-recall spread rather than pretending to a single exact number.
- **Time-aware mastery:** a card counts as "mastered" only when retrievability ≥ 0.8 **and** the
  most recent rated review was answered within a 20 s budget.
- **Give-up:** abstain unless ≥ 10 graded reviews **and** ≥ 5 distinct reviewed cards.
- **Excludes** MCQ practice entirely (scoped to `GMAT::Terms`).

## 2. Performance (can you answer a new question)

- **What:** per-section ability **θ** under an IRT **3PL** model, estimated from logged MCQ
  responses. Engine: `estimate_readiness_impl` in `rslib/src/gmat/mod.rs`.
- **Model:** `P(correct | θ) = c + (1−c)/(1 + e^(−D·a·(θ−b)))`, `D = 1.702`. Item parameters are
  **fixed/assumed**: discrimination `a = 1`, difficulty `b = 0`, guessing `c = 1/#choices`
  (Quant 0.20, Verbal 0.25, Data Insights 0.50). **Difficulty is not calibrated** — no per-item
  labels exist in our content (AQuA/CosmosQA/TabFact). Item invariance (BrainLift Subcat 3.1) is
  what would let calibrated parameters transfer if we had them.
- **Estimation:** **EAP** over a fixed θ grid with an `N(0,1)` prior; θ̂ = posterior mean, and the
  posterior SD is the honest standard error. EAP stays finite for all-correct/all-wrong and few
  items (unlike MLE), and its SD drives the range and give-up.
- **Shown with a range:** the dashboard reports the ability as **θ̂ ± SE** and an **accuracy band**
  obtained by mapping `θ̂ ± SE` through the same 3PL (per-section guessing `c`), so the performance
  score carries its uncertainty rather than a bare point number.
- **Accuracy-only:** response times are recorded but do **not** enter θ (keeps the §7d paraphrase
  test — recall vs accuracy — clean). Timing is a separate readiness factor (below).
- **Responses** are logged by `grade_mcq` as non-scheduling "cramming" revlog entries
  (`is_cramming()`), so they never touch FSRS/scheduling/the memory score.
- **Give-up:** abstain unless ≥ N responses, ≥ C coverage (attempted/available), and SE(θ) ≤ S.
  (Defaults used by callers; state exact values in the README.)
- **Validation:** `tools/gmat_eval/run_eval.py` drives the real engine on simulated students —
  θ recovery corr ≈ 0.93 (bias ≈ 0), and held-out prediction beats base-rate baselines on
  log-loss, with a zero-leakage check. See `docs/gmat/EVAL-RESULTS.md`.

## 3. Readiness (projected section score)

- **What:** projected GMAT Focus **section score (60–90)** with a range and confidence, combining
  **two separately-measured factors** (BrainLift Subcat 1.3):
  - **Accuracy:** θ → percentile via `Φ(θ)` → section score via a documented percentile→score
    table (`percentile_to_section_score`). Range from `θ̂ ± SE` mapped through the table.
  - **Pacing:** from recorded latencies — `% within budget` and a **projected section time**
    (median seconds/item × section length) vs the 45-min limit. A documented penalty reduces the
    score as projected time exceeds the limit.
- **Confidence:** low/medium/high from SE(θ) + coverage.
- **Give-up:** same thresholds as performance, plus enough timed responses to estimate pacing.
- **Honesty:** the θ→score table is an **approximate placeholder** (to be replaced with the exact
  GMAC published percentiles), and the projected score is **not** validated against real exam
  outcomes (needs held-out students; spec Step 4 bonus). We say so on the screen (low confidence)
  and here.

## 4. Recommendation (adaptive practice selection)

- **What:** which MCQ the practice pool serves next. Engine: `next_practice_card_impl` in
  `rslib/src/gmat/mod.rs` (the desktop pool and the AnkiDroid reviewer both call this one RPC).
- **Objective — weakness-first, at your level.** Each candidate is scored
  `WEAKNESS_WEIGHT·(−θ_section) − |b − (θ_section + δ)| + explore`, and the argmax is served (a tiny
  random jitter breaks ties). The section term dominates, so the **weakest section** (lowest IRT
  ability θ, from §2's `eap_ability`) is prioritised; within it, items whose difficulty `b` sits near
  the student's ability (plus a small **desirable-difficulty** offset `δ = +0.5` logit) are preferred.
- **Hybrid item difficulty `b`** (`empirical_difficulty`): estimated from each item's **own** logged
  attempts — invert the 3PL at the section ability, `b_obs = θ + ln((1−p̂)/(p̂−c))/D` (clamped for
  at/below-chance items) — then **shrunk toward the neutral prior `b₀ = 0`** by `K = 4`
  pseudo-observations (empirical Bayes). Unattempted items are therefore ≈ neutral; well-attempted
  items reflect their observed difficulty. This is the calibration the model previously lacked (§2),
  applied per item rather than fixed at `b = 0`.
- **Exploration:** unattempted items get a bonus (`EXPLORE_BONUS/(1+n)`) so new questions still
  surface — which is also what _generates_ the attempts the hybrid estimate needs.
- **Honesty / limits:** difficulty is **provisional** — approximated at the current section ability,
  not a full item/person co-calibration, and single-item counts are noisy (hence the shrinkage). With
  no response data every θ ≈ 0 and every `b ≈ 0`, so selection is **flat → an unbiased random draw**;
  the recommender only sharpens as attempts accumulate. Read-only (no writes); an empty `tag_prefix`
  disables the weighting entirely (pure random pool, the pre-recommendation behaviour).

## Shared-engine note

All three live in `rslib` (the `gmat` module) behind protobuf RPCs, so desktop and the AnkiDroid
build compute identical scores. Reads are undo-safe; the only write (`grade_mcq` response logging)
is an undo-aware, non-scheduling revlog entry.
