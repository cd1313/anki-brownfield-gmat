# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""AI semantic grading for GMAT::Terms flashcards.

Grades a student's *typed* recall of a term against the card's own answer field
for **meaning** (not exact string match). The card is the named source: the
prompt is grounded in the expected answer, and the model only judges the
student's answer against it.

Design:
- Provider-agnostic behind ``grade()``; defaults to OpenAI (stdlib HTTP only, no
  extra dependency), model overridable via ``GMAT_AI_MODEL``.
- Key from a gitignored ``.env`` at the repo root (loaded here) or ``os.environ``.
- Fails safe: any missing key / network error / bad response returns ``None`` so
  the caller falls back to normal self-rating. The app always works with AI off.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_MODEL = "gpt-4o-mini"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

# --- Firebase-backed AI proxy ------------------------------------------------
#
# So users get AI with NO key of their own: the app signs in as an anonymous
# Firebase user and calls our Cloud Function, which holds the real OpenAI key
# server-side. A user who sets their own OPENAI_API_KEY bypasses the proxy and
# calls OpenAI directly (BYOK). All values below are public client identifiers
# (safe to ship) or test overrides; the OpenAI key never lives here.
#
# After deploying the Firebase project, bake the real project's Web API key and
# function URL into these two defaults (they're overridable via env for
# testing/self-hosting). Until both are set, `_proxy_configured()` is False and
# behaviour is unchanged (BYOK only).
_FIREBASE_API_KEY = os.environ.get(
    "GMAT_FIREBASE_API_KEY", "AIzaSyCIsE71A1rZZlIk2ljTEklw3QjFjLWshUI"
)
_PROXY_URL = os.environ.get(
    "GMAT_AI_PROXY_URL", "https://gmataichat-pdxhfrwlqq-uc.a.run.app"
)
# Google Identity Toolkit / Secure Token endpoints (overridable to point at the
# Firebase Auth emulator during testing).
_IDENTITY_URL = os.environ.get(
    "GMAT_IDENTITY_URL", "https://identitytoolkit.googleapis.com/v1"
)
_SECURETOKEN_URL = os.environ.get(
    "GMAT_SECURETOKEN_URL", "https://securetoken.googleapis.com/v1"
)

_SYSTEM_PROMPT = (
    "You grade a student's typed recall of a flashcard term AND choose the Anki "
    "spaced-repetition rating on their behalf.\n"
    "GRADING: Grade generously — reward understanding, not exact or complete "
    "phrasing. The expected answer may contain the term's definition PLUS example "
    "sentences or usage illustrations; judge ONLY whether the student captured the "
    "core meaning of the term. Do NOT penalize brevity, informal wording, "
    "spelling/typos, synonyms, or omitting examples and minor nuance. Use these "
    "thresholds:\n"
    '- "correct": conveys the core meaning, even if brief, loosely worded, or '
    "missing minor detail. When torn between correct and partial, choose correct.\n"
    '- "partial": the general idea is present but a KEY part of the meaning is '
    "wrong or missing.\n"
    '- "incorrect": the meaning is essentially wrong, unrelated, or blank.\n'
    "The expected answer is the source of truth; do not use outside knowledge to "
    "override it.\n"
    "RATING: choose exactly one Anki/FSRS button. Their meanings drive how soon "
    "the card is seen again:\n"
    '- "again": failed recall — meaning wrong, missing, or not remembered. The '
    "card lapses and reappears almost immediately.\n"
    '- "hard": recalled but barely — partially correct, vague, or an incomplete/'
    "struggling definition. The next interval grows only slightly.\n"
    '- "good": correct recall with normal effort — the definitional meaning is '
    "right. This is the default for a correct answer.\n"
    '- "easy": a clearly correct, solid answer that captures the core meaning '
    "with no errors. It need not be perfectly worded or exhaustive, but it should "
    "be more than a bare-minimum pass. Still don't give it to a shaky, vague, or "
    "partial answer.\n"
    "Map the rating from the verdict, favouring the more forgiving option: "
    "correct->good, but use easy when the correct answer is clearly solid and "
    "confident (not just barely sufficient); partial->hard; incorrect->again. "
    "Never give 'again' to an answer that shows real understanding of the term.\n"
    'RATIONALE: write 1-2 sentences addressed to the student ("you") explaining '
    "WHY you gave that rating — how their answer compared to the definition (what "
    "they got right, missed, or confused). Do NOT restate the full definition; the "
    "card already shows it.\n"
    "Respond with ONLY compact JSON of the form "
    '{"verdict":"correct|partial|incorrect","rating":"again|hard|good|easy",'
    '"rationale":"<1-2 sentences explaining the rating>"}.'
)

# Anki answer-button eases (1=Again … 4=Easy).
_EASE = {"again": 1, "hard": 2, "good": 3, "easy": 4}


