# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""AI "peer studier" for GMAT MCQ practice questions.

CorrectPeerDialog — a self-contained HTML/JS "blob" in an AnkiWebView talking to
Python via the pycmd bridge (the same dual-transport pattern as the MCQ card
template), so a later AnkiDroid port reuses the HTML/JS + prompts and only swaps
the bridge. The AI presents a plausible-but-wrong solution; you critique it; the
AI judges your critique (reciprocal teaching). Requires an API key.

All AI calls run off-thread (`mw.taskman.run_in_background`) and fail safe.
"""

from __future__ import annotations

import json
import random
from urllib.parse import unquote

import aqt
from aqt.gmat import PRACTICE_SEARCH, _mcq_field_map
from aqt.qt import *
from aqt.utils import disable_help_button, restoreGeom, saveGeom, showInfo, tooltip
from aqt.webview import AnkiWebView


def _practice_card_ids(col) -> list[int]:
    return list(col.find_cards(PRACTICE_SEARCH))


def _card_payload(col, cid: int) -> dict | None:
    """Question + options (no answer) for the webview, plus the fields we need
    server-side (kept in Python, never sent to JS)."""
    from anki.cards import CardId

    card = col.get_card(CardId(cid))
    fields = _mcq_field_map(card.note())
    options = [
        {"letter": x, "text": fields[x]}
        for x in ("A", "B", "C", "D", "E")
        if fields.get(x, "").strip()
    ]
    if not fields.get("Question", "").strip() or not options:
        return None
    return {
        "card_id": cid,
        "question": fields.get("Question", ""),
        "options": options,
    }


# ---------------------------------------------------------------------------
# Correct the Peer
# ---------------------------------------------------------------------------

_CORRECT_HTML = """
<div id="wrap" style="max-width:44em;margin:0 auto;padding:16px;">
  <h2 style="color:var(--fg);">Correct the Peer</h2>
  <p style="opacity:.8;">A study peer solved this question — but made a mistake.
     Spot the flaw and explain the right approach.</p>
  <div id="q" style="font-weight:600;margin:12px 0;"></div>
  <div id="opts" style="opacity:.9;margin-bottom:12px;"></div>
  <div id="peer" style="background:var(--canvas-inset,#f4ece1);border-radius:12px;
       padding:12px 14px;margin-bottom:12px;"></div>
  <textarea id="crit" rows="4" style="width:100%;box-sizing:border-box;
       border-radius:10px;padding:8px;" placeholder="Give the correct answer AND
       explain why the peer's reasoning is wrong — naming the answer alone isn't
       enough."></textarea>
  <div style="margin-top:10px;">
    <button id="submit" onclick="submitCritique()">Submit critique</button>
    <button id="next" style="display:none" onclick="pycmd('peer_next')">Next question</button>
  </div>
  <div id="feedback" style="margin-top:14px;"></div>
  <div id="busy" style="margin-top:14px;opacity:.7;"></div>
