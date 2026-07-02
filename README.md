# Anki — GMAT Focus Edition

A fork of [Anki](https://apps.ankiweb.net) turned into a focused **GMAT study app** for
desktop and Android. It keeps Anki's spaced-repetition core and adds a GMAT layer on top: an
objectively-graded practice mode, an honest per-section readiness dashboard, and an adaptive
question recommender — all computed in Anki's shared Rust engine so the desktop app and the
Android companion behave identically.

There are **no AI features** — no generated cards, no chatbot, no LLM grading. Every score comes
from your own review history and a transparent statistical model.

---

## The exam we're preparing for

**GMAT Focus Edition** — the current version of the GMAT, used for MBA / business-master admissions.

- **Three sections:** Quantitative, Verbal, and Data Insights.
- **45 minutes each**, computer-adaptive (the real exam uses Item Response Theory to choose questions).
- **Section scores 60–90**; total score 205–805 in 10-point steps.

This fork mirrors that structure: content is organized under three section tags
(`GMAT::Quant`, `GMAT::Verbal`, `GMAT::DataInsights`), the readiness model reports per-section
scores on the 60–90 scale, and practice is timed against the 45-minute section budget.

---

## What's new in this fork (beyond stock Anki)

Stock Anki gives you self-graded flashcards and FSRS scheduling. This fork adds a GMAT layer that
stock Anki does **not** have:

### 1. Two study modes — one of them objectively graded
- **Terms** (`GMAT::Terms` deck) — ordinary flashcards you self-grade (Again/Hard/Good/Easy). This
  is the only place self-grading is used, and it feeds the **Memory** score.
- **Practice** (`GMAT::Practice` deck) — multiple-choice questions using a new **"GMAT MCQ"** note
  type (stem, options A–E, stored answer, explanation). You pick an option and the **engine grades
  it objectively** against the stored answer — you never self-grade a practice question. Attempts
  are recorded as non-scheduling entries, so MCQ results **never** contaminate the Memory score.

### 2. GMAT Readiness dashboard — three honest scores, never blended
Each score has a range and/or a **give-up rule**: below a data threshold it shows *"Not enough
data yet"* instead of a misleading number.

- **Memory** (term recall, from FSRS retrievability), reported two ways:
  - **Practiced** — recall over the cards you've actually reviewed, shown **with a range** (the
    10th–90th percentile of per-card recall).
  - **Category** — coverage-aware recall over the *whole* section (unreviewed cards count as 0),
    shown as a single number, so studying 5 of 500 cards can't read as "100% ready."
  - A term counts as **mastered** only if recall ≥ 0.8 **and** the last review was answered within
    20 s (timed, like the real exam).
  - *Give-up:* needs ≥ **10** graded reviews **and** ≥ **5** distinct reviewed cards.
- **Performance** — per-section ability **θ** under an IRT **3PL** model, estimated by EAP from your
  timed MCQ answers. *Give-up:* needs ≥ **20** answered MCQs and an ability standard error ≤ **0.7**.
- **Readiness** — a projected section score (**60–90**) with a range and confidence, combining
  accuracy (θ → score) with a **pacing** check (your median time/question projected across a full
  45-minute section).

### 3. Adaptive practice recommender (IRT-based)
The Practice pool doesn't serve questions at random. Using your current IRT scores it recommends
**weakness-first, at your level**: it prioritizes your lowest-ability section and, within it, picks
questions whose difficulty is near your ability. Item difficulty is a hybrid estimate calibrated
from each question's own answer history (shrunk toward neutral for rarely-seen items), with an
exploration bonus so new questions still surface. With no data yet it falls back to a plain random
draw — honest by construction.

### 4. Shared Rust engine → identical on phone and desktop
All of the above lives in one Rust module (`rslib/src/gmat/`) behind protobuf RPCs, so the
**AnkiDroid companion** computes the exact same scores and recommendations as the desktop app. The
scores are explicit about their limits (item difficulty is assumed/observed, not professionally
calibrated; the θ→score table is an approximate placeholder) and say so on screen.

---

## Installing on macOS (from the `.dmg`)

1. Open the `Anki.dmg` file.
2. Drag **Anki** into your **Applications** folder.
3. Launch it from Applications. Because this is an unsigned fork build, macOS may warn that it's
   from an unidentified developer — **right-click the app → Open → Open** to run it the first time
   (after that it opens normally).
4. Load a GMAT deck (import a `.apkg`/`.colpkg`, or open your existing collection) and **enable
   FSRS** in the deck options — the Memory score reads FSRS memory state, so scores appear only once
   FSRS is on and you've done some reviews.
5. Open the dashboard from the **GMAT Readiness** menu item.

> The app runs fully offline with no AI: it shows your scores with no internet connection.

---

## Building from source

Everything is driven by the project [`justfile`](justfile) — run `just --list` to see all recipes.

```sh
just run            # build pylib + qt and launch Anki in development mode
just check          # format, build, lint, and run all tests
just test-rust      # Rust tests (includes the GMAT engine tests)
just test-py        # Python tests (includes pylib/tests/test_gmat.py)
```

**Building the macOS `.dmg`:** the installer is
[Briefcase](https://beeware.org/project/projects/tools/briefcase/)-based under
[`qt/installer/`](qt/installer/) (per-platform templates for mac/linux/windows). Build the wheels
with `just wheels`, then run Briefcase against the `mac-template` to produce the `.dmg` package.
See [`qt/installer/README.md`](qt/installer/README.md).

**Android companion:** the phone app is a separate
[AnkiDroid](https://github.com/ankidroid/Anki-Android) fork that embeds this repo's Rust backend
(built via `Anki-Android-Backend`), so the GMAT engine is shared rather than reimplemented.

---

## License & attribution

This project is a fork of **[Anki](https://github.com/ankitects/anki)** by Ankitects Pty Ltd
(Damien Elmes) and contributors, distributed under the **GNU AGPL-3.0-or-later** license (see
[LICENSE](./LICENSE)). All original Anki copyrights and the AGPL terms are retained; the GMAT
additions in this fork are released under the same license.

Upstream Anki: <https://apps.ankiweb.net> · developer docs: <https://dev-docs.ankiweb.net> ·
contributors: [CONTRIBUTORS](./CONTRIBUTORS)

### Files this fork adds/changes on top of Anki
- **New GMAT engine:** `proto/anki/gmat.proto`, `rslib/src/gmat/` (`mod.rs`, `service.rs`),
  registered in `rslib/src/lib.rs`.
- **Desktop UI:** `qt/aqt/gmat.py` (dashboard, "GMAT MCQ" note type/template, practice pool), with
  hooks in `qt/aqt/reviewer.py`.
- **Tests:** `pylib/tests/test_gmat.py` plus the Rust unit tests in `rslib/src/gmat/mod.rs`.
- **Docs:** `docs/gmat/` (`PRD-wednesday.md`, `MODELS.md`, `DATA-SOURCES.md`, `EVAL-RESULTS.md`, …).
- **Android:** GMAT dashboard + reviewer wiring live in the separate AnkiDroid fork.
