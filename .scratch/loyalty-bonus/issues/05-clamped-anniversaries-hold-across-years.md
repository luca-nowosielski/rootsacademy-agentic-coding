# 05: Clamped anniversaries hold across years

**What to build:** A 29 February deposit vests on 28 February in common years and returns
to 29 February in the next leap year. Clamping never advances into March, and a clamped
year does not shift the underlying anniversary for later years (LB11, D27).

**Expect little or no production code.** `anniversary_after()` already behaves correctly —
a 29 Feb 2028 deposit gives 28 Feb 2029, 28 Feb 2030, 28 Feb 2031, then 29 Feb 2032,
because `add_months` measures from the original date each time rather than from the clamped
one. This ticket is mostly about pinning that behaviour through the vesting path so a later
refactor cannot quietly break it. If the test passes on arrival, say so in the ticket
rather than manufacturing a failure.

**Blocked by:** 01 (An untouched deposit vests its first anniversary bonus).

**Status:** ready-for-agent

- [ ] The four-year table of example F holds end to end through the sweep: vests land on
      28 Feb 2029, 28 Feb 2030, 28 Feb 2031 and 29 Feb 2032.
- [ ] A clamped year never advances into March (LB11).
- [ ] Table-driven (T4); every date calculation in Europe/Brussels (D5).
- [ ] Re-runnability asserted (T6); assertions at the seam only (T1); clock injected (T2).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
