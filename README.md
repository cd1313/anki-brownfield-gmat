# Anki — GMAT Focus Edition

A fork of [Anki](https://apps.ankiweb.net) turned into a focused **GMAT study app** for
desktop and Android. It keeps Anki's spaced-repetition core and adds a GMAT layer on top: an
objectively-graded practice mode, an honest per-section readiness dashboard, and an adaptive
question recommender — all computed in Anki's shared Rust engine so the desktop app and the
Android companion behave identically.

On top of that engine sits an **optional** AI study layer: semantic grading of typed term recall, a
first-person "study peer" that explains questions you miss, and a "Correct the Peer" practice game.
It is **off by default** and uses **your own** OpenAI key — every AI call is grounded in the card's
own content and fails safe, so the app works fully offline with AI switched off. The readiness
**scores** themselves stay AI-free: they come only from your review history and a transparent
statistical model, never from a language model.

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

Each score has a range and/or a **give-up rule**: below a data threshold it shows _"Not enough
data yet"_ instead of a misleading number.

- **Memory** (term recall, from FSRS retrievability), reported two ways:
  - **Practiced** — recall over the cards you've actually reviewed, shown **with a range** (the
    10th–90th percentile of per-card recall).
  - **Category** — coverage-aware recall over the _whole_ section (unreviewed cards count as 0),
    shown as a single number, so studying 5 of 500 cards can't read as "100% ready."
  - A term counts as **mastered** only if recall ≥ 0.8 **and** the last review was answered within
    20 s (timed, like the real exam).
  - _Give-up:_ needs ≥ **10** graded reviews **and** ≥ **5** distinct reviewed cards.
- **Performance** — per-section ability **θ** under an IRT **3PL** model, estimated by EAP from your
  timed MCQ answers. _Give-up:_ needs ≥ **20** answered MCQs and an ability standard error ≤ **0.7**.
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

### 4. Optional AI study layer (bring your own key)

A single **"AI features"** switch on the dashboard turns on an LLM study layer. It's **off by
default**, needs your own `OPENAI_API_KEY` (copy `.env.example` → `.env`; model defaults to
`gpt-4o-mini`, overridable via `GMAT_AI_MODEL`), and stays out of the way otherwise — the readiness
scores never touch it. Three features:

- **Semantic term grading.** With AI on, `GMAT::Terms` cards get a typed-recall box. When you
  answer, an LLM grades your typed answer for **meaning** (not exact string match) against the
  card's own answer field, returns a verdict (correct / partial / incorrect), **recommends** an
  Anki/FSRS button (Again/Hard/Good/Easy), and writes a 1–2 sentence rationale. The recommended
  button is highlighted, but **you** click to advance.
- **Study peer.** On a **wrong** practice MCQ, a first-person "study peer" explains the mistake,
  grounded only in the card's stored explanation — with a small, sanitized inline SVG diagram when a
  picture genuinely helps.
- **Correct the Peer.** A reciprocal-teaching game: the AI role-plays a peer who solves a practice
  question with a plausible-but-wrong answer; you critique it; the AI judges your critique, only
  crediting you when you both give the right answer **and** explain why the peer was wrong.

**Grounded and fail-safe by design.** Every prompt is anchored to the named card's content (the
model is told not to override it with outside knowledge), all calls run off the UI thread, and any
missing key / network error / bad response falls back to normal self-rating — the app never breaks
because AI is unavailable.

**Evaluated honestly, against a baseline, behind a gate.** `tools/gmat_eval/run_ai_eval.py`
(`just eval-ai`) scores the AI features against small human-labelled gold sets and writes
[docs/gmat/AI-EVAL-RESULTS.md](docs/gmat/AI-EVAL-RESULTS.md). It's reproducible offline from a
committed response cache (temperature 0, no key needed to re-run).

- **Headline:** term-grader verdict accuracy **~85%** with a **2.8%** false-pass rate.
- **Beats a simpler method:** side-by-side with two model-free lexical graders (keyword-overlap and
  fuzzy-string), each tuned on the same set for its best case, the LLM wins by **+17.6 pts** accuracy
  and **−25 pts** false-pass — far fewer wrong answers waved through.
- **Pre-registered ship gate:** cutoffs fixed _before_ the numbers (verdict accuracy ≥ 75%,
  false-pass ≤ 10%, must beat the best baseline); `just eval-ai` exits nonzero if the grader misses
  them, so the eval gates the feature instead of just describing it.
- **An honest negative:** on the tiny (n=24) critique set, the simple length rule actually ties/edges
  the LLM judge — reported as-is, not hidden.

These are measured against small hand-authored sets, **not** real student data or exam outcomes — see
the report's honesty notes.

**What we deliberately skipped (for now):** no AI-generated cards (content risk + prompt-injection
surface), no chatbot, no LLM anywhere in the readiness scores (kept statistical and auditable), and
no vector-search baseline in the eval (it needs an embedding model/key; keyword overlap is the
standard no-model baseline). AI is desktop-only this milestone.

