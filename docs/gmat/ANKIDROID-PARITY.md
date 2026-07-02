# AnkiDroid full parity — implementation & verification

Carries the GMAT features to the phone. The three sibling repos under `/Users/cat/alpha-projects/`:

- `anki-brownfield-gmat` — this fork (shared Rust engine + desktop).
- `Anki-Android-Backend` — rsdroid: compiles `rslib` → Android `.so` + generates `GeneratedBackend.kt`.
- `Ankidroid-brownfield-gmat` — the Kotlin app.

## Track 1 — Engine parity (done, verified)

rsdroid was rebuilt against this fork, so the four `gmat` RPCs auto-generate into
`GeneratedBackend.kt`:

- `getTopicMastery(search, tagPrefix, rThreshold, timeBudgetSecs, minReviews, minCards) → List<TopicMastery>`
- `gradeMcq(cardId, chosen) → GradeMcqResponse{correct, correctAnswer}`
- `nextPracticeCard(search, cycle) → NextPracticeCardResponse{cardId, exhausted, remaining}`
- `markPracticeDone(cardId, cycle) → OpChanges`

AnkiDroid runs on the rebuilt backend (`local_backend=true`), confirmed on the Pixel_10 emulator.
The whole collection (decks, notes, the "GMAT MCQ" note type + template, tags, FSRS state,
`custom_data`) carries over unchanged, so native flashcard review of `GMAT::Terms` works with zero
Kotlin changes.

## Track 2 — Kotlin ports

All three ports **compile** (`:AnkiDroid:compilePlayDebugKotlin` green) and are wired into the
**new study screen** (`ui/windows/reviewer/`), which AnkiDroid gates behind the
`newReviewerOptions` preference — it must be **on** for the reviewer-side features. (The legacy
`com.ichi2.anki.Reviewer` uses a different, contract-gated `/jsapi` bridge and is not targeted.)

### 1. Memory dashboard — VERIFIED on device

New `GmatDashboardActivity` (launched from the DeckPicker overflow → "GMAT Memory") calls
`getTopicMastery` via `withCol { backend... }` and renders per-section score + range + give-up state.
On the emulator it showed, live from the shared engine:

```
GMAT::Verbal      Memory: 98%  (range 98–98%)   20/20 reviewed · 20 mastered
GMAT::Quant       Not enough data yet (0/95 reviewed)
GMAT::DataInsights Not enough data yet (0/69 reviewed)
Memory only — practice (MCQ) results are excluded.
```

This proves the RPC + the honest score, range, and give-up rule end-to-end on the phone.
Files: `GmatDashboardActivity.kt`, `AndroidManifest.xml`, `res/menu/deck_picker.xml`, `DeckPicker.kt`.

### 2. Pool mode (no-FSRS, random-without-replacement) — VERIFIED on device

`ReviewerViewModel` intercepts `updateCurrentCard()`: when the studied deck is `GMAT::Practice`
(`isGmatPractice()`), it serves cards from `backend.nextPracticeCard(search, cycle)` instead of the
scheduler queue, loads the card by id, and shows it. `answerCardInternal()` and `showAnswerInternal()`
early-return in pool mode, so FSRS is never touched; the whole answer area (Show Answer + ease
buttons) is also hidden for practice cards via `hideAnswerAreaFlow` (`ReviewerFragment` toggles
`binding.answerArea.isVisible`), while `GMAT::Terms` flashcards keep the Show Answer button. Verified
on the emulator: practice cards show no answer bar, term cards still show "Show answer".
`gmatPracticeContinue()` marks the current card done (`markPracticeDone`) and serves the next; when the
pool is exhausted it bumps the cycle counter (`config` key `gmat_practice_cycle`) and finishes, so the
next entry reshuffles the full pool. Mirrors the desktop pool mode in `qt/aqt/gmat.py`.

On the emulator, studying `GMAT::Practice` logged `updateGmatPracticeCard` and rendered a practice MCQ
with the counts bar at `0 0 0` (no scheduler queue → no FSRS), confirming the engine-driven draw.

### 3. MCQ objective grading bridge — compiled + wired

The stored "GMAT MCQ" template is now platform-aware: on AnkiDroid (`globalThis.ankiPlatform ===
"ankidroid"`, where `pycmd` is neutralized) option clicks `POST /ankidroid/gmatGradeMcq` and Continue
`POST /ankidroid/gmatPracticeContinue`; on desktop they still use `pycmd`. `ReviewerViewModel.handlePostRequest`
dispatches `gmatGradeMcq` → `backend.gradeMcq(cardId, chosen)` and returns `{correct, correctAnswer}`
for the template to reveal inline. Objective grading itself is proven on desktop (Rust + Python tests,
`just check` green); the Kotlin handler compiles and is dispatched.

**Deployment note (only gap on device):** the deck currently on the emulator was imported from an
`.apkg` exported _before_ the template became platform-aware, so its cards still carry the old `pycmd`
template (a no-op on AnkiDroid) and taps don't grade yet. To exercise grading + the cycle on the phone,
re-deploy the template: launch desktop Anki once (`ensure_mcq_notetype` refreshes the stored template
on startup), re-export `GMAT::Practice`, then re-import on the phone (or sync). No code change needed.
The emulator (`adb root` denied on the Play image; `run-as` can't reach external storage) can't be
hot-patched, hence the re-import.

## Environment / repro

```
export ANDROID_HOME="$HOME/Library/Android/sdk"
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd Ankidroid-brownfield-gmat
./gradlew :AnkiDroid:assemblePlayDebug          # arm64 APK under AnkiDroid/build/outputs/apk/play/debug/
adb install -r -d AnkiDroid/build/outputs/apk/play/debug/AnkiDroid-play-arm64-v8a-debug.apk
# Enable the new study screen: Settings → Reviewer → New reviewer (pref key newReviewerOptions)
```

Files touched in `Ankidroid-brownfield-gmat`: `GmatDashboardActivity.kt` (new),
`AndroidManifest.xml`, `res/menu/deck_picker.xml`, `DeckPicker.kt`,
`ui/windows/reviewer/ReviewerViewModel.kt`, plus the earlier `libanki/.../Deck.kt` drift fix.
Desktop template change: `qt/aqt/gmat.py` (`_MCQ_FRONT` made platform-aware).
