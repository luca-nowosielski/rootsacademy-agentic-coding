# 08: Reversal of a deposit whose bonus already vested

**What to build:** Nothing yet — this ticket is **not startable**. It records spec open
question 1 so it is not lost.

D11 claws back "exactly the points it credited" when a deposit is reversed. D26 protects
vested bonuses from *withdrawal* — but a reversal is a different event, and the spec does
not say whether a vested bonus survives a deposit that never really happened. The strict
reading claws back only the base points; the stronger reading says a deposit that never
stood should not have paid a bonus either.

A second product question sits nearby and may want deciding at the same time (spec open
question 3): example H means an untouched deposit earns 10% of its original amount every
year indefinitely, long after its base points expired, because deposit lots do not expire
(LB12). That follows from the spec as written. Product may want a cap, a maximum number of
anniversaries, or nothing at all.

**Blocked by:** A product decision on spec open question 1. Not blocked by any ticket.

**Status:** blocked — needs a decision before it can be made ready-for-agent

- [ ] A decision is recorded in `docs/loyalty-bonus-spec.md` resolving open question 1.
- [ ] Acceptance examples are written for the chosen reading before implementation starts.
