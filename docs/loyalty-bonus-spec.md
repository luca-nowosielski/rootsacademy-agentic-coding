# Loyalty-rate bonus — agreed specification

Status: agreed rules and acceptance examples, ready for decomposition into tickets.
Derived from `project_starter/saving-streak-spec.md` D19–D28, with the ambiguities
below resolved explicitly. Where this document and the feature spec disagree, this
document governs; where it is silent, the feature spec governs.

## The rule in one line

Every deposit lot carries its own recurring 12-month clock. On each anniversary,
the portion of that lot still outstanding vests 10% of its base points.

## Agreed rules

- **LB1. Per-lot clock.** Each deposit lot's anniversary clock runs from its own
  deposit date and recurs every 12 months, indefinitely (D22). There is no
  account-level or customer-level clock.
- **LB2. Rate.** On each anniversary, the lot vests 10% of the base points of the
  portion still outstanding at that moment (D22).
- **LB3. No compounding.** Always 10% of base, never of base-plus-accrued. Year
  three on an untouched €100 lot pays 10 points, not 12 (D23).
- **LB4. Flooring, applied twice.** The surviving portion's base points are its
  surviving euros floored to a whole number (D4); the bonus is 10% of that, floored
  again (D24). A €5 lot vests 0. *This double-floor is a clarification recorded
  here — the feature spec states each floor separately but not their interaction.*
- **LB5. Partial withdrawal splits the lot.** A withdrawal consumes lots oldest
  first and splits any lot it only partially covers. The withdrawn portion forfeits
  the year in progress; the surviving portion keeps the **original** anniversary
  date and vests on schedule (D8, D25). Whole-lot forfeiture is rejected.
- **LB6. Vested bonuses are permanent.** A withdrawal forfeits only the year in
  progress. Nothing already vested is ever clawed back by a withdrawal (D26).
- **LB7. Forfeiture is an explicit fact.** When a withdrawal forfeits a year in
  progress, that is recorded in the ledger naming what was given up. Nothing is
  silently dropped (D28).
- **LB8. Vested points are ordinary points.** A vested bonus is a normal points
  lot earned on the anniversary date, with its own fresh 12-month expiry clock
  (D19). It is spendable, giftable and expirable like any other lot.
- **LB9. Vesting date is the anniversary, not the run time.** The bonus lot is
  dated the anniversary even if the sweep that materialises it runs later (D19,
  D20). A late sweep must not shorten or extend the bonus's expiry.
- **LB10. Vest before expire.** Within one sweep, vesting runs before expiry, so a
  bonus vesting tonight is never swept the same night (D21).
- **LB11. Anniversaries clamp.** An anniversary falling on a date that does not
  exist in the target month clamps to the last valid day of that month (D27).
- **LB12. Deposit lots do not expire.** Only points lots expire. A deposit left
  untouched keeps vesting every year indefinitely, long after its original base
  points have expired. See example H — this is a consequence, flagged deliberately.

## Acceptance examples

All amounts in euros, all points integers, all dates Europe/Brussels. "Vests" means
a points lot is created dated the anniversary.

### A. Untouched lot, three years

Given a €1,000 deposit on 15 Mar 2026 that is never withdrawn from:

| Date | Event | Points |
| --- | --- | --- |
| 15 Mar 2026 | Deposit | +1000 base, expires 15 Mar 2027 |
| 15 Mar 2027 | Anniversary 1 vests, then base expires | +100 bonus, −1000 expired |
| 15 Mar 2028 | Anniversary 2 vests, then 2027 bonus expires | +100 bonus, −100 expired |
| 15 Mar 2029 | Anniversary 3 vests, then 2028 bonus expires | +100 bonus, −100 expired |

Year three pays 100, not 121 (LB3). In steady state the customer holds exactly one
year's bonus: each year's vests on the same date the previous year's dies, and LB10
guarantees that order.

### B. Flooring