@dataclass
class GradeResult:
    """Outcome of an AI grade. ``correct`` is strict (only a full ``correct``
    verdict). ``rating`` is the AI-chosen Anki button; ``ease`` maps it to the
    1–4 answer-button value used to auto-answer the card."""

    correct: bool
    verdict: str  # "correct" | "partial" | "incorrect"
    rationale: str
    rating: str = "good"  # "again" | "hard" | "good" | "easy"

    @property
    def ease(self) -> int:
        return _EASE.get(self.rating, 3 if self.correct else 1)


def _load_dotenv() -> None:
    """Load ``KEY=VALUE`` lines from a repo-root ``.env`` into ``os.environ``
    (no dependency). Existing env vars win; never overwrites them."""
    candidates = [Path.cwd() / ".env"]
    here = Path(__file__).resolve()
    candidates += [p / ".env" for p in here.parents[:4]]
    for env in candidates:
        try:
            if not env.is_file():
                continue
            for raw in env.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = val
            return
        except OSError:
            continue


_load_dotenv()


def api_key() -> str | None:
    return os.environ.get("OPENAI_API_KEY") or None


def model() -> str:
    return os.environ.get("GMAT_AI_MODEL") or _DEFAULT_MODEL


def _proxy_configured() -> bool:
    """True when the Firebase proxy is wired up (deployed URL + Web API key)."""
    return bool(_PROXY_URL and _FIREBASE_API_KEY)


def ai_available() -> bool:
    """True when AI can run: either the user set their own OpenAI key, or the
    Firebase proxy is configured (so it works out of the box, no key needed). A
    per-collection enable toggle is checked separately by the caller."""
    return bool(api_key()) or _proxy_configured()


# --- Anonymous Firebase auth for the proxy (Identity Toolkit REST, no SDK) ----

_token_state: dict = {"id_token": None, "refresh_token": None, "expiry": 0.0}


def _token_file() -> Path | None:
    """Where to persist the refresh token so a user keeps a stable anonymous
    identity (and thus a stable daily quota) across launches. Best-effort."""
    try:
        import aqt

        mw = aqt.mw
        if mw and mw.pm:
            return Path(mw.pm.profileFolder()) / "gmat_firebase.json"
    except Exception:
        pass
    return None


def _load_refresh_token() -> str | None:
    if _token_state["refresh_token"]:
        return _token_state["refresh_token"]
    f = _token_file()
    if f and f.is_file():
        try:
            rt = json.loads(f.read_text()).get("refresh_token")
            _token_state["refresh_token"] = rt
            return rt
        except (OSError, ValueError):
            return None
    return None


def _save_refresh_token(rt: str) -> None:
    _token_state["refresh_token"] = rt
    f = _token_file()
    if f:
        try:
            f.write_text(json.dumps({"refresh_token": rt}))
        except OSError:
            pass


