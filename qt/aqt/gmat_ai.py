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
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

_DEFAULT_MODEL = "gpt-4o-mini"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"

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


def ai_available() -> bool:
    """True when a key is present. (A per-collection enable toggle is checked
    separately by the caller via ``col.conf``.)"""
    return bool(api_key())


def grade(
    question: str,
    expected: str,
    answer: str,
    timeout: float = 8.0,
) -> GradeResult | None:
    """Grade ``answer`` against ``expected``. Returns ``None`` on any failure so
    the caller can fall back to self-rating."""
    key = api_key()
    if not key:
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
    body = json.dumps(
        {
            "model": model(),
            "messages": [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "temperature": 0,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        _OPENAI_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        content = payload["choices"][0]["message"]["content"]
        parsed = json.loads(content)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError):
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
