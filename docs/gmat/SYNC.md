# Sync & conflict resolution (spec §7b)

## What sync this fork uses

Desktop and the AnkiDroid companion share **one engine** and sync through Anki's
**built-in self-hosted sync server** — the same protocol AnkiWeb/AnkiMobile use, run
locally:

```
just sync-server            # SYNC_USER1='demo:demo' ./run --syncserver, listens on :8080
```

Point both clients at `http://<your-lan-ip>:8080` (desktop: Preferences → custom sync URL;
AnkiDroid: custom sync server). This fork adds **no** custom sync code — the rules below are
stock Anki (`rslib/src/sync/collection/`), which is exactly what the spec wants ("You may use
Anki's existing sync … reviews must flow between the two apps without losing or double-counting
data"). Because the scheduler and the GMAT engine both live in the shared Rust layer, a review
or MCQ attempt on either device produces the same kind of records, and they sync identically.

## The conflict rule (what actually happens)

Anki sync is **incremental, record-level, last-writer-wins**, with one important asymmetry
between _review logs_ and _card/note state_:

### 1. Review logs (incl. GMAT MCQ attempts): union-merged, never dropped

Every review — and every GMAT MCQ practice attempt, which is written as a non-scheduling
"cramming" revlog entry (`record_mcq_attempt`, `rslib/src/gmat/mod.rs:231`) — is a revlog row
keyed by a unique millisecond-timestamp id. On sync, revlog rows are merged with
`INSERT OR IGNORE` (`merge_revlog`, `rslib/src/sync/collection/chunks.rs:168`;
`rslib/src/storage/revlog/add.sql`). Consequences:

- Reviews done on **different** cards on each device → **all** merge in. Nothing is lost.
- Two devices' attempts on the **same** card while offline → both have **distinct** ids, so
  **both survive** the merge. Nothing is double-counted (only an exact-id collision is
  ignored, which can't happen across devices).

So **no review is ever lost or double-counted** — this is the guarantee the sync test (§7b,
part 1) checks, and it holds by construction.

### 2. Card / note state (due date, interval, FSRS memory): mtime last-writer-wins

Scheduling state is a single row per card. On sync, an incoming card/note is applied only if
the local side has **no** un-synced local edit, **or** the incoming row's modification time is
strictly newer (`add_or_update_card_if_newer` / `add_or_update_note_if_newer`,
`chunks.rs:182-217`, guarded by `is_pending_sync`, `chunks.rs:418`). So for the **same card
reviewed on both devices offline**:

> **Winner = the device whose review of that card has the later modification time (`mtime`).**
> Its scheduling/FSRS state (due date, interval, stability) wins; the other device's
> scheduling change for that card is discarded — but that device's **revlog entry still
> survives** (rule 1), so its attempt is not lost from history.

This is a clear, deterministic, documented winner (§7b, part 2). "Later review wins the card
state" is the correct choice: the most recent grade is the best estimate of current memory.

### 3. Divergence guard (not a silent merge)

After applying changes, sync runs a `SanityCheck` comparing client/server counts
(`sanity.rs`). If the two collections have diverged incompatibly (e.g. a schema change or a
forced full upload), it raises `SanityCheckFailed` and escalates to a **full sync**
(`normal.rs:40,103`) — one whole collection replaces the other, after the user confirms
direction. There is no partial/field-level 3-way merge; Anki prefers an explicit full sync
over a silent incorrect reconciliation.

## Honesty notes

- This is stock Anki sync; the fork's contribution is verifying it carries the **GMAT MCQ
  attempts** (cramming revlog rows) correctly across devices, which it does (rule 1).
- Real-time (sub-second) sync and conflict-free replicated merges are listed as **bonus**
  ideas in the spec (§13) and are out of scope here.
