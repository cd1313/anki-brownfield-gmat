#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html
"""One-command speed benchmark for the GMAT Anki fork (spec §7h/§10).

Builds a large temp collection (default 50,000 cards), then times each key
backend action many times and reports p50 / p95 / worst (max) in milliseconds,
with a pass/fail column against the §10 latency targets. Results are printed to
stdout and written to docs/gmat/BENCHMARK.md.

Run via the justfile recipe:

    just bench                 # full 50k-card run
    just bench --cards 2000    # fast smoke run
    just bench --iters 50      # override iteration counts
"""

from __future__ import annotations

import argparse
import os
import platform
import random
import shutil
import tempfile
import time
from dataclasses import dataclass

from _md import format_md  # type: ignore[import-not-found]

from anki.collection import Collection
from anki.decks import DeckId

# Sections cards are spread across, so section-based RPCs have real data.
SECTIONS = [
    "GMAT::Quant::Algebra",
    "GMAT::Verbal::CR",
    "GMAT::DataInsights::Tables",
]
# Per-section probability of a correct grade, to create genuine ability spread
# for the IRT / mastery estimators to chew on.
SECTION_P_CORRECT = {
    "GMAT::Quant::Algebra": 0.50,
    "GMAT::Verbal::CR": 0.72,
    "GMAT::DataInsights::Tables": 0.61,
}

SEARCH = 'note:"GMAT MCQ"'
SEED = 20260701


# --------------------------------------------------------------------------- #
# Stats helpers
# --------------------------------------------------------------------------- #


def _percentile(sorted_vals: list[float], q: float) -> float:
    """Nearest-rank percentile on an already-sorted list (no numpy)."""
    n = len(sorted_vals)
    if n == 0:
        return 0.0
    if n == 1:
        return sorted_vals[0]
    idx = int(q * (n - 1))
    return sorted_vals[idx]


