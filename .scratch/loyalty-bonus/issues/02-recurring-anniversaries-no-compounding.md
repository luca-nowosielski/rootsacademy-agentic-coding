# 02: The clock recurs, and never compounds

**What to build:** The same untouched deposit pays 100 again in year two and year three —
not 110, not 121. Each anniversary vests 10% of the lot's **base** points, never of
base-plus-accrued (LB3). A customer who leaves a deposit alone reaches a steady state where
they hold exactly one year's bonus at a time, because each year's vest lands on the same
date the previous year's expires, and the vest-before-expire ordering guarantees that order
(LB10).

**Blocked by:** 01 (An untouched deposit vests its first anniversary bonus) — needs the
vesting pass at the sweep seam.

**Status:** ready-for-agent

- [ ] A three-year timeline on a €1,000 deposit yields the vested series `[100, 100, 100]`
      (example A, LB3). A compounding implementation returns `[100, 110, 121]` — that is the
      first failing test.
- [ ] Steady state: after the first anniversary the customer holds exactly one year's bonus
      at any time (example A, LB10).
- [ ] Table-driven across at least three deposit sizes: timelines in, expected vested out
      (T4).
- [ ] Every sweep test asserts re-runnability — the same business date twice leaves the same
      state and reports zeros (T6).
- [ ] Assertions at the D42 seam only (T1); clock injected, no wall-clock reads (T2); time
      travel is advancing the clock and sweeping each elapsed business date (T3).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
