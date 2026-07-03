# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

from __future__ import annotations

import html
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

import aqt
import aqt.operations
from anki.collection import Collection, OpChanges
from anki.decks import DeckCollapseScope, DeckId, DeckTreeNode
from aqt import AnkiQt, gui_hooks
from aqt.deckoptions import display_options_for_deck_id
from aqt.operations import QueryOp
from aqt.operations.deck import (
    add_deck_dialog,
    remove_decks,
    rename_deck,
    reparent_decks,
    set_current_deck,
    set_deck_collapsed,
)
from aqt.qt import *
from aqt.sound import av_player
from aqt.toolbar import BottomBar
from aqt.utils import getOnlyText, openLink, shortcut, showInfo, tr


class DeckBrowserBottomBar:
    def __init__(self, deck_browser: DeckBrowser) -> None:
        self.deck_browser = deck_browser


@dataclass
class GmatSectionStat:
    """One GMAT section's headline numbers for the dashboard hero."""

    label: str
    mastery: float | None  # 0..1 term-recall coverage, or None if not enough data
    readiness: float | None  # projected section score, or None


@dataclass
class RenderData:
    """Data from collection that is required to show the page."""

    tree: DeckTreeNode
    current_deck_id: DeckId
    studied_today: str
    sched_upgrade_required: bool
    total_due: int = 0
    practice_due: int = 0  # today's GMAT MCQ practice quota (FSRS-independent)
    gmat: list[GmatSectionStat] = field(default_factory=list)
    ai_enabled: bool = False  # the "AI features" master switch (col.conf)
    ai_has_key: bool = False  # whether an OPENAI_API_KEY is present


@dataclass
class DeckBrowserContent:
    """Stores sections of HTML content that the deck browser will be
    populated with.

    Attributes:
        tree {str} -- HTML of the deck tree section
        stats {str} -- HTML of the stats section
        hero {str} -- HTML of the dashboard hero (mascot, summary, upload)
    """

    tree: str
    stats: str
    hero: str = ""


@dataclass
class RenderDeckNodeContext:
    current_deck_id: DeckId


def _collect_gmat_stats(col: Collection) -> list[GmatSectionStat]:
    """Per-section GMAT mastery + readiness for the dashboard hero. Returns []
    for non-GMAT collections or when nothing is scored yet, so the hero can
    hide the block. Never raises — the dashboard must render regardless."""
    try:
        from aqt import gmat as g

        memory = {
            t.topic: t
            for t in col._backend.get_topic_mastery(
                search=g.TERMS_SEARCH,
                tag_prefix=g.TAG_PREFIX,
                r_threshold=g.R_THRESHOLD,
                time_budget_secs=g.TIME_BUDGET_SECS,
                min_reviews=g.MIN_REVIEWS,
                min_cards=g.MIN_CARDS,
            )
        }
        readiness = {
            s.section: s
            for s in col._backend.estimate_readiness(
                search=g.PRACTICE_SEARCH,
                tag_prefix=g.TAG_PREFIX,
                time_budget_secs=g.PRACTICE_TIME_BUDGET_SECS,
                section_minutes=g.SECTION_MINUTES,
                min_responses=g.PERF_MIN_RESPONSES,
                min_coverage=g.PERF_MIN_COVERAGE,
                max_se=g.PERF_MAX_SE,
            )
        }
        out: list[GmatSectionStat] = []
        for topic, label in g.SECTIONS:
            t = memory.get(topic)
            s = readiness.get(topic)
            mastery = t.category_score if (t and t.has_score) else None
            score = s.score if (s and s.has_score) else None
            if mastery is not None or score is not None:
                out.append(
                    GmatSectionStat(label=label, mastery=mastery, readiness=score)
                )
        return out
    except Exception:
        return []


