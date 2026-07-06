# Crash-Recovery Test (spec §7g)

**Result: 20/20 kills survived, 0 corrupted collections.**

_Generated: 2026-07-05 16:33:04_

## What this test does

One persistent collection file is created once and **reused across all 20 iterations**, so every kill/reopen exercises recovery of the same on-disk collection -- the realistic corruption scenario. The collection is seeded with ~200 normal notes (default notetype) and a `GMAT MCQ` notetype with ~50 MCQ cards, then closed cleanly.

Each iteration:

1. Spawns a **child process** that opens the collection and enters a tight loop of real DB writes: scheduler answers (`col.sched.answerCard(getCard(), 3)`) interleaved with GMAT grade writes (`col._backend.grade_mcq(..., took_millis=1500)`, which logs a revlog row every call). The child prints `WORKER_STARTED` and flushes so the parent knows it is busy-writing.
2. The parent waits a short randomized delay (80-350 ms, seeded) so the kill lands **mid-write**, then sends **SIGKILL** (`child.kill()`).
3. The parent **reopens the same collection** and runs Anki's integrity check (`col._backend.check_database()`). A collection that fails to open, or reports any problems, is counted as **corrupted**.

## How the kill works (SIGKILL mid-write)

`subprocess.Popen.kill()` delivers **SIGKILL (signal 9)**, which the process cannot catch, block, or clean up after -- the OS terminates it immediately. Because the child is looping on writes, termination almost always lands in the middle of an in-flight SQLite write transaction. This is a strictly harsher failure than a normal app crash or a power loss to the process (though not a full-machine power loss, which would also lose the OS page cache).

## Why the collection survives

Anki stores the collection in **SQLite**, which is crash-safe by design:

- Every write happens inside a **transaction**. SQLite commits **atomically** -- a transaction is either fully applied or not applied at all. A process killed mid-write leaves the database in its last-committed state; the partial, uncommitted write is discarded.
- With the **WAL (write-ahead log)** journal mode, new pages are appended to the `-wal` file and only checkpointed into the main DB after a valid commit frame is written. On the next open, SQLite **replays only the committed frames** from the WAL and ignores any torn/trailing frame from the killed write, so the main database file is never left half-updated.
- On reopen, SQLite performs this recovery automatically before any query runs, which is why `check_database()` sees a consistent schema and data set every time.

## Raw per-iteration results

Seed: `1313`

| Iter | Status | Detail                             |
| ---: | :----- | :--------------------------------- |
|    1 | PASS   | ok (killed after 296 ms mid-write) |
|    2 | PASS   | ok (killed after 187 ms mid-write) |
|    3 | PASS   | ok (killed after 268 ms mid-write) |
|    4 | PASS   | ok (killed after 214 ms mid-write) |
|    5 | PASS   | ok (killed after 231 ms mid-write) |
|    6 | PASS   | ok (killed after 345 ms mid-write) |
|    7 | PASS   | ok (killed after 270 ms mid-write) |
|    8 | PASS   | ok (killed after 253 ms mid-write) |
|    9 | PASS   | ok (killed after 161 ms mid-write) |
|   10 | PASS   | ok (killed after 163 ms mid-write) |
|   11 | PASS   | ok (killed after 321 ms mid-write) |
|   12 | PASS   | ok (killed after 179 ms mid-write) |
|   13 | PASS   | ok (killed after 125 ms mid-write) |
|   14 | PASS   | ok (killed after 211 ms mid-write) |
|   15 | PASS   | ok (killed after 213 ms mid-write) |
|   16 | PASS   | ok (killed after 264 ms mid-write) |
|   17 | PASS   | ok (killed after 278 ms mid-write) |
|   18 | PASS   | ok (killed after 318 ms mid-write) |
|   19 | PASS   | ok (killed after 312 ms mid-write) |
|   20 | PASS   | ok (killed after 237 ms mid-write) |
