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

import html
import json
import re
import time

import aqt
from anki.cards import CardId
from aqt import gui_hooks
from aqt.qt import *
from aqt.utils import disable_help_button, restoreGeom, saveGeom, tooltip
from aqt.webview import AnkiWebView

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


# Card-based readiness view rendered in an AnkiWebView (inherits the coral theme
# + dark mode automatically via stdHtml). `render(data)` is driven from Python.
_READINESS_HTML = """
<div id="wrap">
  <h1>GMAT readiness</h1>
  <p class="muted">Three separate scores per section &mdash; never blended.</p>
  <div id="cards"></div>
  <details class="about">
    <summary>How these scores work</summary>
    <div id="caption" class="muted"></div>
  </details>
</div>
<style>
  #wrap { max-width: 62em; margin: 0 auto; padding: 24px 28px; }
  h1 { font-size: 1.5em; font-weight: 700; margin: 0 0 2px; }
  .muted { color: var(--fg-subtle); font-size: 0.82em; line-height: 1.55; }
  .sec-card { background: var(--canvas-elevated); border: 1px solid var(--border-subtle);
              border-radius: var(--border-radius-medium); padding: 20px 24px; margin: 18px 0; }
  .sec-title { font-weight: 700; font-size: 1.05em; margin-bottom: 18px; }
  .metrics { display: grid; grid-template-columns: repeat(3, 1fr); }
  .metric { min-width: 0; padding: 2px 22px; }
  .metric:first-child { padding-left: 0; }
  .metric + .metric { border-left: 1px solid var(--border-subtle); }
  .metric-label { color: var(--accent-card); font-weight: 700; font-size: 0.68em;
                  text-transform: uppercase; letter-spacing: .06em; }
  .metric-big { font-size: 2.3em; font-weight: 700; line-height: 1.1; margin: 6px 0 2px; }
  .metric-big.dim { color: var(--fg-subtle); font-weight: 600; font-size: 1.15em; margin-top: 12px; }
  .metric-sub { font-size: 0.8em; color: var(--fg); margin-top: 2px; }
  .metric-sub2 { font-size: 0.75em; color: var(--fg-subtle); margin-top: 3px; }
  .bar { height: 6px; border-radius: 3px; background: var(--border-subtle);
         overflow: hidden; margin: 8px 0; max-width: 12em; }
  .bar-fill { height: 100%; background: var(--accent-card); border-radius: 3px; }
  .about { margin-top: 22px; }
  .about > summary { color: var(--fg-link); cursor: pointer; font-size: 0.82em;
                     list-style: none; width: fit-content; }
  .about > summary::-webkit-details-marker { display: none; }
  .about > summary::before { content: "\\25B8  "; }
  .about[open] > summary::before { content: "\\25BE  "; }
  .about > #caption { margin-top: 8px; }
  @media (max-width: 640px) {
    .metrics { grid-template-columns: 1fr; }
    .metric { padding: 10px 0; }
    .metric + .metric { border-left: none; border-top: 1px solid var(--border-subtle); }
  }
</style>
<script>
function esc(s){ var d=document.createElement('div'); d.textContent=(s==null?'':s); return d.innerHTML; }
function metric(label, m){
  var big = '<div class="metric-big'+(m.dim?' dim':'')+'">'+esc(m.big)+'</div>';
  var bar = (m.barPct!=null) ? '<div class="bar"><div class="bar-fill" style="width:'+m.barPct+'%"></div></div>' : '';
  var sub = m.sub ? '<div class="metric-sub">'+esc(m.sub)+'</div>' : '';
  var sub2 = m.sub2 ? '<div class="metric-sub2">'+esc(m.sub2)+'</div>' : '';
  return '<div class="metric"><div class="metric-label">'+esc(label)+'</div>'+big+bar+sub+sub2+'</div>';
}
function render(data){
  document.getElementById('cards').innerHTML = data.sections.map(function(s){
    return '<div class="sec-card"><div class="sec-title">'+esc(s.label)+'</div><div class="metrics">'+
      metric('Memory', s.memory)+metric('Performance', s.performance)+metric('Readiness', s.readiness)+
      '</div></div>';
  }).join('');
  document.getElementById('caption').innerHTML = esc(data.caption);
}
</script>
"""


