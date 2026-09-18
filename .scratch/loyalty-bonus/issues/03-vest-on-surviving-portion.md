# 03: A partially withdrawn lot vests on its surviving portion

**What to build:** Anke's €1,200 deposit with €1,100 still outstanding pays **110** on its
anniversary — not 120, and not 0. This is the ambiguity the spec work resolved (LB5, D25):
a withdrawal consumes lots oldest first and splits the one it only partially covers; the
withdrawn part forfeits the year in progress, and the surviving part keeps its **original**
anniversary date. The deposit ledger already splits lots this way and `next_anniversary` is
already derived from the original `deposited_at`, so this ticket is mostly about vesting
against outstanding amounts rather than deposited ones.

**Blocked by:** 01 (An untouched deposit vests its first anniversary bonus) — needs the
vesting pass. Independent of 02; they touch different code and can run in parallel.

**Status:** ready-for-agent

- [ ] €1,200 deposited, €100 withdrawn, anniversary reached → vests 110 (example C). An
      implementation reading the deposited amount returns 120 — that is the first failing
      test.
- [ ] The anniversary date is unchanged by the split: the withdrawal does not restart the
      clock (LB5).
- [ ] FIFO across lots: €500 on 1 Jan 2026 and €500 on 1 Jun 2026, €600 withdrawn 1 Sep
      2026 → the January lot vests nothing on 1 Jan 2027, the June lot vests 40 on its
      surviving €400 on 1 Jun 2027 (example E).
- [ ] Double flooring: a surviving €30.50 floors to 30 base points, and 10% of that floors
      to 3 (LB4, example B).
- [ ] Bonus rules stay table-driven: timelines in, expected vested out (T4).
- [ ] Re-runnability asserted on every sweep test (T6); assertions at the seam only (T1);
      clock injected (T2); time travel by sweeping each elapsed business date (T3).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