@dataclass
class Stat:
    name: str
    unit: str  # human label for what "one op" is
    samples: list[float]
    target_ms: float | None  # None => informational only
    note: str = ""

    @property
    def p50(self) -> float:
        return _percentile(sorted(self.samples), 0.50)

    @property
    def p95(self) -> float:
        return _percentile(sorted(self.samples), 0.95)

    @property
    def worst(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def passed(self) -> bool | None:
        if self.target_ms is None:
            return None
        return self.p95 <= self.target_ms

    @property
    def verdict(self) -> str:
        p = self.passed
        if p is None:
            return "info"
        return "PASS" if p else "FAIL"


def _time_loop(fn, iters: int) -> list[float]:
    """Run fn() `iters` times, returning per-call elapsed times in ms."""
    out: list[float] = []
    for _ in range(iters):
        t0 = time.perf_counter()
        fn()
        out.append((time.perf_counter() - t0) * 1000.0)
    return out


# --------------------------------------------------------------------------- #
# Collection / deck construction
# --------------------------------------------------------------------------- #


def _build_notetype(col: Collection):
    mm = col.models
    nt = mm.new("GMAT MCQ")
    for f in ["Question", "A", "B", "C", "D", "E", "Answer", "Explanation"]:
        mm.add_field(nt, mm.new_field(f))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = "{{Question}}"
    tmpl["afmt"] = "{{Question}}<hr>{{Answer}}"
    mm.add_template(nt, tmpl)
    mm.add(nt)
    return nt


def build_deck(col: Collection, n_cards: int) -> list[int]:
    """Create `n_cards` GMAT MCQ notes spread across the sections.

    Returns the list of card ids in creation order.
    """
    nt = _build_notetype(col)
    card_ids: list[int] = []
    deck = DeckId(1)
    for i in range(n_cards):
        section = SECTIONS[i % len(SECTIONS)]
        note = col.new_note(nt)
        note["Question"] = f"Question {i}: which option is correct?"
        note["A"] = "alpha"
        note["B"] = "beta"
        note["C"] = "gamma"
        note["D"] = "delta"
        note["E"] = "epsilon"
        note["Answer"] = "C"
        note["Explanation"] = "Because gamma."
        note.tags = [section]
        col.add_note(note, deck_id=deck)
        card_ids.append(note.cards()[0].id)
    return card_ids


def seed_attempts(
    col: Collection, card_ids: list[int], n_attempts: int, rng: random.Random
) -> None:
    """Grade ~n_attempts MCQs spread across sections so the IRT/mastery calls
    have real work to do. Correctness is section-weighted for ability spread."""
    total = len(card_ids)
    n = min(n_attempts, total)
    # Evenly spread across the deck (which is round-robin over sections). The
    # deck position recovers the section, so we can weight correctness per section.
    step = max(1, total // n)
    for k, cid in enumerate(card_ids[::step][:n]):
        pos = k * step
        section = SECTIONS[pos % len(SECTIONS)]
        p_correct = SECTION_P_CORRECT[section]
        chosen = "C" if rng.random() < p_correct else "A"
        took = rng.randint(1500, 4500)
        col._backend.grade_mcq(card_id=cid, chosen=chosen, took_millis=took)


# --------------------------------------------------------------------------- #
# Benchmark
# --------------------------------------------------------------------------- #


def run_benchmark(
    n_cards: int, iters_fast: int, iters_dash: int
) -> tuple[list[Stat], dict]:
    rng = random.Random(SEED)
    tmpdir = tempfile.mkdtemp(prefix="gmat_bench_")
    col_path = os.path.join(tmpdir, "bench.anki2")
    col = Collection(col_path)
    stats: list[Stat] = []
    meta: dict = {}
    try:
        t0 = time.perf_counter()
        card_ids = build_deck(col, n_cards)
        meta["build_secs"] = time.perf_counter() - t0

        # Warm the estimators with a few thousand graded attempts.
        n_seed = min(3000, n_cards)
        t0 = time.perf_counter()
        seed_attempts(col, card_ids, n_seed, rng)
        meta["seed_secs"] = time.perf_counter() - t0
        meta["seed_attempts"] = n_seed

        # 1) grade_mcq single call — the "button press ack".
        #    took_millis=3000 => also logs the attempt, as a real button does.
        grade_cids = [rng.choice(card_ids) for _ in range(iters_fast)]
        gi = iter(grade_cids)
        stats.append(
            Stat(
                name="grade_mcq (button press ack)",
                unit="1 grade",
                samples=_time_loop(
                    lambda: col._backend.grade_mcq(
                        card_id=next(gi), chosen="C", took_millis=3000
                    ),
                    iters_fast,
                ),
                target_ms=50.0,
                note="also logs the attempt (took_millis=3000)",
            )
        )

        # 2) next_practice_card (adaptive / IRT-weighted) — next card after grade.
        stats.append(
            Stat(
                name="next_practice_card (adaptive)",
                unit="1 draw",
                samples=_time_loop(
                    lambda: col._backend.next_practice_card(
                        search=SEARCH, cycle=1, tag_prefix="GMAT"
                    ),
                    iters_fast,
                ),
                target_ms=100.0,
            )
        )

        # 3a) dashboard first load — the very first get_topic_mastery call.
        t0 = time.perf_counter()
        col._backend.get_topic_mastery(
            search="",
            tag_prefix="GMAT",
            r_threshold=0.8,
            time_budget_secs=60,
            min_reviews=1,
            min_cards=1,
        )
        first_load_ms = (time.perf_counter() - t0) * 1000.0
        stats.append(
            Stat(
                name="get_topic_mastery (dashboard first load)",
                unit="1 call",
                samples=[first_load_ms],
                target_ms=1000.0,
                note="single cold call",
            )
        )

        # 3b) get_topic_mastery whole collection — dashboard refresh.
        stats.append(
            Stat(
                name="get_topic_mastery (dashboard refresh)",
                unit="1 call",
                samples=_time_loop(
                    lambda: col._backend.get_topic_mastery(
                        search="",
                        tag_prefix="GMAT",
                        r_threshold=0.8,
                        time_budget_secs=60,
                        min_reviews=1,
                        min_cards=1,
                    ),
                    iters_dash,
                ),
                target_ms=500.0,
            )
        )

        # 4) estimate_readiness — part of the dashboard (informational).
        stats.append(
            Stat(
                name="estimate_readiness (dashboard)",
                unit="1 call",
                samples=_time_loop(
                    lambda: col._backend.estimate_readiness(
                        search="",
                        tag_prefix="GMAT",
                        time_budget_secs=120,
                        section_minutes=45,
                        min_responses=1,
                        min_coverage=0.0,
                        max_se=99.0,
                    ),
                    iters_dash,
                ),
                target_ms=None,
                note="informational",
            )
        )

        meta["n_cards"] = n_cards
        meta["iters_fast"] = iters_fast
        meta["iters_dash"] = iters_dash
        return stats, meta
    finally:
        col.close()
        shutil.rmtree(tmpdir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def _machine_note() -> str:
    return f"{platform.platform()} | {os.cpu_count()} logical CPUs"


def render_table(stats: list[Stat]) -> str:
    header = (
        "| Action | p50 (ms) | p95 (ms) | worst (ms) | iters | §10 target | Result |\n"
        "|---|---:|---:|---:|---:|---|:---:|"
    )
    rows = [header]
    for s in stats:
        target = "—" if s.target_ms is None else f"p95 < {s.target_ms:.0f} ms"
        rows.append(
            f"| {s.name} | {s.p50:.2f} | {s.p95:.2f} | {s.worst:.2f} "
            f"| {len(s.samples)} | {target} | {s.verdict} |"
        )
    return "\n".join(rows)


def render_markdown(stats: list[Stat], meta: dict) -> str:
    lines = [
        "# GMAT Anki — Speed Benchmark",
        "",
        "One-command backend latency benchmark (spec §7h / §10). Regenerate with:",
        "",
        "```",
        "just bench                 # full run (default 50,000 cards)",
        "just bench --cards 2000    # fast smoke run",
        "```",
        "",
        "## Run parameters",
        "",
        f"- Deck size: **{meta['n_cards']:,} cards** (GMAT MCQ notes, spread evenly "
        f"across {', '.join(SECTIONS)})",
        f"- Seeded graded attempts before timing: **{meta['seed_attempts']:,}**",
        f"- Iterations: {meta['iters_fast']} for fast RPCs, {meta['iters_dash']} for dashboard calls",
        f"- Deck build time: {meta['build_secs']:.1f}s; attempt-seeding time: {meta['seed_secs']:.1f}s",
        f"- Machine: `{_machine_note()}`",
        f"- Random seed: {SEED}",
        "",
        "## Results",
        "",
        render_table(stats),
        "",
        "## §10 targets",
        "",
        "- `grade_mcq` (button press ack): p95 < 50 ms",
        "- `next_practice_card` (next card after grading): p95 < 100 ms",
        "- `get_topic_mastery` (dashboard refresh): p95 < 500 ms; first load < 1000 ms",
        "- `estimate_readiness`: informational (part of dashboard)",
        "",
        "## Honesty note",
        "",
        "These are single-machine developer numbers, **not** the reference-machine "
        "figures from the spec. Percentiles are computed over the stated iteration "
        "count using a nearest-rank estimator (`int(q*(n-1))`), and timing uses "
        '`time.perf_counter()`. The "dashboard first load" row is a single cold '
        "call, so it has no distribution. Absolute numbers will vary with hardware, "
        "load, and build profile.",
        "",
    ]
    return "\n".join(lines)


def render_stdout(stats: list[Stat], meta: dict) -> str:
    lines = [
        "",
        "=" * 72,
        "GMAT Anki Speed Benchmark (spec §7h/§10)",
        "=" * 72,
        f"Deck size    : {meta['n_cards']:,} cards",
        f"Seeded grades: {meta['seed_attempts']:,}",
        f"Iterations   : fast={meta['iters_fast']}  dashboard={meta['iters_dash']}",
        f"Build/seed   : {meta['build_secs']:.1f}s / {meta['seed_secs']:.1f}s",
        f"Machine      : {_machine_note()}",
        "-" * 72,
        render_table(stats),
        "-" * 72,
    ]
    n_targets = sum(1 for s in stats if s.target_ms is not None)
    n_pass = sum(1 for s in stats if s.passed is True)
    lines.append(f"Targets passed: {n_pass}/{n_targets}")
    lines.append("=" * 72)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main() -> int:
    ap = argparse.ArgumentParser(description="GMAT Anki speed benchmark")
    ap.add_argument(
        "--cards", type=int, default=50000, help="deck size to build (default 50000)"
    )
    ap.add_argument(
        "--iters",
        type=int,
        default=None,
        help="override iteration count for all actions "
        "(default: 200 fast RPCs / 100 dashboard)",
    )
    args = ap.parse_args()

    iters_fast = args.iters if args.iters is not None else 200
    iters_dash = args.iters if args.iters is not None else 100

    print(f"Building {args.cards:,}-card collection and benchmarking...", flush=True)
    stats, meta = run_benchmark(args.cards, iters_fast, iters_dash)

    print(render_stdout(stats, meta))

    # Write the markdown report.
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.abspath(os.path.join(here, "..", ".."))
    out_dir = os.path.join(repo_root, "docs", "gmat")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "BENCHMARK.md")
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(format_md(render_markdown(stats, meta)))
    print(f"\nWrote {out_path}")

    # The benchmark itself succeeded (it produced a full report); per-action
    # PASS/FAIL vs §10 targets is reported as data in the table, not as the
    # tool's exit status, so `just bench` stays green even when a latency
    # target is missed on a given machine.
    n_targets = sum(1 for s in stats if s.target_ms is not None)
    n_pass = sum(1 for s in stats if s.passed is True)
    if n_pass < n_targets:
        print(
            f"NOTE: {n_targets - n_pass}/{n_targets} §10 target(s) missed on this machine (see table)."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
