# GMAT Focus coverage map (spec §7c)

The dashboard measures how much of the **official GMAT Focus outline** the loaded deck
actually covers, per section, and **abstains from a readiness score** for any section below
the coverage line. A deck that skips a whole high-weight question type must not read as
"ready" — that is exactly what §7c requires.

## The official outline (GMAT Focus Edition, current)

GMAT Focus dropped Sentence Correction, Geometry, and Quant Data Sufficiency; Data
Sufficiency now lives in Data Insights. The outline used
(`GMAT_OUTLINE` in [`qt/aqt/gmat.py`](../../qt/aqt/gmat.py)):

| Section                            | Official question types (topics)                                                                     |
| ---------------------------------- | ---------------------------------------------------------------------------------------------------- |
| **Quantitative** (Problem Solving) | Arithmetic, Algebra                                                                                  |
| **Verbal**                         | Critical Reasoning, Reading Comprehension                                                            |
| **Data Insights**                  | Data Sufficiency, Multi-Source Reasoning, Table Analysis, Graphics Interpretation, Two-Part Analysis |

Each topic maps to the deck search that detects whether it is covered. Section coverage =
(covered topics) / (total topics), computed **live** on the user's actual collection, so it
reflects whatever deck is loaded.

## The give-up (abstain) rule

> **A section shows no readiness score when its outline coverage is below `COVERAGE_ABSTAIN`
> = 50%.** The dashboard displays the coverage % and the missing topics instead.

This is in addition to the data-volume give-up rules (≥20 MCQs, SE ≤ 0.7). Coverage is shown
for every section (even when it passes) so the number behind the score is always visible.

## Coverage of the shipped deck (honest gaps)

With the datasets this fork loads (see [`DATA-SOURCES.md`](DATA-SOURCES.md)):

| Section       | Covered                                                                         | Coverage | Readiness           |
| ------------- | ------------------------------------------------------------------------------- | -------- | ------------------- |
| Quantitative  | Algebra (AQuA); Arithmetic **not** separately present                           | **50%**  | shown (at the line) |
| Verbal        | Critical Reasoning (GMAT-prep) + Reading Comprehension (CosmosQA proxy)         | **100%** | shown               |
| Data Insights | Table Analysis only (TabFact); DS, Multi-Source, Graphics, Two-Part **missing** | **20%**  | **abstains**        |

So Data Insights — where the deck has only 1 of 5 official types — correctly **refuses** a
readiness score rather than projecting one from a fifth of the section. This is the intended
behaviour: honest abstention over a flattering number.

## Honesty notes

- Coverage is measured at the **question-type** granularity the deck can express via tags;
  within-type breadth (e.g. how many arithmetic sub-skills) is not separately verified, and
  the Verbal Reading-Comprehension content is a commonsense-RC **proxy**, not official GMAT
  RC (see DATA-SOURCES.md). The coverage number is therefore an upper bound on true content
  coverage.
- The outline lives in one place (`GMAT_OUTLINE`, `qt/aqt/gmat.py`) and is shared by the
  dashboard's live computation; adding finer tags to the deck automatically sharpens the map.
