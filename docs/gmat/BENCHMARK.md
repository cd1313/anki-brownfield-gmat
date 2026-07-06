# GMAT Anki — Speed Benchmark

One-command backend latency benchmark (spec §7h / §10). Regenerate with:

```
just bench                 # full run (default 50,000 cards)
just bench --cards 2000    # fast smoke run
```

## Run parameters

- Deck size: **50,000 cards** (GMAT MCQ notes, spread evenly across GMAT::Quant::Algebra, GMAT::Verbal::CR, GMAT::DataInsights::Tables)
- Seeded graded attempts before timing: **3,000**
- Iterations: 200 for fast RPCs, 100 for dashboard calls
- Deck build time: 9.3s; attempt-seeding time: 0.2s
- Machine: `macOS-14.4.1-arm64-arm-64bit-Mach-O | 12 logical CPUs`
- Random seed: 20260701

## Results

| Action                                   | p50 (ms) | p95 (ms) | worst (ms) | iters | §10 target    | Result |
| ---------------------------------------- | -------: | -------: | ---------: | ----: | ------------- | :----: |
| grade_mcq (button press ack)             |     0.06 |     0.09 |       1.29 |   200 | p95 < 50 ms   |  PASS  |
| next_practice_card (adaptive)            |   619.53 |   641.99 |     658.82 |   200 | p95 < 100 ms  |  FAIL  |
| get_topic_mastery (dashboard first load) |   437.27 |   437.27 |     437.27 |     1 | p95 < 1000 ms |  PASS  |
| get_topic_mastery (dashboard refresh)    |   438.99 |   464.27 |     477.50 |   100 | p95 < 500 ms  |  PASS  |
| estimate_readiness (dashboard)           |   514.81 |   539.02 |     556.40 |   100 | —             |  info  |

## §10 targets

- `grade_mcq` (button press ack): p95 < 50 ms
- `next_practice_card` (next card after grading): p95 < 100 ms
- `get_topic_mastery` (dashboard refresh): p95 < 500 ms; first load < 1000 ms
- `estimate_readiness`: informational (part of dashboard)

## Honesty note

These are single-machine developer numbers, **not** the reference-machine figures from the spec. Percentiles are computed over the stated iteration count using a nearest-rank estimator (`int(q*(n-1))`), and timing uses `time.perf_counter()`. The "dashboard first load" row is a single cold call, so it has no distribution. Absolute numbers will vary with hardware, load, and build profile.