| Deposit | Base points | Bonus per anniversary |
| --- | --- | --- |
| €5.00 | 5 | 0 |
| €9.99 | 9 | 0 |
| €10.00 | 10 | 1 |
| €1,205.00 | 1205 | 120 |
| €30.50 surviving of a larger lot | 30 | 3 |

### C. Partial withdrawal, then anniversary

Given Anke's real demo data — €1,200 deposited 20 Jun 2026, €100 since withdrawn,
€1,100 outstanding:

- On 20 Jun 2027 the lot vests **110** points, not 120 and not 0 (LB2, LB5).
- The anniversary stays 20 Jun. The split does not restart the clock (LB5).
- The forfeited 10 points on the withdrawn €100 are recorded as an explicit
  forfeiture fact at withdrawal time (LB7).

### D. Withdrawal after vesting

Given €1,000 deposited 1 Feb 2026:

- 1 Feb 2027: vests 100.
- 1 Mar 2027: the customer withdraws the full €1,000.
- The 100 vested points remain spendable (LB6). No further anniversaries vest,
  because nothing of the lot survives.

### E. FIFO across two lots

Given lot 1 of €500 on 1 Jan 2026 and lot 2 of €500 on 1 Jun 2026, and a €600
withdrawal on 1 Sep 2026:

- The withdrawal drains lot 1 entirely, then €100 of lot 2 (LB5).
- 1 Jan 2027: lot 1 vests nothing — nothing survives.
- 1 Jun 2027: lot 2 vests **40** on its surviving €400.

### F. Leap-year clamping

Given a deposit on 29 Feb 2028:

| Anniversary | Date | Why |
| --- | --- | --- |
| 1 | 28 Feb 2029 | 2029 is a common year — clamps (LB11) |
| 2 | 28 Feb 2030 | common year |
| 3 | 28 Feb 2031 | common year |
| 4 | 29 Feb 2032 | leap year — the real date exists again |

Clamping never advances into March, and a clamped year does not shift the
underlying anniversary date for later years.

### G. Late and repeated sweeps

Given a lot with an anniversary on 15 Mar 2027, and the sweep failing to run on
15, 16 and 17 Mar:

- When the sweep next runs, it processes 15, 16 and 17 Mar in order (D20).
- The bonus lot is dated **15 Mar 2027**, not the catch-up date, so its own
  12-month expiry runs from 15 Mar (LB9).
- Running the sweep twice for 15 Mar vests the bonus **once** (D20, idempotent per
  business date).

### H. A bonus that outlives its base points

Given €1,000 deposited 10 Apr 2026 and never touched:

- 10 Apr 2027: vests 100, then the original 1,000 base points expire.
- 10 Apr 2028, 2029, 2030 …: still vests 100 every year.

The deposit lot lives in the money ledger, which has no expiry — only points lots
expire (LB12). A customer who never withdraws therefore earns 10% of their original
deposit every year forever. This follows from the spec as written and is recorded
here so the behaviour is a decision rather than an accident.

## Out of scope

Gifting (D29–D34), notifications including the vest and forfeiture warnings
(D35–D41), and streak multipliers. Bonus points are giftable once gifting exists,
but gifting is not part of this feature.

## Open questions

These need a decision before the tickets that touch them, and are deliberately
**not** resolved here.

1. **Reversal versus vested bonus.** D11 says a reversed deposit claws back
   "exactly the points it credited". If a deposit is reversed after one of its
   anniversaries has already vested, is the vested bonus also clawed back? The
   strict reading claws back only the base points; the stronger reading says a
   deposit that never stood should not have paid a bonus either. D26 protects
   vested bonuses from *withdrawal*, which is a different event from reversal.
2. **Is the anniversary a date or an instant?** A deposit at 03:30 on 15 Mar 2026
   has its 12-month instant fall *after* the 03:00 sweep on 15 Mar 2027.
   Recommendation: treat anniversaries as dates, consistent with LB11's clamping,
   so it vests in that morning's sweep.
3. **Is example H intended?** An indefinite 10%-per-year annuity on an untouched
   deposit is a real cost. Product may want a cap, a maximum number of
   anniversaries, or nothing at all.
