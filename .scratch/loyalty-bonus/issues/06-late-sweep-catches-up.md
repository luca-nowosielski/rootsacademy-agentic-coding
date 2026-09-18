# 06: A late sweep catches up without distorting dates

**What to build:** If the sweep does not run for three nights, the bonuses still land on
the dates they were due. The bonus lot is dated the anniversary, not the catch-up date, so
its own twelve-month expiry runs from the anniversary (LB9, D19, D20).

**Blocked by:** 01 (An untouched deposit vests its first anniversary bonus).

**Status:** ready-for-agent

- [ ] Anniversary 15 Mar 2027 with the sweep missing 15, 16 and 17 Mar: catching up
      processes those dates in order and dates the bonus 15 Mar, so its expiry runs from
      15 Mar (example G, LB9, D20). Asserting the bonus lot's expiry is twelve months after
      the *anniversary* rather than after the catch-up date is the first failing test.
- [ ] Running any of those business dates twice vests once and the second `SweepResult`
      reports zeros (T6, example G).
- [ ] The sweep never vests past `min(swept_at, clock.now())`: a business date whose sweep
      has not happened yet is not vested early.
- [ ] Catching two dates up in order lands on the state each night would have left (D20).
- [ ] Assertions at the seam only (T1); clock injected (T2); time travel is sweeping each
      elapsed business date, never a shortcut (T3).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