class GmatReadinessDialog(QDialog):
    """Shows the three GMAT scores per section, each separately (never blended):
    Memory (term recall, FSRS), Performance (new-question ability, IRT), and
    Readiness (projected section score + pacing)."""

    def __init__(self, mw: aqt.AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("GMAT Readiness")
        disable_help_button(self)

        self.web = AnkiWebView(self, title="GMAT Readiness")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.web)

        buttons = QDialogButtonBox()
        refresh = buttons.addButton("Refresh", QDialogButtonBox.ButtonRole.ActionRole)
        close = buttons.addButton(QDialogButtonBox.StandardButton.Close)
        qconnect(refresh.clicked, self.refresh)
        qconnect(close.clicked, self.close)
        layout.addWidget(buttons)
        layout.setContentsMargins(0, 0, 8, 8)

        self.web.stdHtml(_READINESS_HTML, context=self)
        restoreGeom(self, "GmatReadinessDialog", default_size=(900, 680))
        self.refresh()

    @staticmethod
    def _memory_metric(t) -> dict:
        if t is None:
            return {"big": "—", "dim": True, "sub": "No term cards yet"}
        if not t.has_score:
            return {
                "big": "—",
                "dim": True,
                "sub": f"{t.reviewed_cards}/{t.total_cards} reviewed",
                "sub2": f"needs ≥{MIN_REVIEWS} reviews & ≥{MIN_CARDS} cards",
            }
        return {
            "big": f"{t.category_score * 100:.0f}%",
            "barPct": round(t.category_score * 100),
            "sub": f"{t.reviewed_cards}/{t.total_cards} reviewed · {t.mastered_cards} mastered",
            "sub2": (
                f"studied recall {t.practiced_score * 100:.0f}% "
                f"({t.practiced_low * 100:.0f}–{t.practiced_high * 100:.0f}%)"
            ),
        }

    @staticmethod
    def _performance_metric(s) -> dict:
        if s is None:
            return {"big": "—", "dim": True, "sub": "No practice attempts yet"}
        if not s.has_score:
            return {
                "big": "—",
                "dim": True,
                "sub": f"{s.responses}/{PERF_MIN_RESPONSES} MCQs answered",
                "sub2": f"needs ≥{PERF_MIN_RESPONSES} to score",
            }
        return {
            "big": f"{s.pct_correct * 100:.0f}%",
            "sub": f"θ {s.theta:+.2f} · {s.responses} MCQs",
        }

    @staticmethod
    def _readiness_metric(s) -> dict:
        if s is None or not s.has_score:
            return {"big": "—", "dim": True, "sub": "Not enough data yet"}
        # Project the 60–90 section score onto a 0–100 bar.
        bar = max(0, min(100, round((s.score - 60.0) / 30.0 * 100)))
        return {
            "big": f"{s.score:.0f}",
            "barPct": bar,
            "sub": f"range {s.score_low:.0f}–{s.score_high:.0f} · {s.confidence} confidence",
            "sub2": (
                f"pacing {s.within_budget_rate * 100:.0f}% in budget · "
                f"~{s.projected_section_minutes:.0f}/{SECTION_MINUTES} min"
            ),
        }

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

        sections = []
        for topic, label in SECTIONS:
            s = readiness.get(topic)
            sections.append(
                {
                    "label": label,
                    "memory": self._memory_metric(memory.get(topic)),
                    "performance": self._performance_metric(s),
                    "readiness": self._readiness_metric(s),
                }
            )

        updated = time.strftime("%Y-%m-%d %H:%M:%S")
        caption = (
            f"Last updated {updated}. Memory = term recall only (MCQ excluded); a section "
            f"scores after ≥{MIN_REVIEWS} reviews & ≥{MIN_CARDS} cards. Performance/Readiness "
            f"need ≥{PERF_MIN_RESPONSES} answered MCQs with a precise-enough estimate. "
            "Performance is IRT ability from timed MCQs (item difficulty assumed, not "
            "calibrated); the Readiness θ→score is an approximate placeholder, not validated "
            "against real exam outcomes."
        )
        payload = {"sections": sections, "caption": caption}
        self.web.eval(f"render({json.dumps(payload)});")

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
    """Add the GMAT setup entries under the Tools menu. The user-facing actions
    (Readiness, Race/Correct the Peer) and the AI on/off switch live on the
    deck-browser dashboard hero instead."""
    mcq = QAction("Create GMAT MCQ Note Type", mw)
    qconnect(mcq.triggered, lambda: _create_mcq_notetype(mw))
    mw.form.menuTools.addAction(mcq)

    organize = QAction("Organize GMAT Decks by Section", mw)
    qconnect(organize.triggered, lambda: organize_gmat_decks_by_section(mw))
    mw.form.menuTools.addAction(organize)

    setup_ai_grading()
    # On each profile load, keep the stored note types in sync with the code:
    # refresh the MCQ template/CSS, and (when AI is on) ensure every GMAT::Terms
    # note type has the typed-recall input — so template/styling changes and new
    # sections pick up AI grading without manual steps.
    gui_hooks.profile_did_open.append(_gmat_profile_refresh)
    _gmat_profile_refresh()