def _post_json(url: str, obj: dict, timeout: float = 8.0) -> dict | None:
    req = urllib.request.Request(
        url,
        data=json.dumps(obj).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


def _anon_signup() -> str | None:
    data = _post_json(
        f"{_IDENTITY_URL}/accounts:signUp?key={_FIREBASE_API_KEY}",
        {"returnSecureToken": True},
    )
    if not data or "idToken" not in data:
        return None
    _token_state["id_token"] = data["idToken"]
    _token_state["expiry"] = time.time() + int(data.get("expiresIn", 3600))
    if data.get("refreshToken"):
        _save_refresh_token(data["refreshToken"])
    return data["idToken"]


def _refresh(rt: str) -> str | None:
    data = _post_json(
        f"{_SECURETOKEN_URL}/token?key={_FIREBASE_API_KEY}",
        {"grant_type": "refresh_token", "refresh_token": rt},
    )
    if not data or "id_token" not in data:
        return None
    _token_state["id_token"] = data["id_token"]
    _token_state["expiry"] = time.time() + int(data.get("expires_in", 3600))
    if data.get("refresh_token"):
        _save_refresh_token(data["refresh_token"])
    return data["id_token"]


def _firebase_id_token() -> str | None:
    """A valid anonymous Firebase ID token, minting/refreshing as needed."""
    if _token_state["id_token"] and time.time() < _token_state["expiry"] - 60:
        return _token_state["id_token"]
    rt = _load_refresh_token()
    if rt:
        tok = _refresh(rt)
        if tok:
            return tok
    return _anon_signup()


def _post_chat(body: dict, timeout: float) -> dict | None:
    """Send an OpenAI chat-completions ``body`` and return the parsed JSON of the
    assistant message, or ``None`` on any failure. Uses the user's own key when
    set (direct to OpenAI, BYOK); otherwise routes through the Firebase proxy
    (anonymous auth, key held server-side)."""
    key = api_key()
    if key:
        url = _OPENAI_URL
        headers = {
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }
    elif _proxy_configured():
        token = _firebase_id_token()
        if not token:
            return None
        url = _PROXY_URL
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "X-Gmat-Platform": "desktop",
        }
    else:
        return None
    req = urllib.request.Request(
        url, data=json.dumps(body).encode("utf-8"), headers=headers
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
        return None


def grade(
    question: str,
    expected: str,
    answer: str,
    timeout: float = 8.0,
) -> GradeResult | None:
    """Grade ``answer`` against ``expected``. Returns ``None`` on any failure so
    the caller can fall back to self-rating."""
    if not ai_available():
        return None
    answer = (answer or "").strip()
    if not answer:
        return GradeResult(False, "incorrect", "No answer was entered.", rating="again")
    if not (expected or "").strip():
        return None

    user = (
        f"Prompt (front of card):\n{question}\n\n"
        f"Expected answer (source of truth):\n{expected}\n\n"
        f"Student's answer:\n{answer}\n\n"
        "Grade the student's answer."
    )
    parsed = _post_chat(
        {
            "model": model(),
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout,
    )
    if parsed is None:
        return None

    verdict = str(parsed.get("verdict", "")).strip().lower()
    if verdict not in ("correct", "partial", "incorrect"):
        return None
    rationale = str(parsed.get("rationale", "")).strip()
    rating = str(parsed.get("rating", "")).strip().lower()
    if rating not in _EASE:
        # Fall back to a verdict-consistent rating if the model omitted one.
        rating = {"correct": "good", "partial": "hard", "incorrect": "again"}[verdict]
    # Strict: only a full "correct" verdict counts as correct.
    return GradeResult(
        correct=(verdict == "correct"),
        verdict=verdict,
        rationale=rationale,
        rating=rating,
    )


# --- Peer studier (MCQ questions) --------------------------------------------
#
# A "study peer" for practice questions. All calls are grounded in the card's
# own Answer/Explanation (the named source) and fail safe (return None with no
# key / on any error) so the caller degrades gracefully.


def _chat_json(
    system_prompt: str, user_prompt: str, timeout: float = 12.0
) -> dict | None:
    """Shared JSON-mode chat call. Returns the parsed object, or None on any
    failure (no key/proxy, network error, bad JSON)."""
    return _post_chat(
        {
            "model": model(),
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        },
        timeout,
    )


def _format_options(options: list[tuple[str, str]]) -> str:
    return "\n".join(f"{letter}. {text}" for letter, text in options)


@dataclass
class PeerReply:
    text: str  # the peer's first-person feedback
    svg: str = ""  # optional sanitized inline SVG diagram ("" when none)


@dataclass
class PeerFlaw:
    choice: str  # the wrong option letter the peer "picked"
    reasoning: str  # the plausible-but-flawed solution


@dataclass
class Critique:
    found_flaw: bool
    feedback: str


_PEER_EXPLAIN_PROMPT = (
    "You are a friendly fellow GMAT student — a study buddy, NOT a teacher — "
    "reacting to a question your friend just got wrong. Speak in the FIRST PERSON "
    "with a warm, casual, encouraging voice, like texting a classmate. Start by "
    "relating to how tricky it was, then walk through how YOU approached it — "
    '("that one was rough! here\'s how I tried it: …") — and gently point out '
    "where their choice slips up. Share it as a peer thinking out loud, not a "
    "lecture. Base your walkthrough ONLY on the reference explanation/answer; do "
    "not invent facts. Keep it to 2-4 sentences.\n"
    "IF (and only if) a simple picture would genuinely help — a geometry figure, "
    "a number line, a small bar chart, or a tiny table — include a minimal, "
    "self-contained inline SVG in `svg` (a single <svg …>…</svg>, roughly "
    "360x260 max, readable in both light and dark themes, NO <script>, no "
    "external images or links). For questions where a diagram adds nothing (most "
    'verbal / plain-text ones), set "svg" to an empty string.\n'
    "Respond with ONLY compact JSON: "
    '{"guidance":"<your peer message>","svg":"<inline SVG or empty string>"}.'
)


def _sanitize_svg(svg: str) -> str:
    """Keep a single inline <svg> only, stripping scripts, event handlers, and
    external references. Returns "" if it doesn't look like a safe SVG."""
    s = (svg or "").strip()
    low = s.lower()
    if not low.startswith("<svg") or "</svg>" not in low or len(s) > 20000:
        return ""
    s = re.sub(r"<script.*?</script>", "", s, flags=re.IGNORECASE | re.DOTALL)
    s = re.sub(
        r"<foreignobject.*?</foreignobject>", "", s, flags=re.IGNORECASE | re.DOTALL
    )
    s = re.sub(r"""\son\w+\s*=\s*("[^"]*"|'[^']*')""", "", s, flags=re.IGNORECASE)
    # drop external hrefs/urls (keep only inline drawing)
    s = re.sub(
        r"""\s(?:xlink:href|href)\s*=\s*("[^"]*"|'[^']*')""", "", s, flags=re.IGNORECASE
    )
    return s


_PEER_FLAW_PROMPT = (
    "Role-play a fellow GMAT student who solves a multiple-choice question but "
    "makes ONE realistic, plausible mistake and arrives at a WRONG answer. Pick a "
    "wrong option (never the correct one) and write a short first-person solution "
    "(2-4 sentences) that sounds confident but contains the flaw — do not reveal "
    "that it is wrong. Base it on the real question; the correct answer is given "
    "so you can avoid it. Respond with ONLY compact JSON: "
    '{"choice":"<wrong option letter>","reasoning":"<2-4 sentence flawed solution>"}.'
)

_CRITIQUE_PROMPT = (
    "A student is playing 'correct the peer': a peer gave a flawed solution to a "
    "GMAT question and the student critiques it. To succeed the student must do "
    "BOTH: (1) give the correct answer or approach, AND (2) actually EXPLAIN it — "
    "why the peer's reasoning is wrong and/or why the correct approach works.\n"
    "Set found_flaw=true ONLY when both are present. If the student merely states "
    'an answer with no real reasoning (e.g. "the answer is A", "it\'s B", just a '
    "letter, or a vague restatement with no justification), set found_flaw=false "
    "and, in the feedback, tell them they need to explain WHY — not just name the "
    "answer. Be encouraging but honest, and ground your judgement in the reference "
    "explanation. Respond with ONLY compact JSON: "
    '{"found_flaw":true|false,"feedback":"<2-3 sentences>"}.'
)


def peer_explain(
    question: str,
    options: list[tuple[str, str]],
    correct_letter: str,
    chosen_letter: str,
    explanation: str,
) -> PeerReply | None:
    """A peer's first-person take on why the student's MCQ answer was wrong,
    grounded in the card's explanation, with an optional inline SVG diagram when
    a visual would help. None when unavailable."""
    user = (
        f"Question:\n{question}\n\nOptions:\n{_format_options(options)}\n\n"
        f"Correct answer: {correct_letter}\nStudent chose: {chosen_letter}\n\n"
        f"Reference explanation:\n{explanation or '(none provided)'}\n\n"
        "Explain the student's mistake, with a diagram only if it truly helps."
    )
    data = _chat_json(_PEER_EXPLAIN_PROMPT, user)
    if not data:
        return None
    text = str(data.get("guidance", "")).strip()
    if not text:
        return None
    return PeerReply(text=text, svg=_sanitize_svg(str(data.get("svg", ""))))


def peer_flawed_solution(
    question: str,
    options: list[tuple[str, str]],
    correct_letter: str,
    explanation: str,
) -> PeerFlaw | None:
    """A plausible-but-wrong peer solution for the 'correct the peer' mode.
    Guaranteed to pick a wrong option (or None). None when unavailable."""
    user = (
        f"Question:\n{question}\n\nOptions:\n{_format_options(options)}\n\n"
        f"Correct answer (avoid this one): {correct_letter}\n\n"
        f"Reference explanation:\n{explanation or '(none provided)'}\n\n"
        "Give a confident but flawed solution ending on a wrong option."
    )
    data = _chat_json(_PEER_FLAW_PROMPT, user)
    if not data:
        return None
    choice = str(data.get("choice", "")).strip().upper()[:1]
    reasoning = str(data.get("reasoning", "")).strip()
    # Safety: must be a real, wrong choice with reasoning.
    if not choice or not reasoning or choice == (correct_letter or "").strip().upper():
        return None
    return PeerFlaw(choice=choice, reasoning=reasoning)


def critique_check(
    question: str,
    correct_letter: str,
    explanation: str,
    flawed_reasoning: str,
    student_critique: str,
) -> Critique | None:
    """Judge the student's critique of the peer's flawed solution. None when
    unavailable."""
    user = (
        f"Question:\n{question}\n\nCorrect answer: {correct_letter}\n\n"
        f"Reference explanation:\n{explanation or '(none provided)'}\n\n"
        f"Peer's flawed solution:\n{flawed_reasoning}\n\n"
        f"Student's critique:\n{student_critique}\n\n"
        "Did the student correctly identify the flaw or the right approach?"
    )
    data = _chat_json(_CRITIQUE_PROMPT, user)
    if data is None:
        return None
    return Critique(
        found_flaw=bool(data.get("found_flaw")),
        feedback=str(data.get("feedback", "")).strip(),
    )