### 5. Shared Rust engine → identical on phone and desktop

The scoring engine (modes 1–3 above) lives in one Rust module (`rslib/src/gmat/`) behind protobuf
RPCs, so the **AnkiDroid companion** computes the exact same scores and recommendations as the
desktop app. The scores are explicit about their limits (item difficulty is assumed/observed, not
professionally calibrated; the θ→score table is an approximate placeholder) and say so on screen.
The AI layer is a desktop-side add-on; its self-contained HTML/JS + prompts are written so a later
AnkiDroid port can reuse them and only swap the bridge.

---

## Download

Grab a ready-to-run installer from the [**Releases**](../../releases) page — no building required.
Pick the file for your platform:

| Platform              | File                             |
| --------------------- | -------------------------------- |
| macOS (Apple Silicon) | `installer-macos-arm` → `.dmg`   |
| macOS (Intel)         | `installer-macos-intel` → `.dmg` |
| Windows               | `.msi`                           |
| Linux                 | `.tar.zst` bundle                |

These are **unsigned fork builds**, so on first launch macOS Gatekeeper (right-click → Open) or
Windows SmartScreen ("More info" → "Run anyway") may warn you — this is expected.

New releases are produced by the [Fork Release workflow](.github/workflows/fork-release.yml): push a
tag (`git tag v26.05 && git push origin v26.05`) or run it manually from the **Actions** tab, and it
builds every platform and publishes the installers here automatically.

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
6. _(Optional)_ To use the AI study features, copy `.env.example` → `.env`, set your
   `OPENAI_API_KEY`, and flip the **AI features** switch on the dashboard. Leave it off to run
   entirely offline.

> The readiness scores work fully offline with no internet connection. The AI study layer is
> optional and off by default — only it needs a key and network access.

---

## Building from source

Everything is driven by the project [`justfile`](justfile) — run `just --list` to see all recipes.

```sh
just run            # build pylib + qt and launch Anki in development mode
just check          # format, build, lint, and run all tests
just test-rust      # Rust tests (includes the GMAT engine tests)
just test-py        # Python tests (includes pylib/tests/test_gmat.py)
```

### Building the macOS installer (`.dmg`)

The installer is [Briefcase](https://beeware.org/project/projects/tools/briefcase/)-based (config
under [`qt/installer/`](qt/installer/)). Two steps — a one-time template download, then the build:

```sh
# 1. One-time: fetch the macOS app template (a git submodule).
git submodule update --init qt/installer/mac-template

# 2. Build the wheels + app bundle + .dmg (this is what CI runs).
tools/build-installer
```

The finished installer is written to:

```
out/installer/dist/anki-<version>-mac-apple.dmg      # -mac-intel on Intel Macs
```

Then install it with the [macOS install steps](#installing-on-macos-from-the-dmg) above.

**Notes**

- **Requirements:** network access (step 1 clones the template; the build downloads dependencies)
  and the Xcode Command Line Tools (`xcode-select --install`) for macOS packaging.
- **Signing:** by default the app is **ad-hoc signed**, so it runs on your own machine but shows the
  "unidentified developer" prompt elsewhere (right-click → Open to bypass). For a distributable,
  properly-signed build, set `SIGN_IDENTITY` to your Apple Developer ID before running step 2.
- **If step 1 is skipped**, the build (and the `test_installer.py` tests in `just check`) fail with
  _"Unable to clone application template"_ — that just means the template submodule isn't present.
- `tools/build-installer` runs `RELEASE=2 ./ninja installer`. To run the heavy stages separately:
  `./ninja installer:build` (compile the app), then `./ninja installer:package` (wrap the `.dmg`).
- **Other platforms:** the same `tools/build-installer` works on Linux/Windows (initialize the
  matching `linux-template` / `windows-template` submodule instead); the output extension differs.

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
  hooks in `qt/aqt/reviewer.py` and the dashboard hero in `qt/aqt/deckbrowser.py`.
- **Optional AI layer:** `qt/aqt/gmat_ai.py` (provider-agnostic, stdlib-only grader + peer calls),
  `qt/aqt/gmat_peer.py` ("Correct the Peer" dialog), and `.env.example` for the key.
- **AI evals:** `tools/gmat_eval/run_ai_eval.py` (+ committed response cache), run via `just eval-ai`.
- **Tests:** `pylib/tests/test_gmat.py` plus the Rust unit tests in `rslib/src/gmat/mod.rs`.
- **Docs:** `docs/gmat/` (`PRD-wednesday.md`, `MODELS.md`, `DATA-SOURCES.md`, `EVAL-RESULTS.md`,
  `AI-EVAL-RESULTS.md`, …).
- **Android:** GMAT dashboard + reviewer wiring live in the separate AnkiDroid fork.