def _gmat_profile_refresh() -> None:
    mw = aqt.mw
    if not (mw and mw.col):
        return
    if mw.col.models.by_name(MCQ_NOTETYPE_NAME):
        ensure_mcq_notetype(mw.col)  # refresh template/CSS (never creates it here)
    # Keep the term note types in sync with the AI switch: text box present only
    # when AI is on, removed when off.
    if mw.col.get_config(AI_ENABLED_KEY, False):
        ensure_terms_typed_recall(mw.col)
    else:
        remove_terms_typed_recall(mw.col)


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

# --- Daily practice quota (FSRS-independent) ---------------------------------
# MCQ practice is a drill, not spaced repetition, so it is excluded from the
# normal "due today" counts (see deckbrowser). Instead a fixed number of practice
# questions are "due" each day. "Done today" is derived directly from the review
# log (distinct GMAT MCQ cards answered since the day's rollover) rather than a
# hand-incremented counter — the revlog is the synced source of truth, so the
# count is consistent across devices, self-correcting, and never spuriously resets.
PRACTICE_TARGET_KEY = "gmat_practice_daily_target"
_DEFAULT_PRACTICE_TARGET = 20


def practice_daily_target(col) -> int:
    """How many practice questions are due per day (configurable)."""
    try:
        return int(col.get_config(PRACTICE_TARGET_KEY, _DEFAULT_PRACTICE_TARGET))
    except (TypeError, ValueError):
        return _DEFAULT_PRACTICE_TARGET


def practice_done_today(col) -> int:
    """Distinct GMAT MCQ practice cards answered since today's rollover, counted
    from the review log (MCQ answers are logged as non-scheduling revlog rows)."""
    nt = col.models.by_name(MCQ_NOTETYPE_NAME)
    if nt is None:
        return 0
    day_start_ms = (col.sched.day_cutoff - 86400) * 1000
    return (
        col.db.scalar(
            "select count(distinct r.cid) from revlog r "
            "join cards c on r.cid = c.id "
            "join notes n on c.nid = n.id "
            "where n.mid = ? and r.id >= ?",
            nt["id"],
            day_start_ms,
        )
        or 0
    )


def practice_due_today(col) -> int:
    """Remaining practice questions in today's quota (never negative)."""
    return max(0, practice_daily_target(col) - practice_done_today(col))


def _subtree_contains(node, did) -> bool:
    if node.deck_id == did:
        return True
    return any(_subtree_contains(child, did) for child in node.children)


def counts_excluding_practice(col, node) -> tuple[int, int, int]:
    """(new, learn, review) for a deck-tree node with the GMAT::Practice subtree
    removed. Sums non-practice subtrees rather than subtracting practice from the
    aggregate, so daily-limit capping stays consistent (a parent never drops below
    its FSRS children)."""
    practice_did = col.decks.id_for_name(PRACTICE_DECK)
    return _counts_excl(node, practice_did)


def _counts_excl(node, practice_did) -> tuple[int, int, int]:
    if practice_did is not None and node.deck_id == practice_did:
        return (0, 0, 0)  # the practice subtree contributes nothing
    if practice_did is not None and _subtree_contains(node, practice_did):
        # An ancestor of practice: sum its children with practice excluded.
        new = learn = review = 0
        for child in node.children:
            cn, cl, cr = _counts_excl(child, practice_did)
            new += cn
            learn += cl
            review += cr
        return (new, learn, review)
    # No practice inside: the node's own (capped) aggregate is already correct.
    return (node.new_count, node.learn_count, node.review_count)