</div>
<script>
function setBusy(msg){ document.getElementById('busy').textContent = msg || ''; }
function esc(s){ var d=document.createElement('div'); d.textContent=s; return d.innerHTML; }
function showChallenge(d){
  setBusy('');
  document.getElementById('feedback').innerHTML='';
  document.getElementById('crit').value='';
  document.getElementById('crit').disabled=false;
  document.getElementById('submit').style.display='inline-block';
  document.getElementById('next').style.display='none';
  document.getElementById('q').innerHTML = d.question;
  document.getElementById('opts').innerHTML =
    d.options.map(function(o){return '<div><b>'+o.letter+'.</b> '+o.text+'</div>';}).join('');
  document.getElementById('peer').innerHTML =
    '<div style="font-weight:700;">🐱 Peer chose '+esc(d.peerChoice)+'</div>'+
    '<div style="margin-top:4px;">'+esc(d.peerReasoning)+'</div>';
}
function submitCritique(){
  var t = document.getElementById('crit').value.trim();
  if(!t){ setBusy('Write your critique first.'); return; }
  document.getElementById('submit').style.display='none';
  document.getElementById('crit').disabled=true;
  setBusy('Peer is considering your critique…');
  pycmd('peer_submit:' + encodeURIComponent(t));
}
function showFeedback(d){
  setBusy('');
  var color = d.found_flaw ? '#2e7d32' : '#b8860b';
  var head = d.found_flaw ? '✓ Good catch' : '✗ Not quite';
  document.getElementById('feedback').innerHTML =
    '<div style="font-weight:700;color:'+color+';">'+head+'</div>'+
    '<div style="margin:6px 0;">'+esc(d.feedback)+'</div>'+
    '<div style="opacity:.85;"><b>Correct answer:</b> '+esc(d.correct)+'</div>';
  document.getElementById('next').style.display='inline-block';
}
</script>
"""


class CorrectPeerDialog(QDialog):
    _GEOM = "GmatCorrectPeer"

    def __init__(self, mw: aqt.AnkiQt) -> None:
        super().__init__(mw)
        self.mw = mw
        self.setWindowTitle("Correct the Peer")
        disable_help_button(self)
        self.web = AnkiWebView(self, title="Correct the Peer")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.web)
        self.web.set_bridge_command(self._on_cmd, self)
        self.web.stdHtml(_CORRECT_HTML, context=self)
        restoreGeom(self, self._GEOM, default_size=(720, 640))
        self._current: dict | None = None  # {card_id, correct, explanation, flaw}
        self._serve()

    def _on_cmd(self, cmd: str):
        if cmd == "peer_next":
            self._serve()
        elif cmd.startswith("peer_submit:"):
            self._judge(unquote(cmd.split(":", 1)[1]))

    def _serve(self) -> None:
        col = self.mw.col
        cids = _practice_card_ids(col)
        if not cids:
            showInfo('No practice questions found in "GMAT::Practice".', parent=self)
            return
        payload = None
        for cid in random.sample(cids, len(cids)):
            payload = _card_payload(col, cid)
            if payload:
                break
        if not payload:
            return
        from anki.cards import CardId

        fields = _mcq_field_map(col.get_card(CardId(payload["card_id"])).note())
        correct = fields.get("Answer", "").strip()
        explanation = fields.get("Explanation", "").strip()
        options = [(o["letter"], o["text"]) for o in payload["options"]]
        self.web.eval("setBusy('Peer is solving…');")

        from aqt import gmat_ai

        def op(_c=None):
            return gmat_ai.peer_flawed_solution(
                payload["question"], options, correct, explanation
            )

        def on_done(fut) -> None:
            try:
                flaw = fut.result()
            except Exception:
                flaw = None
            if flaw is None:
                self.web.eval(
                    "setBusy('AI is unavailable — set OPENAI_API_KEY in .env to play.');"
                )
                return
            self._current = {
                "card_id": payload["card_id"],
                "correct": correct,
                "explanation": explanation,
                "flaw": flaw.reasoning,
            }
            data = {
                "question": payload["question"],
                "options": payload["options"],
                "peerChoice": flaw.choice,
                "peerReasoning": flaw.reasoning,
            }
            self.web.eval(f"showChallenge({json.dumps(data)});")

        self.mw.taskman.run_in_background(op, on_done)

    def _judge(self, critique: str) -> None:
        cur = self._current
        if not cur:
            return
        from aqt import gmat_ai

        def op(_c=None):
            return gmat_ai.critique_check(
                "", cur["correct"], cur["explanation"], cur["flaw"], critique
            )

        def on_done(fut) -> None:
            try:
                res = fut.result()
            except Exception:
                res = None
            if res is None:
                self.web.eval("setBusy('AI is unavailable right now.');")
                return
            data = {
                "found_flaw": res.found_flaw,
                "feedback": res.feedback,
                "correct": cur["correct"],
            }
            self.web.eval(f"showFeedback({json.dumps(data)});")

        self.mw.taskman.run_in_background(op, on_done)

    def closeEvent(self, evt) -> None:
        saveGeom(self, self._GEOM)
        super().closeEvent(evt)


# Module-level ref so the modeless dialog isn't garbage-collected.
_correct_dialog: CorrectPeerDialog | None = None


def show_correct_peer(mw: aqt.AnkiQt) -> None:
    from aqt import gmat, gmat_ai

    if not gmat_ai.ai_available():
        tooltip(
            "Correct the Peer needs an OpenAI key — set OPENAI_API_KEY in .env.",
            parent=mw,
        )
        return
    if not (mw.col and mw.col.get_config(gmat.AI_ENABLED_KEY, False)):
        tooltip(
            "Turn on AI features (dashboard switch) to play Correct the Peer.",
            parent=mw,
        )
        return
    if not gmat.peer_enabled(mw.col):
        tooltip(
            "Peer feature is OFF (study-feature ablation). Re-enable it from "
            "Tools → GMAT: Toggle Peer Feature.",
            parent=mw,
        )
        return
    global _correct_dialog
    _correct_dialog = CorrectPeerDialog(mw)
    _correct_dialog.show()