class DeckBrowser:
    _render_data: RenderData

    def __init__(self, mw: AnkiQt) -> None:
        self.mw = mw
        self.web = mw.web
        self.bottom = BottomBar(mw, mw.bottomWeb)
        self.scrollPos = QPoint(0, 0)
        self._refresh_needed = False

    def show(self) -> None:
        av_player.stop_and_clear_queue()
        self.web.set_bridge_command(self._linkHandler, self)
        # redraw top bar for theme change
        self.mw.toolbar.redraw()
        self.refresh()

    def refresh(self) -> None:
        self._renderPage()
        self._refresh_needed = False

    def refresh_if_needed(self) -> None:
        if self._refresh_needed:
            self.refresh()

    def op_executed(
        self, changes: OpChanges, handler: object | None, focused: bool
    ) -> bool:
        if changes.study_queues and handler is not self:
            self._refresh_needed = True

        if focused:
            self.refresh_if_needed()

        return self._refresh_needed

    # Event handlers
    ##########################################################################

    def _linkHandler(self, url: str) -> Any:
        if ":" in url:
            (cmd, arg) = url.split(":", 1)
        else:
            cmd = url
            arg = ""
        if cmd == "open":
            self.set_current_deck(DeckId(int(arg)))
        elif cmd == "opts":
            self._showOptions(arg)
        elif cmd == "shared":
            self._onShared()
        elif cmd == "import":
            self.mw.onImport()
        elif cmd == "create":
            self._on_create()
        elif cmd == "drag":
            source, target = arg.split(",")
            self._handle_drag_and_drop(DeckId(int(source)), DeckId(int(target or 0)))
        elif cmd == "collapse":
            self._collapse(DeckId(int(arg)))
        elif cmd == "v2upgrade":
            self._confirm_upgrade()
        elif cmd == "v2upgradeinfo":
            if self.mw.col.sched_ver() == 1:
                openLink("https://faqs.ankiweb.net/the-anki-2.1-scheduler.html")
            else:
                openLink("https://faqs.ankiweb.net/the-2021-scheduler.html")
        elif cmd == "select":
            set_current_deck(
                parent=self.mw, deck_id=DeckId(int(arg))
            ).run_in_background()
        elif cmd == "gmat_ai_toggle":
            from aqt import gmat

            gmat.toggle_ai_grading(self.mw)
            self._renderPage()  # re-render the hero with the new switch state
        elif cmd == "gmat_readiness":
            from aqt import gmat

            gmat.show_gmat_readiness(self.mw)
        elif cmd == "gmat_correct":
            from aqt import gmat_peer

            gmat_peer.show_correct_peer(self.mw)
        return False

    def set_current_deck(self, deck_id: DeckId) -> None:
        set_current_deck(parent=self.mw, deck_id=deck_id).success(
            lambda _: self.mw.onOverview()
        ).run_in_background(initiator=self)

    # HTML generation
    ##########################################################################

    # Friendly cat mascot; colors follow the theme via CSS custom properties.
    _mascot = """
<svg class="mascot" viewBox="0 0 120 120" role="img" aria-label="cat mascot"
     width="72" height="72">
  <path d="M28 40 L24 12 L52 30 Z" fill="var(--fg)"/>
  <path d="M92 40 L96 12 L68 30 Z" fill="var(--fg)"/>
  <ellipse cx="60" cy="66" rx="42" ry="38" fill="var(--fg)"/>
  <circle cx="45" cy="60" r="10" fill="var(--canvas-elevated)"/>
  <circle cx="75" cy="60" r="10" fill="var(--canvas-elevated)"/>
  <circle cx="47" cy="62" r="5" fill="var(--fg)"/>
  <circle cx="73" cy="62" r="5" fill="var(--fg)"/>
  <path d="M56 74 L64 74 L60 80 Z" fill="var(--accent-card)"/>
  <g stroke="var(--canvas-elevated)" stroke-width="2" stroke-linecap="round">
    <line x1="30" y1="72" x2="14" y2="68"/>
    <line x1="30" y1="78" x2="14" y2="80"/>
    <line x1="90" y1="72" x2="106" y2="68"/>
    <line x1="90" y1="78" x2="106" y2="80"/>
  </g>
</svg>
"""

    _body = """
<center>
%(hero)s
<table cellspacing=0 cellpadding=3>
%(tree)s
</table>

<br>
%(stats)s
</center>
"""

    def _renderPage(self, reuse: bool = False) -> None:
        if not reuse:

            def get_data(col: Collection) -> RenderData:
                from aqt import gmat, gmat_ai

                tree = col.sched.deck_due_tree()
                # MCQ practice is a drill, not spaced repetition — keep it out of the
                # FSRS "due today" total (it gets its own daily quota below). Sum the
                # non-practice subtrees so daily-limit capping stays consistent.
                total_due = sum(
                    sum(gmat.counts_excluding_practice(col, child))
                    for child in tree.children
                )
                return RenderData(
                    tree=tree,
                    current_deck_id=col.decks.get_current_id(),
                    studied_today=col.studied_today(),
                    sched_upgrade_required=not col.v3_scheduler(),
                    total_due=total_due,
                    practice_due=gmat.practice_due_today(col),
                    gmat=_collect_gmat_stats(col),
                    ai_enabled=bool(col.get_config(gmat.AI_ENABLED_KEY, False)),
                    ai_has_key=gmat_ai.ai_available(),
                )

            def success(output: RenderData) -> None:
                self._render_data = output
                self.__renderPage(None)

            QueryOp(
                parent=self.mw,
                op=get_data,
                success=success,
            ).run_in_background()
        else:
            self.web.evalWithCallback("window.pageYOffset", self.__renderPage)

    def __renderPage(self, offset: int | None) -> None:
        data = self._render_data
        content = DeckBrowserContent(
            tree=self._renderDeckTree(data.tree),
            stats=self._renderStats(),
            hero=self._renderHero(),
        )
        gui_hooks.deck_browser_will_render_content(self, content)
        self.web.stdHtml(
            self._v1_upgrade_message(data.sched_upgrade_required)
            + self._body % content.__dict__,
            css=["css/deckbrowser.css"],
            js=[
                "js/vendor/jquery.min.js",
                "js/vendor/jquery-ui.min.js",
                "js/deckbrowser.js",
            ],
            context=self,
        )
        self._drawButtons()
        if offset is not None:
            self._scrollToOffset(offset)
        gui_hooks.deck_browser_did_render(self)

    def _scrollToOffset(self, offset: int) -> None:
        self.web.eval("window.scrollTo(0, %d, 'instant');" % offset)

    def _renderStats(self) -> str:
        return '<div id="studiedToday"><span>{}</span></div>'.format(
            self._render_data.studied_today
        )

    def _renderHero(self) -> str:
        data = self._render_data
        deck_count = len(data.tree.children)
        # summary chips
        chips = (
            f'<div class="chip"><div class="chip-num">{data.total_due}</div>'
            f'<div class="chip-label">due today</div></div>'
            f'<div class="chip"><div class="chip-num">{data.practice_due}</div>'
            f'<div class="chip-label">practice today</div></div>'
            f'<div class="chip"><div class="chip-num">{deck_count}</div>'
            f'<div class="chip-label">{"deck" if deck_count == 1 else "decks"}</div></div>'
        )
        # optional GMAT section mastery / readiness
        gmat_html = ""
        if data.gmat:
            cards = ""
            for s in data.gmat:
                mastery_pct = (
                    f"{s.mastery * 100:.0f}%" if s.mastery is not None else "—"
                )
                fill = int((s.mastery or 0) * 100)
                score = f"{s.readiness:.0f}" if s.readiness is not None else "—"
                cards += (
                    f'<div class="gmat-card">'
                    f'<div class="gmat-sec">{html.escape(s.label)}</div>'
                    f'<div class="gmat-bar"><div class="gmat-bar-fill" '
                    f'style="width:{fill}%"></div></div>'
                    f'<div class="gmat-nums"><span>Memory {mastery_pct}</span>'
                    f"<span>Readiness {score}</span></div>"
                    f"</div>"
                )
            caption = (
                '<div class="gmat-caption">'
                "Percentages show your <b>memory score</b> "
                "(how well you recall this section&rsquo;s terms) &mdash; "
                "not test performance. Readiness is your projected section score."
                "</div>"
            )
            gmat_html = f'<div class="gmat-strip">{cards}</div>{caption}'

        upload = (
            '<a class="upload-btn" href=# onclick="return pycmd(\'import\')">'
            "&#x2191; Upload deck</a>"
        )
        actions = (
            '<a class="hero-btn" href=# onclick="return pycmd(\'gmat_readiness\')">'
            "Readiness</a>"
            '<a class="hero-btn" href=# onclick="return pycmd(\'gmat_correct\')">'
            "Correct the Peer</a>"
        )
        # AI-features master switch (reflects col.conf; hint when no key present).
        checked = "checked" if data.ai_enabled else ""
        hint = (
            ""
            if data.ai_has_key
            else '<span class="ai-hint">add OPENAI_API_KEY to .env</span>'
        )
        ai_switch = (
            '<label class="ai-switch" title="Toggle all AI features">'
            f'<input type="checkbox" {checked} onchange="pycmd(\'gmat_ai_toggle\')">'
            '<span class="ai-slider"></span>'
            '<span class="ai-switch-label">AI features</span></label>'
            f"{hint}"
        )
        return f"""
<div class="hero">
  <div class="hero-top">
    {self._mascot}
    <div class="hero-info">
      <div class="hero-title">Welcome back</div>
      <div class="hero-chips">{chips}</div>
    </div>
    <div class="hero-ai">{ai_switch}</div>
  </div>
  <div class="hero-actions">{upload}{actions}</div>
  {gmat_html}
</div>
"""

    def _renderDeckTree(self, top: DeckTreeNode) -> str:
        buf = """
<tr><th colspan=5 align=start>{}</th>
<th class=count>{}</th>
<th class=count>{}</th>
<th class=count>{}</th>
<th class=optscol></th></tr>""".format(
            tr.decks_deck(),
            tr.actions_new(),
            tr.decks_learn_header(),
            tr.decks_review_header(),
        )
        buf += self._topLevelDragRow()

        ctx = RenderDeckNodeContext(current_deck_id=self._render_data.current_deck_id)

        for child in top.children:
            buf += self._render_deck_node(child, ctx)

        return buf

    def _render_deck_node(self, node: DeckTreeNode, ctx: RenderDeckNodeContext) -> str:
        if node.collapsed:
            prefix = "+"
        else:
            prefix = "−"

        def indent() -> str:
            return "&nbsp;" * 6 * (node.level - 1)

        if node.deck_id == ctx.current_deck_id:
            klass = "deck current"
        else:
            klass = "deck"

        buf = (
            "<tr class='%s' id='%d' onclick='if(event.shiftKey) return pycmd(\"select:%d\")'>"
            % (
                klass,
                node.deck_id,
                node.deck_id,
            )
        )
        # deck link
        if node.children:
            collapse = (
                "<a class=collapse href=# onclick='return pycmd(\"collapse:%d\")'>%s</a>"
                % (node.deck_id, prefix)
            )
        else:
            collapse = "<span class=collapse></span>"
        if node.filtered:
            extraclass = "filtered"
        else:
            extraclass = ""
        buf += """

        <td class=decktd colspan=5>%s%s<a class="deck %s"
        href=# onclick="return pycmd('open:%d')">%s</a>%s</td>""" % (
            indent(),
            collapse,
            extraclass,
            node.deck_id,
            html.escape(node.name),
            self._progress_bar(node),
        )

        # due counts
        def nonzeroColour(cnt: int, klass: str) -> str:
            if not cnt:
                klass = "zero-count"
            return f'<span class="{klass}">{cnt}</span>'

        # GMAT MCQ practice is a drill, not FSRS — its new/learn/review counts are
        # meaningless, so blank them out on the practice rows; ancestor rows (e.g.
        # "GMAT") show their FSRS children only. The daily practice quota lives on the
        # hero instead.
        from aqt import gmat

        deck_name = self.mw.col.decks.name(node.deck_id)
        practice = gmat.PRACTICE_DECK
        if deck_name == practice or deck_name.startswith(practice + "::"):
            dash = '<span class="zero-count">–</span>'
            buf += ("<td align=end>%s</td>" * 3) % (dash, dash, dash)
        else:
            new_c, learn_c, review_c = gmat.counts_excluding_practice(self.mw.col, node)
            buf += ("<td align=end>%s</td>" * 3) % (
                nonzeroColour(new_c, "new-count"),
                nonzeroColour(learn_c, "learn-count"),
                nonzeroColour(review_c, "review-count"),
            )
        # options
        buf += (
            "<td align=center class=opts><a onclick='return pycmd(\"opts:%d\");'>"
            "<img src='/_anki/imgs/gears.svg' class=gears></a></td></tr>" % node.deck_id
        )
        # children
        if not node.collapsed:
            for child in node.children:
                buf += self._render_deck_node(child, ctx)
        return buf

    def _progress_bar(self, node: DeckTreeNode) -> str:
        """A thin bar under the deck name showing the new/learn/review mix of
        what's due. Shown only on top-level decks (whose counts aggregate their
        subdecks) to avoid a clunky stack of near-identical bars under every
        subdeck; hidden when the deck has nothing due."""
        if node.level != 1:  # top-level decks only (indent uses level - 1)
            return ""
        n, lrn, rev = node.new_count, node.learn_count, node.review_count
        total = n + lrn + rev
        if total <= 0:
            return ""
        return (
            '<div class="deck-progress" title="{n} new · {l} learning · {r} to review">'
            '<span class="pb pb-new" style="width:{np:.1f}%"></span>'
            '<span class="pb pb-learn" style="width:{lp:.1f}%"></span>'
            '<span class="pb pb-review" style="width:{rp:.1f}%"></span>'
            "</div>"
        ).format(
            n=n,
            l=lrn,
            r=rev,
            np=n / total * 100,
            lp=lrn / total * 100,
            rp=rev / total * 100,
        )

    def _topLevelDragRow(self) -> str:
        return "<tr class='top-level-drag-row'><td colspan='6'>&nbsp;</td></tr>"

    # Options
    ##########################################################################

    def _showOptions(self, did: str) -> None:
        m = QMenu(self.mw)
        a = m.addAction(tr.actions_rename())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._rename(DeckId(int(did))))
        a = m.addAction(tr.actions_options())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._options(DeckId(int(did))))
        a = m.addAction(tr.actions_export())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._export(DeckId(int(did))))
        a = m.addAction(tr.actions_delete())
        assert a is not None
        qconnect(a.triggered, lambda b, did=did: self._delete(DeckId(int(did))))
        gui_hooks.deck_browser_will_show_options_menu(m, int(did))
        m.popup(QCursor.pos())

    def _export(self, did: DeckId) -> None:
        self.mw.onExport(did=did)

    def _rename(self, did: DeckId) -> None:
        def prompt(name: str) -> None:
            new_name = getOnlyText(
                tr.decks_new_deck_name(), default=name, title=tr.actions_rename()
            )
            if not new_name or new_name == name:
                return
            else:
                rename_deck(
                    parent=self.mw, deck_id=did, new_name=new_name
                ).run_in_background()

        QueryOp(
            parent=self.mw, op=lambda col: col.decks.name(did), success=prompt
        ).run_in_background()

    def _options(self, did: DeckId) -> None:
        display_options_for_deck_id(did)

    def _collapse(self, did: DeckId) -> None:
        node = self.mw.col.decks.find_deck_in_tree(self._render_data.tree, did)
        if node:
            node.collapsed = not node.collapsed
            set_deck_collapsed(
                parent=self.mw,
                deck_id=did,
                collapsed=node.collapsed,
                scope=DeckCollapseScope.REVIEWER,
            ).run_in_background()
            self._renderPage(reuse=True)

    def _handle_drag_and_drop(self, source: DeckId, target: DeckId) -> None:
        reparent_decks(
            parent=self.mw, deck_ids=[source], new_parent=target
        ).run_in_background()

    def _delete(self, did: DeckId) -> None:
        deck = self.mw.col.decks.find_deck_in_tree(self._render_data.tree, did)
        assert deck is not None
        deck_name = deck.name
        remove_decks(
            parent=self.mw, deck_ids=[did], deck_name=deck_name
        ).run_in_background()

    # Top buttons
    ######################################################################

    drawLinks = [
        ["", "shared", tr.decks_get_shared()],
        ["", "create", tr.decks_create_deck()],
        ["Ctrl+Shift+I", "import", tr.decks_import_file()],
    ]

    def _drawButtons(self) -> None:
        buf = ""
        drawLinks = deepcopy(self.drawLinks)
        for b in drawLinks:
            if b[0]:
                b[0] = tr.actions_shortcut_key(val=shortcut(b[0]))
            buf += """
<button title='%s' onclick='pycmd(\"%s\");'>%s</button>""" % tuple(b)
        self.bottom.draw(
            buf=buf,
            link_handler=self._linkHandler,
            web_context=DeckBrowserBottomBar(self),
        )

    def _onShared(self) -> None:
        openLink(f"{aqt.appShared}decks/")

    def _on_create(self) -> None:
        if op := add_deck_dialog(
            parent=self.mw, default_text=self.mw.col.decks.current()["name"]
        ):
            op.run_in_background()

    ######################################################################

    def _v1_upgrade_message(self, required: bool) -> str:
        if not required:
            return ""

        update_required = tr.scheduling_update_required().replace("V2", "v3")

        return f"""
<center>
<div class=callout>
    <div>
      {update_required}
    </div>
    <div>
      <button onclick='pycmd("v2upgrade")'>
        {tr.scheduling_update_button()}
      </button>
      <button onclick='pycmd("v2upgradeinfo")'>
        {tr.scheduling_update_more_info_button()}
      </button>
    </div>
</div>
</center>
"""

    def _confirm_upgrade(self) -> None:
        if self.mw.col.sched_ver() == 1:
            self.mw.col.mod_schema(check=True)
            self.mw.col.upgrade_to_v2_scheduler()
        self.mw.col.set_v3_scheduler(True)

        showInfo(tr.scheduling_update_done())
        self.refresh()
