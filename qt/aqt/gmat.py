# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT memory dashboard (Wednesday milestone, Deliverable 4).

Surfaces the read-only ``get_topic_mastery`` backend RPC (Deliverable 2a) as a
simple per-section memory score with an honest range and give-up rule.

Memory only, term flashcards only -- MCQ practice / performance / readiness are
deliberately excluded here.
"""

from __future__ import annotations

import json
import time

import aqt
from aqt.qt import *
from aqt.utils import disable_help_button, restoreGeom, saveGeom, tooltip

# Give-up rule + mastery thresholds (see PRD "Thresholds" open task; tweak freely).
R_THRESHOLD = 0.8  # retrievability >= this counts toward "mastered"
TIME_BUDGET_SECS = 20  # most-recent rated review must be within this to count
# (kept below Anki's ~60s answer-time cap so the speed gate is actually active:
# a correct-but-slow recall won't count as "mastered")
MIN_REVIEWS = 10  # a section is scored only after >= this many graded reviews ...
MIN_CARDS = 5  # ... and >= this many distinct reviewed cards

# The term-card universe (excludes MCQ practice) and the canonical GMAT sections
# we always display, so empty sections still show an honest "no data" state.
TERMS_SEARCH = 'deck:"GMAT::Terms"'
TAG_PREFIX = "GMAT"
SECTIONS = [
    ("GMAT::Quant", "Quantitative"),
    ("GMAT::Verbal", "Verbal"),
    ("GMAT::DataInsights", "Data Insights"),
]


class GmatMemoryDialog(QDialog):
    def __init__(self, mw: aqt.AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("GMAT Memory")
        disable_help_button(self)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Memory score per GMAT section, from term flashcards only "
            "(FSRS retrievability, gated by recall speed). Coverage-aware: it is "
            "your average recall across the whole section, so cards you haven't "
            "reviewed pull it down. No score is shown until a section has enough "
            "review data."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.table = QTableWidget(len(SECTIONS), 5, self)
        self.table.setHorizontalHeaderLabels(
            ["Section", "Memory score", "Likely range", "Reviewed / Total", "Status"]
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(self.table)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox()
        refresh = buttons.addButton("Refresh", QDialogButtonBox.ButtonRole.ActionRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        qconnect(refresh.clicked, self.refresh)
        qconnect(close.clicked, self.close)
        layout.addWidget(buttons)

        restoreGeom(self, "GmatMemoryDialog", default_size=(680, 320))
        self.refresh()

    def refresh(self) -> None:
        col = self.mw.col
        if not col:
            return
        results = col._backend.get_topic_mastery(
            search=TERMS_SEARCH,
            tag_prefix=TAG_PREFIX,
            r_threshold=R_THRESHOLD,
            time_budget_secs=TIME_BUDGET_SECS,
            min_reviews=MIN_REVIEWS,
            min_cards=MIN_CARDS,
        )
        by_topic = {t.topic: t for t in results}

        for row, (topic, label) in enumerate(SECTIONS):
            t = by_topic.get(topic)
            if t is None:
                cells = [label, "Not enough data yet", "", "0 / 0", "No cards"]
            elif not t.has_score:
                cells = [
                    label,
                    "Not enough data yet",
                    "",
                    f"{t.reviewed_cards} / {t.total_cards}",
                    f"Needs ≥{MIN_REVIEWS} reviews & ≥{MIN_CARDS} cards",
                ]
            else:
                low = t.retrievability_low * 100
                high = t.retrievability_high * 100
                cells = [
                    label,
                    f"{t.mean_retrievability * 100:.0f}%",
                    f"{low:.0f}–{high:.0f}%",
                    f"{t.reviewed_cards} / {t.total_cards}",
                    f"{t.mastered_cards} mastered",
                ]
            for col_idx, text in enumerate(cells):
                self.table.setItem(row, col_idx, QTableWidgetItem(text))

        updated = time.strftime("%Y-%m-%d %H:%M:%S")
        self.status_label.setText(
            f"Last updated: {updated}    ·    "
            f"Give-up rule: a section is scored only after ≥{MIN_REVIEWS} graded "
            f"reviews and ≥{MIN_CARDS} reviewed cards.    ·    "
            "Memory only — MCQ practice is excluded."
        )

    def closeEvent(self, evt: QCloseEvent | None) -> None:
        saveGeom(self, "GmatMemoryDialog")
        super().closeEvent(evt)


# Module-level reference so the modeless dialog isn't garbage-collected.
_dialog: GmatMemoryDialog | None = None


def show_gmat_memory(mw: aqt.AnkiQt) -> None:
    global _dialog
    _dialog = GmatMemoryDialog(mw)
    _dialog.show()


def _create_mcq_notetype(mw: aqt.AnkiQt) -> None:
    if not mw.col:
        return
    ensure_mcq_notetype(mw.col)
    tooltip(f'"{MCQ_NOTETYPE_NAME}" note type is ready.', parent=mw)


def setup_gmat_menu(mw: aqt.AnkiQt) -> None:
    """Add the GMAT entries under the Tools menu."""
    memory = QAction("GMAT Memory", mw)
    qconnect(memory.triggered, lambda: show_gmat_memory(mw))
    mw.form.menuTools.addAction(memory)

    mcq = QAction("Create GMAT MCQ Note Type", mw)
    qconnect(mcq.triggered, lambda: _create_mcq_notetype(mw))
    mw.form.menuTools.addAction(mcq)


# --- MCQ practice mode (Deliverable 2b) --------------------------------------
#
# A "GMAT MCQ" note type renders the question with clickable option buttons. The
# correct answer lives in the (non-rendered) Answer field; clicking an option is
# graded objectively by the Rust `grade_mcq` RPC, then recorded through Anki's
# normal (undo-safe) answer path -- the user never self-grades.

MCQ_NOTETYPE_NAME = "GMAT MCQ"
_MCQ_FIELDS = ["Question", "A", "B", "C", "D", "E", "Answer", "Explanation"]

# --- Practice pool (no-FSRS, random-without-replacement) ---------------------
# Studying GMAT::Practice serves MCQs from a pool via the Rust engine
# (NextPracticeCard / MarkPracticeDone). Each answered card is removed for the
# cycle; when the pool empties we show a "cycle complete" screen and reset on the
# next start. No FSRS: pool cards are never answer_card'd.
PRACTICE_DECK = "GMAT::Practice"
PRACTICE_SEARCH = f'deck:"{PRACTICE_DECK}" note:"{MCQ_NOTETYPE_NAME}"'
_CYCLE_KEY = "gmat_practice_cycle"
_CYCLE_DONE_KEY = "gmat_practice_cycle_done"

_MCQ_FRONT = """\
<div class="gmat-q">{{Question}}</div>
<div class="gmat-opts">
  {{#A}}<button class="gmat-opt" data-letter="A" onclick="gmatChoose('A')"><b>A.</b> {{A}}</button>{{/A}}
  {{#B}}<button class="gmat-opt" data-letter="B" onclick="gmatChoose('B')"><b>B.</b> {{B}}</button>{{/B}}
  {{#C}}<button class="gmat-opt" data-letter="C" onclick="gmatChoose('C')"><b>C.</b> {{C}}</button>{{/C}}
  {{#D}}<button class="gmat-opt" data-letter="D" onclick="gmatChoose('D')"><b>D.</b> {{D}}</button>{{/D}}
  {{#E}}<button class="gmat-opt" data-letter="E" onclick="gmatChoose('E')"><b>E.</b> {{E}}</button>{{/E}}
</div>
<div id="gmat-status"></div>
<button id="gmat-continue" style="display:none" onclick="gmatContinue()">Continue</button>
<script>
// AnkiDroid sets globalThis.ankiPlatform = "ankidroid" and neutralizes pycmd, so the
// same stored template must route option clicks to the local server instead. Both
// platforms grade objectively in the shared Rust engine (grade_mcq); only the
// transport differs (desktop pycmd vs AnkiDroid POST /ankidroid/...).
var GMAT_DROID = (typeof globalThis !== 'undefined' && globalThis.ankiPlatform === 'ankidroid');
function gmatChoose(l) {
  if (GMAT_DROID) { gmatChooseDroid(l); } else { pycmd('gmat_mcq:' + l); }
}
function gmatChooseDroid(l) {
  fetch('ankidroid/gmatGradeMcq', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chosen: l })
  }).then(function (r) { return r.json(); })
    .then(function (res) { gmatReveal(l, res.correct, res.correctAnswer); })
    .catch(function (e) { console.log('gmat grade failed', e); });
}
function gmatContinue() {
  if (GMAT_DROID) {
    fetch('ankidroid/gmatPracticeContinue', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }).catch(function (e) { console.log('gmat continue failed', e); });
  } else { pycmd('gmat_mcq_continue'); }
}
function gmatReveal(chosen, correct, correctLetter) {
  document.querySelectorAll('.gmat-opt').forEach(function (b) {
    b.disabled = true;
    if (b.dataset.letter === correctLetter) b.classList.add('gmat-correct');
    if (b.dataset.letter === chosen && !correct) b.classList.add('gmat-wrong');
  });
  var s = document.getElementById('gmat-status');
  if (s) s.textContent = correct ? '✓ Correct' : '✗ Incorrect';
  var c = document.getElementById('gmat-continue');
  if (c) c.style.display = 'inline-block';
}
</script>
"""

_MCQ_BACK = """\
{{FrontSide}}
<hr>
<div class="gmat-answer">Correct answer: <b>{{Answer}}</b></div>
{{#Explanation}}<div class="gmat-expl">{{Explanation}}</div>{{/Explanation}}
"""

_MCQ_CSS = """\
.card { font-family: arial; font-size: 18px; text-align: left; }
.gmat-q { font-weight: bold; margin-bottom: 12px; }
.gmat-opts { display: flex; flex-direction: column; gap: 8px; }
.gmat-opt { text-align: left; padding: 8px 12px; border: 1px solid #ccc;
            border-radius: 6px; cursor: pointer; font-size: 16px;
            /* force readable contrast in both light and night mode */
            background: #f8f8f8 !important; color: #222 !important; }
.gmat-opt:hover:enabled { background: #eef !important; }
.gmat-correct { background: #c8e6c9 !important; border-color: #2e7d32 !important; }
.gmat-wrong { background: #ffcdd2 !important; border-color: #c62828 !important; }
#gmat-status { margin: 12px 0; font-weight: bold; }
#gmat-continue { padding: 8px 16px; font-size: 16px; }
"""


def ensure_mcq_notetype(col) -> int:
    """Create the 'GMAT MCQ' note type if missing, or refresh its template/CSS if
    it already exists (so styling fixes apply); return its id."""
    mm = col.models
    existing = mm.by_name(MCQ_NOTETYPE_NAME)
    if existing:
        existing["tmpls"][0]["qfmt"] = _MCQ_FRONT
        existing["tmpls"][0]["afmt"] = _MCQ_BACK
        existing["css"] = _MCQ_CSS
        mm.update_dict(existing)
        return existing["id"]
    nt = mm.new(MCQ_NOTETYPE_NAME)
    for name in _MCQ_FIELDS:
        mm.add_field(nt, mm.new_field(name))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = _MCQ_FRONT
    tmpl["afmt"] = _MCQ_BACK
    mm.add_template(nt, tmpl)
    nt["css"] = _MCQ_CSS
    mm.add(nt)
    return nt["id"]


# Auto-answer state: only one card is active at a time, so a module global is fine.
_pending_ease: int | None = None


def handle_mcq_message(reviewer, url: str) -> None:
    """Handle reviewer pycmd messages emitted by the MCQ template."""
    if url.startswith("gmat_mcq:"):
        _grade_and_reveal(reviewer, url.split(":", 1)[1])
    elif url == "gmat_mcq_continue":
        _advance(reviewer)


def _is_mcq_card(reviewer) -> bool:
    card = reviewer.card
    return bool(card) and card.note_type()["name"] == MCQ_NOTETYPE_NAME


def maybe_show_mcq_bottom(reviewer) -> bool:
    """For MCQ cards, render a question-side bottom bar without the "Show Answer"
    button (grading happens by clicking an option). Returns True if handled."""
    if not _is_mcq_card(reviewer):
        return False
    middle = (
        "<table cellpadding=0><tr><td class=stat2 align=center>"
        "<span class=stattxt>Choose an answer above</span></td></tr></table>"
    )
    card = reviewer.card
    max_time = card.time_limit() / 1000 if card.should_show_timer() else 0
    reviewer.bottom.web.eval("showQuestion(%s,%d);" % (json.dumps(middle), max_time))
    return True


def _grade_and_reveal(reviewer, letter: str) -> None:
    global _pending_ease
    if reviewer.state != "question" or not _is_mcq_card(reviewer):
        return
    res = reviewer.mw.col._backend.grade_mcq(card_id=reviewer.card.id, chosen=letter)
    _pending_ease = 3 if res.correct else 1  # Good / Again
    reviewer.web.eval(
        f"gmatReveal({json.dumps(letter)}, {json.dumps(res.correct)}, "
        f"{json.dumps(res.correct_answer)});"
    )


def _advance(reviewer) -> None:
    global _pending_ease
    if _pending_ease is None:
        return
    ease = _pending_ease
    _pending_ease = None
    if practice_pool_active(reviewer):
        # Pool mode: no FSRS. Mark the card done for this cycle and draw the next.
        card = reviewer.card
        if card is not None:
            pool_mark_done(reviewer.mw.col, card.id)
        reviewer.nextCard()
        return
    # Normal mode: record the objective grade through Anki's undo-safe answer path.
    reviewer._showAnswer()
    reviewer._answerCard(3 if ease == 3 else 1)


# --- Practice pool helpers (called from the reviewer) ------------------------


def practice_pool_active(reviewer) -> bool:
    """True when the deck being studied is GMAT::Practice (pool mode)."""
    col = reviewer.mw.col
    if not col:
        return False
    did = col.decks.get_current_id()
    return col.decks.name(did) == PRACTICE_DECK


def suppress_default_answer(reviewer) -> bool:
    """In pool mode only option-clicks advance; the spacebar / ease keys and the
    normal Show-Answer path must not run (they'd hit FSRS with no `_v3`)."""
    return practice_pool_active(reviewer) and _is_mcq_card(reviewer)


def _cycle(col) -> int:
    return int(col.get_config(_CYCLE_KEY, 1))


def pool_mark_done(col, card_id: int) -> None:
    col._backend.mark_practice_done(card_id=card_id, cycle=_cycle(col))


def pool_reset(col) -> None:
    col.set_config(_CYCLE_KEY, _cycle(col) + 1)


def pool_serve(reviewer) -> bool:
    """Set `reviewer.card` to a random not-yet-done practice card for this cycle.
    Returns False (and shows the cycle-complete screen) when the pool is empty."""
    col = reviewer.mw.col
    # A previously completed cycle resets on this next entry.
    if col.get_config(_CYCLE_DONE_KEY, False):
        pool_reset(col)
        col.set_config(_CYCLE_DONE_KEY, False)
    res = col._backend.next_practice_card(search=PRACTICE_SEARCH, cycle=_cycle(col))
    if res.exhausted:
        col.set_config(_CYCLE_DONE_KEY, True)
        _show_cycle_complete(reviewer)
        reviewer.card = None
        return False
    from anki.cards import CardId

    reviewer.card = col.get_card(CardId(res.card_id))
    reviewer.card.start_timer()
    reviewer._v3 = None
    return True


def _show_cycle_complete(reviewer) -> None:
    tooltip(
        "You've completed all practice questions — the pool resets next time.",
        parent=reviewer.mw,
    )
