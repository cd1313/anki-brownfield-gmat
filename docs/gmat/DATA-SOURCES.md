# GMAT deck — data sources & attribution

This fork ships/loads practice content derived from third-party datasets. Each is
credited below with its license. Per the CC BY terms, we indicate the source, the
license, and that the data was **modified** (reformatted into Anki notes; see how
in each entry). None of this content is AI-generated.

## Practice MCQs (`GMAT::Practice`)

### Quantitative — AQuA-RAT

- **Source:** AQuA-RAT (Algebra Question Answering with Rationales), Ling et al., 2017 —
  <https://github.com/google-deepmind/AQuA>
- **License:** Apache License 2.0.
- **Use / modifications:** algebra word-problem MCQs mapped into the "GMAT MCQ" note type
  (`Question`, `A`–`E`, `Answer`, `Explanation` from the rationale); option letter prefixes
  stripped. Tagged `GMAT::Quant`. The full converted bank is kept at
  `data/gmat/aqua_mcq.csv` (gitignored); ~2000 are loaded into the deck.

### Verbal — CosmosQA

- **Source:** CosmosQA (Huang et al., 2019) — <https://huggingface.co/datasets/allenai/cosmos_qa>
- **License:** CC BY 4.0.
- **Use / modifications:** 4-option reading-comprehension MCQs mapped into the "GMAT MCQ"
  note type (`context`+`question` → `Question`, `answer0–3` → `A`–`D`, `label` → `Answer`).
  Tagged `GMAT::Verbal`. _Note: this is commonsense RC, used as a reading-comprehension proxy —
  not official GMAT verbal content._

### Data Insights — TabFact

- **Source:** TabFact (Chen et al., ICLR 2020) — <https://github.com/wenhuchen/Table-Fact-Checking>
- **License:** CC BY 4.0.
- **Use / modifications:** table + statement items rendered as True/False (2-option) MCQs in the
  "GMAT MCQ" note type (table → HTML in `Question`; `entailed`/`refuted` → `Answer`). Tagged
  `GMAT::DataInsights`. Covers the **Table Analysis** DI question type only.

## Term flashcards (`GMAT::Terms`)

- **Quantitative (95) and Data Insights (69) terms:** concise, standard definitions compiled from
  general mathematical/statistical knowledge and public GMAT syllabus references (Focus Edition,
  geometry excluded). Not copied verbatim from any single copyrighted source; review for accuracy.
- **Verbal terms:** imported from a downloaded GMAT `.apkg`; confirm that deck's license before
  redistribution.

## Anki

Built on **Anki** (Ankitects Pty Ltd and contributors), **AGPL-3.0-or-later**; some components are
BSD-3-Clause. This fork is likewise AGPL-3.0-or-later.
