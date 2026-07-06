#!/usr/bin/env python3
# Copyright: Ankitects Pty Ltd and contributors
# License: GNU AGPL, version 3 or later; http://www.gnu.org/licenses/agpl.html

"""Crash-recovery test for the GMAT Anki fork (spec §7g).

Kill the app mid-review N times in a row (default 20) with an uncatchable
SIGKILL, then verify that the *same* on-disk collection reopens cleanly and
passes an integrity check every time -- i.e. zero corrupted collections.

The realistic corruption scenario is a single collection file that is being
written to when the process dies, so we deliberately reuse ONE collection
file across every iteration rather than a fresh copy each time.

Usage (via the justfile recipe):

    just crash-test              # full 20-iteration run
    just crash-test --iters 3    # quick smoke test

The child worker mode (--worker <path>) is spawned internally and should not
be invoked by hand.
"""

from __future__ import annotations

import argparse
import os
import random
import select
import subprocess
import sys
import tempfile
import time

from _md import format_md  # type: ignore[import-not-found]

# Make sure the child (and this process) can import anki from out/pylib even
# if PYTHONPATH was not inherited for some reason.
_OUT_PYLIB = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "out", "pylib")
)
if os.path.isdir(_OUT_PYLIB) and _OUT_PYLIB not in sys.path:
    sys.path.insert(0, _OUT_PYLIB)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

MCQ_NOTETYPE = "GMAT MCQ"
MCQ_SEARCH = f'note:"{MCQ_NOTETYPE}"'
WORKER_READY = "WORKER_STARTED"


def _open_collection(path: str):
    from anki.collection import Collection

    return Collection(path)


def _add_mcq_notetype(col):
    """Create the GMAT MCQ notetype (Question/Answer), mirroring test_gmat.py."""
    mm = col.models
    nt = mm.new(MCQ_NOTETYPE)
    for field in ["Question", "Answer"]:
        mm.add_field(nt, mm.new_field(field))
    tmpl = mm.new_template("Card 1")
    tmpl["qfmt"] = "{{Question}}"
    tmpl["afmt"] = "{{Question}}<hr>{{Answer}}"
    mm.add_template(nt, tmpl)
    mm.add(nt)
    return nt


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def seed_collection(path: str, normal_notes: int, mcq_cards: int) -> None:
    """Create the persistent collection and populate it, then close cleanly."""
    col = _open_collection(path)
    try:
        # ~200 normal notes using the default notetype.
        for i in range(normal_notes):
            note = col.newNote()
            note["Front"] = f"Front {i}"
            note["Back"] = f"Back {i}"
            col.addNote(note)

        # A "GMAT MCQ" notetype with ~50 MCQ cards.
        nt = _add_mcq_notetype(col)
        for i in range(mcq_cards):
            note = col.new_note(nt)
            note["Question"] = f"Q{i}: 2 + 2 = ?"
            note["Answer"] = "C"
            col.add_note(note, deck_id=1)
    finally:
        col.close(downgrade=False)


# ---------------------------------------------------------------------------
# Child worker: opens the collection and hammers it with writes until killed
# ---------------------------------------------------------------------------


def run_worker(path: str) -> int:
    """Open the collection and write to it in a tight loop, forever.

    Two kinds of real DB writes are interleaved:
      * scheduler writes  -- col.sched.answerCard(getCard(), 3)
      * GMAT grade writes -- col._backend.grade_mcq(..., took_millis>0) which
                             logs a revlog row on every call.

    grade_mcq always writes even when the review queue is empty, so the child
    is guaranteed to be busy-writing when the parent kills it.
    """
    col = _open_collection(path)

    # MCQ card ids used for grade_mcq writes.
    mcq_cids = list(col.find_cards(MCQ_SEARCH))

    # Signal the parent that we are up and about to start writing.
    print(WORKER_READY, flush=True)

    i = 0
    while True:
        # Scheduler write (best-effort; queue may be empty on later runs).
        try:
            card = col.sched.getCard()
            if card is not None:
                col.sched.answerCard(card, 3)
        except Exception:
            pass

        # GMAT grade write -- always writes a revlog row (took_millis > 0).
        if mcq_cids:
            cid = mcq_cids[i % len(mcq_cids)]
            chosen = "C" if (i % 2 == 0) else "A"
            try:
                col._backend.grade_mcq(card_id=cid, chosen=chosen, took_millis=1500)
            except Exception:
                pass

        i += 1


# ---------------------------------------------------------------------------
# Parent: spawn, kill mid-write, reopen, integrity-check
# ---------------------------------------------------------------------------


def _child_env() -> dict:
    env = dict(os.environ)
    existing = env.get("PYTHONPATH", "")
    parts = [p for p in existing.split(os.pathsep) if p]
    if _OUT_PYLIB not in parts:
        parts.insert(0, _OUT_PYLIB)
    env["PYTHONPATH"] = os.pathsep.join(parts)
    return env


