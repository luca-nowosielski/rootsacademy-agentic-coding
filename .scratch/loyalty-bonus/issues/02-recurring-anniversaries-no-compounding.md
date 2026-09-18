# 02: The clock recurs, and never compounds

**What to build:** *Behaviour delivered by ticket 01 — this is now a test-only ticket.*

The same untouched deposit pays 100 again in year two and year three, not 110 and not 121.
Each anniversary vests 10% of the lot's **base** points, never of base-plus-accrued (LB3).

**Why it is already delivered.** Ticket 01 implemented `anniversaries_through()`, which
returns every anniversary a lot has reached rather than only its first, and the vesting
pass pays each one from the lot's base. Recurrence could not honestly be left out: a sweep
that has been down for over a year would otherwise silently underpay. Verified against the
running service — a three-year timeline on a €1,000 deposit yields `[100, 100, 100]`.

So the ticket's original first failing test **passes on arrival**. Do not manufacture a
failure. Write the tests, record that they passed on arrival, and be suspicious rather than
satisfied: a rule with no failing test behind it is a rule nothing is holding in place.

**Blocked by:** nothing. Ticket 01 is merged.

**Status:** ready-for-agent

- [ ] A three-year timeline on a €1,000 deposit yields the vested series `[100, 100, 100]`
      (example A, LB3). Confirm a compounding implementation would return `[100, 110, 121]`
      by making the change locally and watching the test fail — then revert it. A test that
      has never failed is not yet a test.
- [ ] Steady state: after the first anniversary the customer holds exactly one year's bonus
      at any time (example A, LB10).
- [ ] Table-driven across at least three deposit sizes: timelines in, expected vested out
      (T4). This is the T4 debt ticket 01 left — its only table is the flooring one.
- [ ] Every sweep test asserts re-runnability (T6); assertions at the D42 seam only (T1);
      clock injected (T2); time travel is sweeping each elapsed business date (T3).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
