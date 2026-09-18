# 06: A late sweep catches up without distorting dates

**What to build:** *Mostly delivered by ticket 01, but one acceptance criterion below was
invalidated by a decision ticket 01 made, and must be rewritten before this is picked up.*

If the sweep does not run for three nights, the bonuses still land on the dates they were
due, each dated its own anniversary rather than the catch-up date, so each runs its own
twelve-month expiry from the anniversary (LB9, D19, D20). Ticket 01 shipped this: a
catch-up sweep walks every anniversary a lot has reached and stamps each bonus at the start
of its own anniversary date.

**⚠️ The third criterion below is no longer implementable as written.** It said:

> The sweep never vests past `min(swept_at, clock.now())` — a lot due tonight at 03:00 is
> not vested by a sweep run at 02:00.

That only holds if an anniversary is an **instant**. Ticket 01 settled open question 2 the
other way — an anniversary is a **date**, consistent with LB11's calendar-based clamping —
so a sweep run at 02:00 on the anniversary date *does* vest, and verifying this confirmed
it. The criterion is not a bug to fix; it is a leftover from the reading the spec work
rejected. **Rewrite it, or delete it, before starting.** Whoever picks this up should
confirm the date reading with the spec owner rather than assume ticket 01 got it right.

**Amount, not date.** The catch-up *dates* are correct; the catch-up *amounts* are not,
when a withdrawal falls between an anniversary and the late sweep. That defect is owned by
ticket 03, so take 03 first or the timelines here will encode the wrong numbers.

**Blocked by:** 03 (A partially withdrawn lot vests on its surviving portion) — for any
timeline combining a late sweep with a withdrawal.

**Status:** blocked — needs the third criterion rewritten and ticket 03 landed

- [ ] Anniversary 15 Mar 2027 with the sweep missing 15, 16 and 17 Mar: catching up
      processes those dates in order and dates the bonus 15 Mar, so its expiry runs from
      15 Mar (example G, LB9, D20). Passes today; pin it.
- [ ] Running any of those business dates twice vests once and reports zeros (T6).
- [ ] ~~The sweep never vests past `min(swept_at, clock.now())`~~ — **rewrite this**, see
      above. What is still true and worth asserting: the sweep refuses a business date that
      has not happened yet, and reads the money side no later than the horizon.
- [ ] Catching two dates up in order lands on the state each night would have left (D20),
      including when a withdrawal falls between them — that is ticket 03's fix, asserted
      from here.
- [ ] Assertions at the seam only (T1); clock injected (T2); time travel is sweeping each
      elapsed business date, never a shortcut (T3).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
