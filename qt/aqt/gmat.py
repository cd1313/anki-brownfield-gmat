# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""GMAT readiness dashboard + MCQ practice wiring.

Surfaces the three per-section GMAT scores, each separately (never blended), via
read-only backend RPCs:
  - Memory: ``get_topic_mastery`` (term-flashcard FSRS retrievability).
  - Performance: ``estimate_readiness`` (IRT ability θ / accuracy on new MCQs).
  - Readiness: ``estimate_readiness`` (projected section score + pacing).

Each score has an honest range and its own give-up rule. Also wires the "GMAT
MCQ" note type + the no-FSRS practice pool.
"""

from __future__ import annotations

import json
import time

import aqt
from aqt import colors, props
from aqt.qt import *
from aqt.theme import theme_manager
from aqt.utils import disable_help_button, restoreGeom, saveGeom, tooltip

# Give-up rule + mastery thresholds (see PRD "Thresholds" open task; tweak freely).
R_THRESHOLD = 0.8  # retrievability >= this counts toward "mastered"
TIME_BUDGET_SECS = 20  # most-recent rated review must be within this to count
# (kept below Anki's ~60s answer-time cap so the speed gate is actually active:
# a correct-but-slow recall won't count as "mastered")
MIN_REVIEWS = 10  # a section is scored only after >= this many graded reviews ...
MIN_CARDS = 5  # ... and >= this many distinct reviewed cards

# Performance/readiness (IRT) give-up gate — based on how many MCQs answered, not
# elapsed time. Coverage is NOT gated (practice pools are ~2000 cards/section).
PERF_MIN_RESPONSES = 20  # need >= this many graded MCQ responses to show a score
PERF_MAX_SE = 0.7  # ... and an ability standard error no larger than this
PERF_MIN_COVERAGE = 0.0  # coverage is not a gate for performance/readiness
# Pacing parameters (feed the readiness pacing factor; NOT a gate):
SECTION_MINUTES = 45  # real GMAT Focus section time limit, for the pacing projection
PRACTICE_TIME_BUDGET_SECS = (
    120  # per-question budget; over this counts as "over budget"
)

# The term-card universe (excludes MCQ practice) and the canonical GMAT sections
# we always display, so empty sections still show an honest "no data" state.
TERMS_SEARCH = 'deck:"GMAT::Terms"'
TAG_PREFIX = "GMAT"
SECTIONS = [
    ("GMAT::Quant", "Quantitative"),
    ("GMAT::Verbal", "Verbal"),
    ("GMAT::DataInsights", "Data Insights"),
]


class GmatReadinessDialog(QDialog):
    """Shows the three GMAT scores per section, each separately (never blended):
    Memory (term recall, FSRS), Performance (new-question ability, IRT), and
    Readiness (projected section score + pacing)."""

    def __init__(self, mw: aqt.AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("GMAT Readiness")
        disable_help_button(self)

        layout = QVBoxLayout(self)

        intro = QLabel(
            "Three separate scores per GMAT section. <b>Memory</b> = recall of term "
            "flashcards (FSRS), shown two ways: <i>Practiced</i> = recall over the cards "
            "you've studied (shown with a 10th–90th percentile range); <i>Category</i> "
            "= coverage-aware over the whole section as a single number (unreviewed "
            "cards count as 0). <b>Performance</b> = ability on new MCQs (IRT). "
            "<b>Readiness</b> = projected section score (60–90) with a pacing check. "
            "No score is shown until a section has enough data."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)

        self.memory_table = self._add_table(
            layout,
            "Memory — term recall (MCQ excluded)",
            [
                "Section",
                "Practiced (studied cards)",
                "Category (whole section)",
                "Reviewed / Total",
                "Status",
            ],
        )
        self.performance_table = self._add_table(
            layout,
            "Performance — new MCQs (IRT ability)",
            ["Section", "Accuracy", "Ability θ", "Responses", "Status"],
        )
        self.readiness_table = self._add_table(
            layout,
            "Readiness — projected section score",
            ["Section", "Projected score", "Likely range", "Confidence", "Pacing"],
        )

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        buttons = QDialogButtonBox()
        refresh = buttons.addButton("Refresh", QDialogButtonBox.ButtonRole.ActionRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        qconnect(refresh.clicked, self.refresh)
        qconnect(close.clicked, self.close)
        layout.addWidget(buttons)

        self._apply_style()
        restoreGeom(self, "GmatReadinessDialog", default_size=(760, 640))
        self.refresh()

    def _apply_style(self) -> None:
        """Warm pastel skin for the dashboard's raw Qt widgets (they don't pick
        up the web CSS tokens, so resolve the shared theme tokens here)."""
        v = theme_manager.var
        surface = v(colors.CANVAS_ELEVATED)
        border = v(colors.BORDER_SUBTLE)
        fg = v(colors.FG)
        subtle = v(colors.FG_SUBTLE)
        accent = v(colors.ACCENT_CARD)
        radius = v(props.BORDER_RADIUS_MEDIUM)
        self.status_label.setObjectName("gmatStatus")
        self.setStyleSheet(
            f"""
            QLabel {{ color: {fg}; }}
            QTableWidget {{
                background: {surface};
                border: 1px solid {border};
                border-radius: {radius};
                gridline-color: {border};
                color: {fg};
            }}
            QTableWidget::item {{ padding: 4px 8px; }}
            QHeaderView::section {{
                background: {accent};
                color: #ffffff;
                padding: 6px 10px;
                border: none;
                font-weight: 600;
            }}
            #gmatStatus {{ color: {subtle}; }}
            """
        )

    def _add_table(
        self, layout: QVBoxLayout, title: str, headers: list[str]
    ) -> QTableWidget:
        layout.addWidget(QLabel(f"<b>{title}</b>"))
        table = QTableWidget(len(SECTIONS), len(headers), self)
        table.setHorizontalHeaderLabels(headers)
        table.verticalHeader().setVisible(False)
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setFixedHeight(28 * (len(SECTIONS) + 1) + 8)
        layout.addWidget(table)
        return table

    @staticmethod
    def _set_row(table: QTableWidget, row: int, cells: list[str]) -> None:
        for col_idx, text in enumerate(cells):
            table.setItem(row, col_idx, QTableWidgetItem(text))

    def refresh(self) -> None:
        col = self.mw.col
        if not col:
            return
        memory = {
            t.topic: t
            for t in col._backend.get_topic_mastery(
                search=TERMS_SEARCH,
                tag_prefix=TAG_PREFIX,
                r_threshold=R_THRESHOLD,
                time_budget_secs=TIME_BUDGET_SECS,
                min_reviews=MIN_REVIEWS,
                min_cards=MIN_CARDS,
            )
        }
        readiness = {
            s.section: s
            for s in col._backend.estimate_readiness(
                search=PRACTICE_SEARCH,
                tag_prefix=TAG_PREFIX,
                time_budget_secs=PRACTICE_TIME_BUDGET_SECS,
                section_minutes=SECTION_MINUTES,
                min_responses=PERF_MIN_RESPONSES,
                min_coverage=PERF_MIN_COVERAGE,
                max_se=PERF_MAX_SE,
            )
        }

        for row, (topic, label) in enumerate(SECTIONS):
            # --- Memory ---
            t = memory.get(topic)
            if t is None:
                self._set_row(
                    self.memory_table,
                    row,
                    [label, "Not enough data yet", "", "0 / 0", "No cards"],
                )
            elif not t.has_score:
                self._set_row(
                    self.memory_table,
                    row,
                    [
                        label,
                        "Not enough data yet",
                        "Not enough data yet",
                        f"{t.reviewed_cards} / {t.total_cards}",
                        f"Needs ≥{MIN_REVIEWS} reviews & ≥{MIN_CARDS} cards",
                    ],
                )
            else:
                practiced = (
                    f"{t.practiced_score * 100:.0f}%  "
                    f"({t.practiced_low * 100:.0f}–{t.practiced_high * 100:.0f}%)"
                )
                category = f"{t.category_score * 100:.0f}%"
                self._set_row(
                    self.memory_table,
                    row,
                    [
                        label,
                        practiced,
                        category,
                        f"{t.reviewed_cards} / {t.total_cards}",
                        f"{t.mastered_cards} mastered",
                    ],
                )

            # --- Performance + Readiness (same estimate_readiness row) ---
            s = readiness.get(topic)
            if s is None:
                self._set_row(
                    self.performance_table,
                    row,
                    [label, "Not enough data yet", "", "0", "No practice attempts"],
                )
                self._set_row(
                    self.readiness_table,
                    row,
                    [label, "Not enough data yet", "", "", ""],
                )
            elif not s.has_score:
                self._set_row(
                    self.performance_table,
                    row,
                    [
                        label,
                        "Not enough data yet",
                        "",
                        str(s.responses),
                        f"Needs ≥{PERF_MIN_RESPONSES} responses",
                    ],
                )
                self._set_row(
                    self.readiness_table,
                    row,
                    [
                        label,
                        "Not enough data yet",
                        "",
                        "low",
                        f"{s.within_budget_rate * 100:.0f}% within budget",
                    ],
                )
            else:
                self._set_row(
                    self.performance_table,
                    row,
                    [
                        label,
                        f"{s.pct_correct * 100:.0f}%",
                        f"{s.theta:+.2f}",
                        str(s.responses),
                        "scored",
                    ],
                )
                self._set_row(
                    self.readiness_table,
                    row,
                    [
                        label,
                        f"{s.score:.0f}",
                        f"{s.score_low:.0f}–{s.score_high:.0f}",
                        s.confidence,
                        f"{s.within_budget_rate * 100:.0f}% in budget · ~{s.projected_section_minutes:.0f}/{SECTION_MINUTES} min",
                    ],
                )

        updated = time.strftime("%Y-%m-%d %H:%M:%S")
        self.status_label.setText(
            f"Last updated: {updated}\n"
            f"Give-up: Memory needs ≥{MIN_REVIEWS} reviews & ≥{MIN_CARDS} cards; "
            f"Performance/Readiness need ≥{PERF_MIN_RESPONSES} answered MCQs (with a precise "
            "enough estimate).\n"
            "Memory = term recall only (MCQ excluded). Performance = IRT ability from timed MCQs "
            "(item difficulty is assumed, not calibrated). Readiness θ→score is an approximate "
            "placeholder, not validated against real exam outcomes."
        )

    def closeEvent(self, evt: QCloseEvent | None) -> None:
        saveGeom(self, "GmatReadinessDialog")
        super().closeEvent(evt)


# Module-level reference so the modeless dialog isn't garbage-collected.
_dialog: GmatReadinessDialog | None = None


def show_gmat_readiness(mw: aqt.AnkiQt) -> None:
    global _dialog
    _dialog = GmatReadinessDialog(mw)
    _dialog.show()


def _create_mcq_notetype(mw: aqt.AnkiQt) -> None:
    if not mw.col:
        return
    ensure_mcq_notetype(mw.col)
    tooltip(f'"{MCQ_NOTETYPE_NAME}" note type is ready.', parent=mw)


def setup_gmat_menu(mw: aqt.AnkiQt) -> None:
    """Add the GMAT entries under the Tools menu."""
    readiness = QAction("GMAT Readiness", mw)
    qconnect(readiness.triggered, lambda: show_gmat_readiness(mw))
    mw.form.menuTools.addAction(readiness)

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
# Section-level tag prefix; enables the IRT-weighted "recommend my weakest
# section, at my level" selection in the practice pool.
TAG_PREFIX = "GMAT"
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
// When this card is shown, for the AnkiDroid answer-latency (the IRT pacing input).
var GMAT_SHOWN = Date.now();
function gmatChoose(l) {
  if (GMAT_DROID) { gmatChooseDroid(l); } else { pycmd('gmat_mcq:' + l); }
}
function gmatChooseDroid(l) {
  var tookMillis = Math.max(0, Date.now() - GMAT_SHOWN);
  fetch('ankidroid/gmatGradeMcq', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ chosen: l, tookMillis: tookMillis })
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
.card { font-family: Inter, "Familjen Grotesk", system-ui, -apple-system, sans-serif;
        font-size: 18px; text-align: left; }
.gmat-q { font-weight: 600; margin-bottom: 16px; line-height: 1.5; }
.gmat-opts { display: flex; flex-direction: column; gap: 10px; }
/* Follow the app theme via the reviewer's CSS custom properties, with warm
 * pastel fallbacks so the card stays readable even if a var is missing. */
/* The reviewer card is always a warm "paper" surface (light in both themes),
 * so options use fixed light styling rather than the theme vars. */
.gmat-opt { text-align: left; padding: 12px 16px;
            border: 1.5px solid #e3d7ca;
            border-radius: var(--border-radius, 12px);
            cursor: pointer; font-size: 16px; line-height: 1.4;
            background: #f4ece1 !important;
            color: #241f1c !important;
            transition: background 120ms ease, border-color 120ms ease; }
.gmat-opt:hover:enabled { border-color: #f9876f;
            background: rgba(249, 135, 111, 0.12) !important; }
/* translucent tints read well over both light and dark backgrounds */
.gmat-correct { background: rgba(74, 222, 128, 0.22) !important;
            border-color: #4ade80 !important; }
.gmat-wrong { background: rgba(227, 140, 146, 0.30) !important;
            border-color: #d5747b !important; }
#gmat-status { margin: 16px 0; font-weight: 700; font-size: 17px; }
#gmat-continue { padding: 10px 22px; font-size: 16px; font-weight: 600;
            color: #fff; border: none; cursor: pointer;
            background: var(--button-primary-bg, #f9876f);
            border-radius: var(--border-radius-large, 22px); }
#gmat-continue:hover { filter: brightness(1.05); }
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
    # Latency (card shown -> option click) feeds the IRT performance model's
    # pacing factor; passing it also logs the attempt as a non-scheduling
    # revlog entry (see grade_mcq_answer in rslib/src/gmat).
    took_millis = reviewer.card.time_taken() if reviewer.card else 0
    res = reviewer.mw.col._backend.grade_mcq(
        card_id=reviewer.card.id, chosen=letter, took_millis=took_millis
    )
    _pending_ease = 3 if res.correct else 1  # Good / Again
    reviewer.web.eval(
        f"gmatReveal({json.dumps(letter)}, {json.dumps(res.correct)}, "
        f"{json.dumps(res.correct_answer)});"
    )


def _advance(reviewer) -> None:
    global _pending_ease
    if _pending_ease is None:
        return
    _pending_ease = None
    card = reviewer.card
    if practice_pool_active(reviewer):
        # Pool mode: no FSRS. Mark the card done for this cycle and draw the next.
        if card is not None:
            pool_mark_done(reviewer.mw.col, card.id)
        reviewer.nextCard()
        return
    # An MCQ card reached via the normal scheduler (e.g. studying a parent deck):
    # never FSRS-answer it. Bury it (no reschedule, no memory state) so the queue
    # advances without re-serving it, then move on.
    if _is_mcq_card(reviewer):
        if card is not None:
            reviewer.mw.col.sched.bury_cards([card.id])
        reviewer.nextCard()
        return
    # A genuine non-MCQ card: unreachable here (MCQ grading is the only caller),
    # but keep the normal answer path for safety.
    reviewer._showAnswer()
    reviewer._answerCard(3)


# --- Practice pool helpers (called from the reviewer) ------------------------


def practice_pool_active(reviewer) -> bool:
    """True when the deck being studied is GMAT::Practice (pool mode)."""
    col = reviewer.mw.col
    if not col:
        return False
    did = col.decks.get_current_id()
    return col.decks.name(did) == PRACTICE_DECK


def suppress_default_answer(reviewer) -> bool:
    """For any MCQ card (in ANY deck, not just when studying GMAT::Practice
    directly): only option-clicks advance. The spacebar / ease keys and the
    normal Show-Answer path must not run — they'd FSRS-answer the card. This is
    the guard that keeps practice cards out of FSRS even under parent-deck study.
    Grading a served MCQ card then buries it (see `_advance`)."""
    return _is_mcq_card(reviewer)


def _cycle(col) -> int:
    return int(col.get_config(_CYCLE_KEY, 1))


def pool_mark_done(col, card_id: int) -> None:
    col._backend.mark_practice_done(card_id=card_id, cycle=_cycle(col))


def pool_reset(col) -> None:
    col.set_config(_CYCLE_KEY, _cycle(col) + 1)


def pool_serve(reviewer) -> bool:
    """Set `reviewer.card` to the recommended not-yet-done practice card for this
    cycle (IRT-weighted: weakest section, at your level; see the Rust engine).
    Returns False (and shows the cycle-complete screen) when the pool is empty."""
    col = reviewer.mw.col
    # A previously completed cycle resets on this next entry.
    if col.get_config(_CYCLE_DONE_KEY, False):
        pool_reset(col)
        col.set_config(_CYCLE_DONE_KEY, False)
    res = col._backend.next_practice_card(
        search=PRACTICE_SEARCH, cycle=_cycle(col), tag_prefix=TAG_PREFIX
    )
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
