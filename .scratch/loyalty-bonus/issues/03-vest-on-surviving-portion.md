# 03: A partially withdrawn lot vests on its surviving portion

**What to build:** *Partly delivered by ticket 01. The remaining work is a real defect,
and it is the one piece of production code the bonus still gets wrong.*

Anke's €1,200 deposit with €1,100 still outstanding pays **110** on its anniversary — not
120, and not 0 (LB5, D25). That much works: ticket 01's vesting pass reads each lot's
outstanding amount rather than its deposited amount, and the deposit ledger already splits
lots on partial withdrawal and keeps the original anniversary. Verified against the running
service: example C vests 110, and example E's FIFO case vests nothing on the January lot
and 40 on the June lot's surviving €400.

**The defect this ticket owns.** `_vest_anniversaries` reads the lot's outstanding amount
**once, from the position at the sweep's horizon**, and then pays that same amount for
every anniversary that has fallen due:

```python
points = bonus_points_for(lot.outstanding_eur)   # read once, at `horizon`
if not points: continue
for anniversary in anniversaries_through(lot.deposited_at, business_date):
```

For a sweep running nightly the two are the same day, so nothing is visibly wrong. For a
sweep that has been down — which ticket 01 also shipped support for — a withdrawal made
after an anniversary passed **retroactively shrinks the bonus for that anniversary**. LB5
says the surviving portion "keeps the **original** anniversary date and vests on schedule";
the date is right and the amount is not. The error is one year's bonus per un-swept
anniversary.

**The fix is a design decision, not a patch.** The amount owed on an anniversary is the
portion standing **at that anniversary**, so the deposit-lot replay needs a time bound —
`DepositLedger.position` currently replays every entry on record whatever `as_of` says, and
its docstring defends that deliberately. Changing it touches a method every money-side read
goes through, so weigh: bounding `position` itself, versus a separate read for the sweep.
Record the reasoning either way.

**Blocked by:** nothing. Ticket 01 is merged.

**Status:** ready-for-agent

- [ ] **First failing test:** deposit €1,200; let the anniversary pass with no sweep;
      withdraw €100; *then* sweep. The anniversary owed 120 and today pays 110.
- [ ] A withdrawal made before the anniversary still reduces it: €1,200 less €100 withdrawn
      four months early vests 110 (example C).
- [ ] The anniversary date is unchanged by the split (LB5).
- [ ] FIFO across lots: €500 on 1 Jan 2026 and €500 on 1 Jun 2026, €600 withdrawn 1 Sep
      2026 → the January lot vests nothing, the June lot vests 40 (example E). Passes today;
      pin it.
- [ ] Double flooring: a surviving €30.50 floors to 30 base points, and 10% of that floors
      to 3 (LB4, example B).
- [ ] Table-driven: timelines in, expected vested out (T4).
- [ ] Re-runnability asserted (T6); assertions at the seam only (T1); clock injected (T2);
      time travel by sweeping each elapsed business date (T3).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
