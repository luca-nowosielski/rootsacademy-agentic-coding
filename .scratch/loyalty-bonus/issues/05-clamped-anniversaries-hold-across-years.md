# 05: Clamped anniversaries hold across years

**What to build:** *Behaviour delivered by ticket 01 — this is now a test-only ticket.*

A 29 February deposit vests on 28 February in common years and returns to 29 February in
the next leap year. Clamping never advances into March, and a clamped year does not shift
the underlying anniversary for later years (LB11, D27).

**Why it is already delivered.** This ticket always expected "little or no production
code", and that is now literally true. `add_months` measures from the original deposit date
each time rather than from the clamped one, and ticket 01's `anniversaries_through()`
inherits that. Verified end to end through the sweep against the running service: a 29 Feb
2028 deposit vests on 28 Feb 2029, 28 Feb 2030, 28 Feb 2031 and 29 Feb 2032.

The whole of example F therefore **passes on arrival**. Say so in the ticket rather than
manufacturing a failure — but do pin it, because nothing currently stops a refactor of
`add_months` from quietly breaking it.

**Blocked by:** nothing. Ticket 01 is merged.

**Status:** ready-for-agent

- [ ] The four-year table of example F holds end to end through the sweep: vests land on
      28 Feb 2029, 28 Feb 2030, 28 Feb 2031 and 29 Feb 2032.
- [ ] A clamped year never advances into March (LB11).
- [ ] The clamped bonus lot's own twelve-month expiry clamps the same way (LB8): a bonus
      vested 29 Feb 2032 expires 28 Feb 2033. Ticket 01 did not check this.
- [ ] Table-driven (T4); every date calculation in Europe/Brussels (D5).
- [ ] Re-runnability asserted (T6); assertions at the seam only (T1); clock injected (T2).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
