# GMAT deck — data sources & attribution

This fork ships/loads practice content derived from third-party datasets. Each is
credited below with its license. Per the CC BY terms, we indicate the source, the
license, and that the data was **modified** (reformatted into Anki notes; see how
in each entry). None of this content is AI-generated.

The shipped deck (`qt/aqt/data/gmat/gmat.apkg`) currently contains:

| Deck                           | Cards | Source(s)                                                              |
| ------------------------------ | ----- | ---------------------------------------------------------------------- |
| `GMAT::Practice::Quant`        | 2,265 | AQuA-RAT (~2,000) + TestPrepReview Problem Solving (265)               |
| `GMAT::Practice::Verbal`       | 850   | TestPrepReview (SC 485, CR 247, RC 118)                                |
| `GMAT::Practice::DataInsights` | 2,436 | TabFact Table Analysis (2,000) + TestPrepReview Data Sufficiency (436) |
| `GMAT::Terms::Quant`           | 95    | compiled definitions (see below)                                       |
| `GMAT::Terms::DataInsights`    | 69    | compiled definitions (see below)                                       |
| `GMAT::Terms::Verbal`          | 1,341 | imported `.apkg` (see below)                                           |

All practice items use the "GMAT MCQ" note type (`Question`, `A`–`E`, `Answer`,
`Explanation`) and are tagged by section and question type (e.g.
`GMAT::Quant::Arithmetic`, `GMAT::Verbal::SentenceCorrection`,
`GMAT::DataInsights::DataSufficiency`).

## Practice MCQs (`GMAT::Practice`)

### Quantitative — AQuA-RAT + TestPrepReview

- **AQuA-RAT** (Algebra Question Answering with Rationales), Ling et al., 2017 —
  <https://github.com/google-deepmind/AQuA>. **License:** Apache License 2.0.
  Algebra/arithmetic word-problem MCQs mapped into the "GMAT MCQ" note type
  (`Question`, `A`–`E`, `Answer`, `Explanation` from the rationale); option letter
  prefixes stripped. Tagged `GMAT::Quant` with subtopic tags (`::Arithmetic`,
  `::Algebra`, `::Geometry`, `::Probability`). The full converted bank (~549k rows)
  is kept at `data/gmat/aqua_mcq.csv` (gitignored); ~2,000 are loaded into the deck.
- **Problem Solving (265)** — from TestPrepReview free GMAT questions (see the shared
  source note below). Tagged `GMAT::Quant::ProblemSolving`. No rationale (empty
  `Explanation`).

### Verbal — TestPrepReview

All 850 Verbal practice items come from TestPrepReview free GMAT questions (see the
shared source note below), tagged by question type:

- **Sentence Correction (485)** — `GMAT::Verbal::SentenceCorrection`
- **Critical Reasoning (247)** — `GMAT::Verbal::CriticalReasoning`
- **Reading Comprehension (118)** — `GMAT::Verbal::ReadingComprehension` (full
  academic-passage RC, multiple questions per passage)

### Data Insights — TabFact + TestPrepReview

- **Table Analysis (2,000)** — **TabFact** (Chen et al., ICLR 2020) —
  <https://github.com/wenhuchen/Table-Fact-Checking>. **License:** CC BY 4.0.
  Table + statement items rendered as True/False (2-option) MCQs (table → HTML in
  `Question`; `entailed`/`refuted` → `Answer`). Tagged
  `GMAT::DataInsights::TableAnalysis`.
- **Data Sufficiency (436)** — from TestPrepReview free GMAT questions (see below).
  Standard 5-option DS format. Tagged `GMAT::DataInsights::DataSufficiency`.

### Shared source — TestPrepReview free GMAT questions

The Verbal (SC/CR/RC), Data Insights Data Sufficiency, and Quant Problem Solving items
above are all drawn from the **free downloadable GMAT practice questions published by
TestPrepReview.com** (<https://www.testprepreview.com/gmat_practice.htm>). The
converted bank (1,551 rows) is at `data/gmat/gmat_prep_mcq.csv` (gitignored).

- **Use / modifications:** questions and answer options reformatted into the "GMAT MCQ"
  note type; correct-answer letter placed in `Answer`; no explanations were provided in
  the source, so `Explanation` is empty. Tagged by section/question type as listed above.
- **License / redistribution:** these are free-to-download practice materials provided
  by TestPrepReview for personal study.

## Term flashcards (`GMAT::Terms`)

- **Quantitative (95) and Data Insights (69) terms:** concise, standard definitions compiled from
  general mathematical/statistical knowledge and public GMAT syllabus references (Focus Edition,
  geometry excluded). Not copied verbatim from any single copyrighted source; review for accuracy.
- **Verbal terms (1,341):** imported from a downloaded GMAT `.apkg`; confirm that deck's license
  before redistribution.

## Anki

Built on **Anki** (Ankitects Pty Ltd and contributors), **AGPL-3.0-or-later**; some components are
BSD-3-Clause. This fork is likewise AGPL-3.0-or-later.