def _wait_for_ready(proc: subprocess.Popen, timeout: float) -> bool:
    """Block until the worker prints WORKER_STARTED, or timeout/EOF."""
    deadline = time.time() + timeout
    assert proc.stdout is not None
    while time.time() < deadline:
        remaining = deadline - time.time()
        rlist, _, _ = select.select([proc.stdout], [], [], remaining)
        if not rlist:
            continue
        line = proc.stdout.readline()
        if line == b"":
            # EOF -- the child exited before signalling readiness.
            return False
        if WORKER_READY.encode() in line:
            return True
    return False


class IterationResult:
    def __init__(self, index: int):
        self.index = index
        self.passed = False
        self.detail = ""
        self.test_error = False  # child never started -> test error, not corruption


def run_iteration(
    index: int, col_path: str, rng: random.Random, script: str
) -> IterationResult:
    result = IterationResult(index)

    proc = subprocess.Popen(
        [sys.executable, script, "--worker", col_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_child_env(),
    )

    ready = _wait_for_ready(proc, timeout=60.0)
    if not ready:
        # Child crashed / never started writing: a test error, not corruption.
        try:
            proc.kill()
        except Exception:
            pass
        _, err = proc.communicate(timeout=30)
        result.test_error = True
        result.detail = (
            f"worker failed to start (exit={proc.returncode}); "
            f"stderr tail: {err.decode(errors='replace')[-500:].strip()}"
        )
        return result

    # Let the worker get well into its write loop, then kill mid-write.
    delay_ms = rng.randint(80, 350)
    time.sleep(delay_ms / 1000.0)

    if proc.poll() is not None:
        # The worker exited on its own before we could SIGKILL it: test error.
        _, err = proc.communicate(timeout=30)
        result.test_error = True
        result.detail = (
            f"worker exited on its own (exit={proc.returncode}) before kill; "
            f"stderr tail: {err.decode(errors='replace')[-500:].strip()}"
        )
        return result

    # Uncatchable, unclean kill -- lands mid-write.
    proc.kill()
    proc.wait(timeout=30)
    # Drain pipes so the OS buffers are freed.
    try:
        proc.communicate(timeout=30)
    except Exception:
        pass

    # Reopen the SAME on-disk collection and check integrity.
    try:
        col = _open_collection(col_path)
    except Exception as exc:  # a corrupt DB raises on open
        result.passed = False
        result.detail = f"CORRUPT: collection failed to reopen: {exc!r}"
        return result

    try:
        problems = list(col._backend.check_database())
    except Exception as exc:
        result.passed = False
        result.detail = f"CORRUPT: integrity check raised: {exc!r}"
        return result
    finally:
        try:
            col.close(downgrade=False)
        except Exception:
            pass

    if problems:
        result.passed = False
        result.detail = "CORRUPT: integrity problems: " + "; ".join(problems)
    else:
        result.passed = True
        result.detail = f"ok (killed after {delay_ms} ms mid-write)"
    return result


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def write_report(
    doc_path: str,
    iters: int,
    results: list[IterationResult],
    seed: int,
    normal_notes: int,
    mcq_cards: int,
) -> None:
    survived = sum(1 for r in results if r.passed)
    corrupted = sum(1 for r in results if not r.passed and not r.test_error)
    test_errors = sum(1 for r in results if r.test_error)

    lines: list[str] = []
    lines.append("# Crash-Recovery Test (spec §7g)")
    lines.append("")
    lines.append(
        f"**Result: {survived}/{iters} kills survived, "
        f"{corrupted} corrupted collections.**"
    )
    if test_errors:
        lines.append("")
        lines.append(
            f"> Note: {test_errors} iteration(s) were **test errors** "
            "(the worker never entered its write loop) and are excluded from "
            "the corruption count -- rerun to get a clean sample."
        )
    lines.append("")
    lines.append(f"_Generated: {time.strftime('%Y-%m-%d %H:%M:%S')}_")
    lines.append("")

    lines.append("## What this test does")
    lines.append("")
    lines.append(
        "One persistent collection file is created once and **reused across "
        f"all {iters} iterations**, so every kill/reopen exercises recovery of "
        "the same on-disk collection -- the realistic corruption scenario. The "
        f"collection is seeded with ~{normal_notes} normal notes (default "
        f"notetype) and a `GMAT MCQ` notetype with ~{mcq_cards} MCQ cards, then "
        "closed cleanly."
    )
    lines.append("")
    lines.append("Each iteration:")
    lines.append("")
    lines.append(
        "1. Spawns a **child process** that opens the collection and enters a "
        "tight loop of real DB writes: scheduler answers "
        "(`col.sched.answerCard(getCard(), 3)`) interleaved with GMAT grade "
        "writes (`col._backend.grade_mcq(..., took_millis=1500)`, which logs a "
        "revlog row every call). The child prints `WORKER_STARTED` and flushes "
        "so the parent knows it is busy-writing."
    )
    lines.append(
        "2. The parent waits a short randomized delay (80-350 ms, seeded) so "
        "the kill lands **mid-write**, then sends **SIGKILL** "
        "(`child.kill()`)."
    )
    lines.append(
        "3. The parent **reopens the same collection** and runs Anki's "
        "integrity check (`col._backend.check_database()`). A collection that "
        "fails to open, or reports any problems, is counted as **corrupted**."
    )
    lines.append("")

    lines.append("## How the kill works (SIGKILL mid-write)")
    lines.append("")
    lines.append(
        "`subprocess.Popen.kill()` delivers **SIGKILL (signal 9)**, which the "
        "process cannot catch, block, or clean up after -- the OS terminates it "
        "immediately. Because the child is looping on writes, termination "
        "almost always lands in the middle of an in-flight SQLite write "
        "transaction. This is a strictly harsher failure than a normal app "
        "crash or a power loss to the process (though not a full-machine power "
        "loss, which would also lose the OS page cache)."
    )
    lines.append("")

    lines.append("## Why the collection survives")
    lines.append("")
    lines.append(
        "Anki stores the collection in **SQLite**, which is crash-safe by design:"
    )
    lines.append("")
    lines.append(
        "- Every write happens inside a **transaction**. SQLite commits "
        "**atomically** -- a transaction is either fully applied or not applied "
        "at all. A process killed mid-write leaves the database in its "
        "last-committed state; the partial, uncommitted write is discarded."
    )
    lines.append(
        "- With the **WAL (write-ahead log)** journal mode, new pages are "
        "appended to the `-wal` file and only checkpointed into the main DB "
        "after a valid commit frame is written. On the next open, SQLite "
        "**replays only the committed frames** from the WAL and ignores any "
        "torn/trailing frame from the killed write, so the main database file "
        "is never left half-updated."
    )
    lines.append(
        "- On reopen, SQLite performs this recovery automatically before any "
        "query runs, which is why `check_database()` sees a consistent schema "
        "and data set every time."
    )
    lines.append("")

    lines.append("## Raw per-iteration results")
    lines.append("")
    lines.append(f"Seed: `{seed}`")
    lines.append("")
    lines.append("| Iter | Status | Detail |")
    lines.append("| ---: | :----- | :----- |")
    for r in results:
        if r.test_error:
            status = "TEST-ERR"
        elif r.passed:
            status = "PASS"
        else:
            status = "CORRUPT"
        detail = r.detail.replace("|", "\\|")
        lines.append(f"| {r.index} | {status} | {detail} |")
    lines.append("")

    os.makedirs(os.path.dirname(doc_path), exist_ok=True)
    with open(doc_path, "w", encoding="utf-8") as fh:
        fh.write(format_md("\n".join(lines)))


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", metavar="COL_PATH", help=argparse.SUPPRESS)
    parser.add_argument("--iters", type=int, default=20, help="kill iterations")
    parser.add_argument("--seed", type=int, default=1313, help="RNG seed")
    parser.add_argument(
        "--normal-notes", type=int, default=200, help="normal notes to seed"
    )
    parser.add_argument(
        "--mcq-cards", type=int, default=50, help="GMAT MCQ cards to seed"
    )
    parser.add_argument(
        "--doc",
        default=os.path.abspath(
            os.path.join(
                os.path.dirname(__file__), "..", "..", "docs", "gmat", "CRASH-TEST.md"
            )
        ),
        help="results markdown path",
    )
    args = parser.parse_args()

    # Child worker mode.
    if args.worker:
        return run_worker(args.worker)

    rng = random.Random(args.seed)
    script = os.path.abspath(__file__)

    tmpdir = tempfile.mkdtemp(prefix="gmat_crash_")
    col_path = os.path.join(tmpdir, "collection.anki2")

    print(f"Seeding persistent collection at {col_path} ...", flush=True)
    seed_collection(col_path, args.normal_notes, args.mcq_cards)

    print(f"Running {args.iters} SIGKILL-mid-write iterations ...", flush=True)
    results: list[IterationResult] = []
    for i in range(1, args.iters + 1):
        res = run_iteration(i, col_path, rng, script)
        results.append(res)
        if res.test_error:
            tag = "TEST-ERR"
        elif res.passed:
            tag = "PASS"
        else:
            tag = "CORRUPT"
        print(f"  [{i:>2}/{args.iters}] {tag}: {res.detail}", flush=True)

    survived = sum(1 for r in results if r.passed)
    corrupted = sum(1 for r in results if not r.passed and not r.test_error)
    test_errors = sum(1 for r in results if r.test_error)

    write_report(
        args.doc,
        args.iters,
        results,
        args.seed,
        args.normal_notes,
        args.mcq_cards,
    )

    print("")
    print(f"{survived}/{args.iters} kills survived, {corrupted} corrupted collections.")
    if test_errors:
        print(f"({test_errors} test error(s) -- worker never started; rerun.)")
    print(f"Report written to {args.doc}")

    # Nonzero exit if any corruption OR any test error occurred.
    if corrupted or test_errors:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