# --- AI term-card grading (Deliverable: Friday AI) ---------------------------
# When enabled, GMAT::Terms cards get a typed-recall input and the student's
# answer is graded for *meaning* by an LLM (qt/aqt/gmat_ai.py), grounded in the
# card's own answer field (the named source). With AI off / no key, term cards
# fall back to normal self-rating.
TERMS_DECK = "GMAT::Terms"
AI_ENABLED_KEY = "gmat_ai_enabled"

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
<div id="gmat-peer" style="display:none"></div>
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
// Desktop-only: an AI "study peer" explains a wrong answer (injected async).
function gmatPeerGuidance(html) {
  var p = document.getElementById('gmat-peer');
  if (p) { p.innerHTML = html; p.style.display = 'block'; }
}
function gmatPeerHide() {
  var p = document.getElementById('gmat-peer');
  if (p) { p.style.display = 'none'; p.innerHTML = ''; }
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
    # On a wrong answer, an AI "study peer" explains the mistake (desktop-only,
    # async so the reveal isn't blocked; nothing shown when AI is off).
    if not res.correct and ai_grading_enabled(reviewer.mw.col):
        _maybe_peer_guidance(reviewer, letter)


def _mcq_field_map(note) -> dict:
    """Map GMAT MCQ field names -> values for a note."""
    names = [f["name"] for f in note.note_type()["flds"]]
    return {name: note.fields[i] for i, name in enumerate(names)}


def _render_peer_panel(reply) -> str:
    # reply.svg is already sanitized in gmat_ai; insert it raw so it renders.
    svg_html = ""
    if getattr(reply, "svg", ""):
        svg_html = (
            '<div style="margin-top:10px;max-width:100%;overflow:auto;">'
            f"{reply.svg}</div>"
        )
    return (
        '<div style="margin-top:14px;text-align:left;max-width:40em;'
        "margin-left:auto;margin-right:auto;padding:12px 14px;border-radius:12px;"
        'background:rgba(249,135,111,0.12);">'
        '<div style="font-weight:700;margin-bottom:4px;">🐱 Study peer</div>'
        f"<div>{html.escape(reply.text)}</div>{svg_html}</div>"
    )


def _render_peer_thinking() -> str:
    """Placeholder shown immediately while the AI peer explanation loads."""
    return (
        '<div style="margin-top:14px;text-align:left;max-width:40em;'
        "margin-left:auto;margin-right:auto;padding:12px 14px;border-radius:12px;"
        'background:rgba(249,135,111,0.12);">'
        '<div style="font-weight:700;margin-bottom:6px;">🐱 Study peer</div>'
        '<div style="display:flex;align-items:center;gap:10px;opacity:.85;">'
        '<span class="gmat-peer-spin"></span>'
        "<span>Thinking about your answer&hellip;</span></div>"
        "<style>.gmat-peer-spin{width:15px;height:15px;border:2px solid "
        "rgba(216,90,64,.25);border-top-color:#d85a40;border-radius:50%;"
        "display:inline-block;animation:gmat-spin .7s linear infinite;}"
        "@keyframes gmat-spin{to{transform:rotate(360deg);}}"
        "@media (prefers-reduced-motion){.gmat-peer-spin{animation-duration:2s;}}"
        "</style></div>"
    )


def _maybe_peer_guidance(reviewer, chosen_letter: str) -> None:
    """Fetch an AI peer explanation off-thread and inject it into the card."""
    from aqt import gmat_ai

    card = reviewer.card
    if card is None:
        return
    fields = _mcq_field_map(card.note())
    question = fields.get("Question", "")
    options = [
        (x, fields[x]) for x in ("A", "B", "C", "D", "E") if fields.get(x, "").strip()
    ]
    correct = fields.get("Answer", "").strip()
    explanation = fields.get("Explanation", "").strip()
    card_id = card.id
    mw = reviewer.mw

    # Show a "thinking" indicator right away so there's visible feedback while the
    # peer explanation loads off-thread.
    reviewer.web.eval(f"gmatPeerGuidance({json.dumps(_render_peer_thinking())});")

    def op(_col=None):
        return gmat_ai.peer_explain(
            question, options, correct, chosen_letter, explanation
        )

    def on_done(fut) -> None:
        try:
            reply = fut.result()
        except Exception:
            reply = None
        # Only touch the panel if we're still on the same card.
        if reviewer.card is None or reviewer.card.id != card_id:
            return
        if not reply:
            reviewer.web.eval("gmatPeerHide();")  # AI unavailable — clear the spinner
            return
        reviewer.web.eval(f"gmatPeerGuidance({json.dumps(_render_peer_panel(reply))});")

    mw.taskman.run_in_background(op, on_done)


def _advance(reviewer) -> None:
    global _pending_ease
    if _pending_ease is None:
        return
    _pending_ease = None
    card = reviewer.card
    if practice_pool_active(reviewer):
        # Pool mode: no FSRS. Mark the card done for this cycle and draw the next.
        # (Today's practice count is derived from the revlog, so nothing to record here.)
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
    """True when studying GMAT::Practice or any of its per-section subdecks."""
    col = reviewer.mw.col
    if not col:
        return False
    name = col.decks.name(col.decks.get_current_id())
    return name == PRACTICE_DECK or name.startswith(PRACTICE_DECK + "::")


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
    # Scope the pool to the deck being studied: the parent GMAT::Practice serves
    # every section (deck: includes subdecks), while a section subdeck (e.g.
    # GMAT::Practice::Quant) serves only that section.
    deck_name = col.decks.name(col.decks.get_current_id())
    search = f'deck:"{deck_name}" note:"{MCQ_NOTETYPE_NAME}"'
    res = col._backend.next_practice_card(
        search=search, cycle=_cycle(col), tag_prefix=TAG_PREFIX
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


# --- Organize decks by GMAT section (Quant / Verbal / Data Insights) ---------

# Subdeck leaf name per section, derived from SECTIONS (e.g. "Quant").
_SECTION_LEAVES = [topic.split("::")[1] for topic, _ in SECTIONS]


def _section_leaf_from_tags(tagstr: str) -> str | None:
    """Return the section subdeck leaf (Quant/Verbal/DataInsights) for a card
    from its space-separated tag string, or None if it has no section tag."""
    valid = set(_SECTION_LEAVES)
    for tag in tagstr.split():
        if tag.startswith(TAG_PREFIX + "::"):
            parts = tag.split("::")
            if len(parts) >= 2 and parts[1] in valid:
                return parts[1]
    return None


def organize_gmat_decks_by_section(mw: aqt.AnkiQt) -> None:
    """Move GMAT::Terms and GMAT::Practice cards into per-section subdecks
    (…::Quant / ::Verbal / ::DataInsights) based on each card's section tag.
    One-time and undo-safe; cards without a section tag stay in the parent."""
    col = mw.col
    if not col:
        return
    moved = 0
    made: set[str] = set()
    for parent in (TERMS_DECK, PRACTICE_DECK):
        pd = col.decks.by_name(parent)
        if not pd:
            continue
        parent_did = pd["id"]
        # Cards directly in the parent (subdeck cards already have a different did).
        rows = col.db.all(
            "select c.id, n.tags from cards c, notes n "
            "where c.nid = n.id and c.did = ?",
            parent_did,
        )
        buckets: dict[str, list[CardId]] = {}
        for cid, tags in rows:
            leaf = _section_leaf_from_tags(tags or "")
            if leaf:
                buckets.setdefault(leaf, []).append(CardId(cid))
        for leaf, cids in buckets.items():
            target_did = col.decks.id(f"{parent}::{leaf}")
            col.set_deck(cids, target_did)
            moved += len(cids)
            made.add(f"{parent}::{leaf}")
    if moved:
        tooltip(
            f"Organized {moved} cards into {len(made)} section subdecks.", parent=mw
        )
    else:
        tooltip(
            "Nothing to organize — cards are already sorted or have no "
            f'"{TAG_PREFIX}::<section>" tags.',
            parent=mw,
        )
    mw.reset()


# --- AI term-card grading ----------------------------------------------------
#
# Term cards use Anki's built-in type-answer machinery (a {{type:Field}} input
# we add to the note type when AI grading is enabled). On answer, the
# `reviewer_will_render_compared_answer` hook replaces the exact-match diff with
# an LLM semantic verdict AND recommends the Anki/FSRS rating (Again/Hard/Good/
# Easy). The recommended answer button is highlighted + focused, but the student
# clicks to advance (so they can review first). Any failure (no key, network
# error, non-term card) falls back to normal self-rating; the app works with AI off.

# Small delay so the answer buttons are on screen before we highlight the pick.
_HIGHLIGHT_DELAY_MS = 60

# Guard so we grade + highlight only once per answer render.
_last_ai_graded_card: int | None = None


def ai_grading_enabled(col) -> bool:
    """True when the per-collection toggle is on AND a key is available."""
    if not col or not col.get_config(AI_ENABLED_KEY, False):
        return False
    try:
        from aqt import gmat_ai

        return gmat_ai.ai_available()
    except Exception:
        return False


def _is_terms_card(card) -> bool:
    if card is None or aqt.mw is None or aqt.mw.col is None:
        return False
    try:
        name = aqt.mw.col.decks.name(card.current_deck_id())
    except Exception:
        return False
    return name == TERMS_DECK or name.startswith(TERMS_DECK + "::")


def _render_ai_verdict(result) -> str:
    color = {"correct": "#2e7d32", "partial": "#b8860b", "incorrect": "#c62828"}.get(
        result.verdict, "#555"
    )
    label = {
        "correct": "✓ Correct",
        "partial": "≈ Partially correct",
        "incorrect": "✗ Incorrect",
    }.get(result.verdict, result.verdict)
    rationale = html.escape(result.rationale) if result.rationale else ""
    rating_color = {
        "again": "#c62828",
        "hard": "#b8860b",
        "good": "#2e7d32",
        "easy": "#1565c0",
    }.get(result.rating, "#555")
    rating = {
        "again": "Again",
        "hard": "Hard",
        "good": "Good",
        "easy": "Easy",
    }.get(result.rating, result.rating)
    # A distinct callout panel so the AI feedback reads as separate from the card's
    # own definition. The reviewer paper card is always light (both themes), so fixed
    # light panel colors are safe here.
    return (
        '<div class="gmat-ai-verdict" style="text-align:left;max-width:40em;'
        "margin:18px auto;background:#ffffff;border:1px solid rgba(0,0,0,.08);"
        f"border-left:5px solid {color};border-radius:14px;padding:14px 18px 16px;"
        'box-shadow:0 2px 10px rgba(120,70,50,.10);">'
        # Header badge marks this whole block as AI, not card content.
        '<div style="display:flex;align-items:center;gap:6px;font-size:.72em;'
        "font-weight:700;text-transform:uppercase;letter-spacing:.06em;"
        'color:#d85a40;margin-bottom:8px;">'
        "<span>🐱</span><span>AI feedback</span></div>"
        f'<div style="font-weight:700;color:{color};font-size:1.15em;">{label}</div>'
        '<div style="margin-top:10px;display:flex;align-items:center;gap:8px;'
        'flex-wrap:wrap;">'
        '<span style="opacity:.7;">AI recommends:</span>'
        f'<span style="display:inline-block;background:{rating_color};color:#fff;'
        "font-weight:700;font-size:1.05em;padding:4px 16px;border-radius:999px;"
        f'letter-spacing:.02em;">{rating}</span>'
        '<span style="opacity:.6;">— press it (highlighted below) when ready</span>'
        "</div>"
        # Why this rating (1–2 sentences), not a repeat of the definition.
        f'<div style="margin:10px 0 0;color:#241f1c;">{rationale}</div>'
        '<div style="opacity:.55;font-size:.78em;margin-top:10px;'
        'border-top:1px solid rgba(0,0,0,.06);padding-top:8px;">'
        "Graded by AI against this card's definition.</div>"
        "</div>"
    )


def _thinking_panel_html() -> str:
    """Placeholder shown immediately while the AI grades off-thread (grading can
    take a few seconds; this gives instant visual feedback)."""
    return (
        '<div class="gmat-ai-verdict" id="gmat-ai-panel" style="text-align:left;'
        "max-width:40em;margin:18px auto;background:#ffffff;border:1px solid "
        "rgba(0,0,0,.08);border-left:5px solid #d85a40;border-radius:14px;"
        'padding:14px 18px 16px;box-shadow:0 2px 10px rgba(120,70,50,.10);">'
        '<div style="display:flex;align-items:center;gap:6px;font-size:.72em;'
        "font-weight:700;text-transform:uppercase;letter-spacing:.06em;"
        'color:#d85a40;margin-bottom:10px;"><span>🐱</span><span>AI feedback</span></div>'
        '<div style="display:flex;align-items:center;gap:10px;color:#241f1c;">'
        '<span class="gmat-spinner"></span>'
        "<span>Grading your answer&hellip;</span></div>"
        "<style>"
        ".gmat-spinner{width:16px;height:16px;border:2px solid rgba(216,90,64,.25);"
        "border-top-color:#d85a40;border-radius:50%;display:inline-block;"
        "animation:gmat-spin .7s linear infinite;}"
        "@keyframes gmat-spin{to{transform:rotate(360deg);}}"
        "@media (prefers-reduced-motion){.gmat-spinner{animation-duration:2s;}}"
        "</style></div>"
    )


def _swap_ai_panel(html_str: str) -> None:
    """Replace the in-card thinking placeholder with final content (verdict or the
    original diff), via the reviewer's card webview."""
    web = getattr(aqt.mw, "web", None)
    if web is None:
        return
    web.eval(
        "(function(){var p=document.getElementById('gmat-ai-panel');"
        f"if(p){{p.outerHTML={json.dumps(html_str)};}}}})();"
    )


def _start_ai_grade(
    card, question: str, expected: str, provided: str, fallback: str
) -> None:
    """Grade off-thread, then swap the placeholder for the verdict (or revert to the
    normal diff on failure) and highlight the recommended rating."""
    mw = aqt.mw

    def op(_col=None):
        try:
            from aqt import gmat_ai

            return gmat_ai.grade(question, expected, provided)
        except Exception:
            return None

    def on_done(fut) -> None:
        try:
            result = fut.result()
        except Exception:
            result = None
        rev = getattr(mw, "reviewer", None)
        # The card may have changed while grading — don't touch a different card.
        if rev is None or rev.card is None or rev.card.id != card.id:
            return
        if result is None:
            _swap_ai_panel(fallback)  # fall back to the exact-match diff
            return
        _swap_ai_panel(_render_ai_verdict(result))
        try:
            count = mw.col.sched.answerButtons(card)
        except Exception:
            count = 4
        ease = _ease_for_rating(result.rating, count)
        QTimer.singleShot(_HIGHLIGHT_DELAY_MS, lambda e=ease: _highlight_choice(e))

    mw.taskman.run_in_background(op, on_done)


def maybe_ai_grade_render(
    output: str, expected: str, provided: str, type_pattern: str
) -> str:
    """`reviewer_will_render_compared_answer` hook. For GMAT::Terms cards with AI
    grading on, show a "thinking" placeholder immediately and grade off-thread
    (swapping in the verdict when ready); otherwise return the unchanged output."""
    global _last_ai_graded_card
    mw = aqt.mw
    reviewer = getattr(mw, "reviewer", None)
    if reviewer is None or not ai_grading_enabled(mw.col):
        return output
    card = reviewer.card
    if not _is_terms_card(card):
        return output
    # Grade once per answer render; re-renders reuse the in-flight/finished result.
    if _last_ai_graded_card != card.id:
        _last_ai_graded_card = card.id
        question = card.note().fields[0] if card.note().fields else ""
        _start_ai_grade(card, question, expected or "", provided or "", output)
    return _thinking_panel_html()


def _ease_for_rating(rating: str, button_count: int) -> int:
    """Map a rating name to the correct ease for the card's button layout. Anki
    shifts ease values by count: 4 buttons = Again/Hard/Good/Easy (1-4), 3 =
    Again/Good/Easy (1-3), 2 = Again/Good (1-2). Ratings without a matching
    button fall back to the nearest available (hard->Good, easy->Good/top)."""
    if button_count >= 4:
        return {"again": 1, "hard": 2, "good": 3, "easy": 4}.get(rating, 3)
    if button_count == 3:  # Again, Good, Easy
        return {"again": 1, "hard": 2, "good": 2, "easy": 3}.get(rating, 2)
    return {"again": 1, "hard": 2, "good": 2, "easy": 2}.get(rating, 2)  # Again, Good


def _highlight_choice(ease: int) -> None:
    """Ring + focus the AI-recommended answer button in the bottom bar so the
    student can see (and one-tap / press Enter) the pick. Never answers for them."""
    rev = getattr(aqt.mw, "reviewer", None)
    if rev is None or getattr(rev, "bottom", None) is None:
        return
    js = (
        """
(function() {
  document.querySelectorAll('button[data-ease]').forEach(function(b) {
    b.style.outline = ''; b.style.outlineOffset = ''; b.style.boxShadow = '';
  });
  var el = document.querySelector('button[data-ease="%d"]');
  if (el) {
    el.style.outline = '3px solid #f9876f';
    el.style.outlineOffset = '2px';
    el.style.boxShadow = '0 0 0 5px rgba(249,135,111,0.35)';
    try { el.focus(); } catch (e) {}
  }
})();
"""
        % ease
    )
    try:
        rev.bottom.web.eval(js)
    except Exception:
        pass


def _terms_notetype_ids(col) -> list[int]:
    """Distinct note-type ids used by GMAT::Terms cards (all sections)."""
    ids = col.find_cards(f'deck:"{TERMS_DECK}"')
    if not ids:
        return []
    id_list = ",".join(str(int(c)) for c in ids)  # ids are ints -> safe to inline
    return col.db.list(
        f"select distinct mid from notes where id in "
        f"(select nid from cards where id in ({id_list}))"
    )


def ensure_terms_typed_recall(col) -> bool:
    """Add a `{{type:<answer-field>}}` recall input to EVERY note type used by
    GMAT::Terms cards, so the typed-answer flow + AI grader engage for all
    sections. Idempotent. Returns True if any term cards were found."""
    from anki.models import NotetypeId

    mids = _terms_notetype_ids(col)
    if not mids:
        return False
    for mid in mids:
        nt = col.models.get(NotetypeId(mid))
        if not nt:
            continue
        tmpl = nt["tmpls"][0]
        if "{{type:" in tmpl["qfmt"]:
            continue
        fields = [f["name"] for f in nt["flds"]]
        back = fields[1] if len(fields) > 1 else fields[0]
        tmpl["qfmt"] = tmpl["qfmt"] + f"\n\n{{{{type:{back}}}}}"
        col.models.update_dict(nt)
    return True


def remove_terms_typed_recall(col) -> None:
    """Strip the `{{type:...}}` recall input from GMAT::Terms note types, so with
    AI off the term cards revert to plain front/back self-rating (no text box,
    no AI response). Idempotent."""
    from anki.models import NotetypeId

    for mid in _terms_notetype_ids(col):
        nt = col.models.get(NotetypeId(mid))
        if not nt:
            continue
        tmpl = nt["tmpls"][0]
        if "{{type:" not in tmpl["qfmt"]:
            continue
        # Remove the injected type field (and the whitespace before it).
        new_qfmt = re.sub(r"\s*\{\{type:[^}]*\}\}", "", tmpl["qfmt"]).rstrip()
        if new_qfmt != tmpl["qfmt"]:
            tmpl["qfmt"] = new_qfmt
            col.models.update_dict(nt)


def toggle_ai_grading(mw: aqt.AnkiQt) -> None:
    col = mw.col
    if not col:
        return
    new = not bool(col.get_config(AI_ENABLED_KEY, False))
    col.set_config(AI_ENABLED_KEY, new)
    if not new:
        remove_terms_typed_recall(col)  # drop the text box; back to self-rating
        tooltip("AI features off — term cards use normal self-rating.", parent=mw)
        return
    found = ensure_terms_typed_recall(col)
    from aqt import gmat_ai

    if not found:
        tooltip(f'No cards found in "{TERMS_DECK}".', parent=mw)
    elif not gmat_ai.ai_available():
        tooltip(
            "AI grading on, but no OPENAI_API_KEY found — set it in .env. "
            "Term cards fall back to self-rating until then.",
            parent=mw,
        )
    else:
        tooltip("AI term grading enabled.", parent=mw)


def _reset_ai_grade_guard(*args) -> None:
    """Clear the once-per-answer guard when a new question is shown, so the same
    card re-grades (e.g. after pressing Again) instead of getting stuck 'thinking'."""
    global _last_ai_graded_card
    _last_ai_graded_card = None


def setup_ai_grading() -> None:
    """Register the AI grading render + reset hooks (idempotent)."""
    if (
        maybe_ai_grade_render
        not in gui_hooks.reviewer_will_render_compared_answer._hooks
    ):
        gui_hooks.reviewer_will_render_compared_answer.append(maybe_ai_grade_render)
    if _reset_ai_grade_guard not in gui_hooks.reviewer_did_show_question._hooks:
        gui_hooks.reviewer_did_show_question.append(_reset_ai_grade_guard)
