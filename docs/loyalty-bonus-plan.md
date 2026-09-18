# Loyalty-rate bonus — implementation plan

Ordered tickets for the spec in `loyalty-bonus-spec.md`. Each is a vertical slice
with a customer-visible outcome, not a layer. Rule references are `LB*` from that
spec; `D*` and `T*` are from `project_starter/saving-streak-spec.md`.

> **Status note — this plan is the original decomposition, not the current state.**
> LB-1 is merged, and it delivered more than its own slice: recurrence over
> successive anniversaries reaches into LB-2, LB-5 and LB-6, because a sweep that
> has been down for over a year would otherwise silently underpay. LB-2 and LB-5
> are now test-only; LB-3 keeps the one piece of production code the bonus still
> gets wrong; LB-6's third acceptance criterion was invalidated by LB-1 settling
> open question 2 and must be rewritten before it is picked up. The re-scoped
> tickets under `.scratch/loyalty-bonus/issues/` are the current truth; the
> sections below are kept as the record of what was planned.

## Where the feature lands

The groundwork is already in place, and the seam is pre-marked. Inside
`ApplicationService.run_daily_sweep`, within the existing `atomically()` block and
**before** the expiry loop, the code carries this comment:

> Spec D21: the ticket that adds the loyalty-rate bonus vests due deposit lots
> here, *before* anything expires, so a bonus vesting tonight is never swept the
> same night.

That is the insertion point. What already exists and should be used rather than
rebuilt: `DepositLedger.position()` returns each lot's outstanding amount and its
`next_anniversary`, already split correctly on partial withdrawal (D25);
`anniversary_after()` and `add_months()` already clamp month-ends (D27, verified);
the points ledger already mints lots with their own expiry clocks.

What does not exist yet: a `MovementReason` for vesting (the enum's own docstring
says the remaining reasons "arrive with the tickets that own them"), a deposit-side
reason for forfeiture, vesting counters on `SweepResult`, a query for lots due on or
before the sweep horizon, and the idempotency record that stops a vest being written
twice — the expiry path's `materialised_lot_ids` is the pattern to follow.

## Definition of done — every ticket

- `pytest` and `ruff check .` pass in `app/backend`; `npm run lint` and
  `npm run build` pass in `app/frontend`.
- Assertions sit at the D42 seam only. No test asserts on ledger rows, lot
  identifiers or the order of internal drains (T1).
- The clock is injected; no `sleep`, no wall-clock reads (T2).
- Time travel is advancing the clock and calling `run_daily_sweep` for each elapsed
  business date — never a test-only shortcut (T3).
- Every sweep test asserts re-runnability: the same business date twice leaves the
  same state, and the second `SweepResult` reports zeros (T6).
- Bonus rules are table-driven: timelines in, expected vested and forfeited out (T4).

---

## LB-1 — An untouched deposit vests its first anniversary bonus

**Outcome.** A customer who deposited €1,000 a year ago and has not withdrawn sees
100 points appear on the anniversary.

**Why first.** This is the single crossing between the two ledgers (D6) — a vesting
deposit lot minting a points lot — and it is the only genuinely risky seam in the
feature. It also cannot avoid the expiry interaction: base points earned on day zero
expire at exactly twelve months, which *is* the first anniversary. So the D21
ordering has to be right in this ticket, not a later one.

**Changes.** Vesting pass at the marked slot in `run_daily_sweep`, bounded by the
same `horizon = min(swept_at, clock.now())` the expiry pass uses. New
`MovementReason.VESTING`. `SweepResult` gains `anniversaries_vested` and
`points_vested`.
Deposit ledger gains a due-lots query and a materialised-vesting record keyed by
(lot, anniversary) for idempotency.

**Acceptance.**
- €1,000 on 15 Mar 2026; sweep 15 Mar 2027 → balance is 100. The 1,000 base points
  expired that same night, and the vest still happened (example A, LB10).
- The bonus lot is dated the anniversary and expires 15 Mar 2028 (LB8, LB9).
- Flooring: €5 → 0, €9.99 → 0, €10 → 1, €1,205 → 120 (example B, LB4).
- Re-running 15 Mar 2027 vests nothing more and reports zeros (T6).

**First failing test.** Deposit €1,000, advance the injected clock twelve months,
run the sweep for that date, assert the balance at the seam is 100. Before the
change it is 0, because the base expired and nothing vested.

---

## LB-2 — The clock recurs, and never compounds

**Outcome.** The same deposit pays 100 again in year two and year three — not 110,
not 121.

**Changes.** Recurrence over successive anniversaries; the rate is always 10% of the
lot's base, never of base-plus-accrued (LB3).

**Acceptance.**
- Three-year timeline yields `[100, 100, 100]` (example A, LB3).
- Steady state: the customer holds exactly one year's bonus, because each year's
  vest lands on the same date the previous year's expires, in that order (LB10).
- Table-driven across at least three deposit sizes (T4).

**First failing test.** A three-year table asserting the vested series is
`[100, 100, 100]`; a compounding implementation returns `[100, 110, 121]`.

---

## LB-3 — A partially withdrawn lot vests on its surviving portion

**Outcome.** Anke's €1,200 deposit with €1,100 still outstanding pays **110** on its
anniversary — not 120, and not 0.

**Why here.** This is the ambiguity the spec work resolved (LB5, D25): the withdrawn
part forfeits the year, the surviving part keeps its original anniversary. The
deposit ledger already splits lots this way, so this ticket is mostly about reading
outstanding amounts rather than deposited amounts.

**Acceptance.**
- €1,200 deposited, €100 withdrawn, anniversary → 110 (example C).
- The anniversary date is unchanged by the split (LB5).
- FIFO across lots: €500 on 1 Jan and €500 on 1 Jun, €600 withdrawn 1 Sep → the
  January lot vests nothing, the June lot vests 40 on its surviving €400 (example E).
- Double flooring: a surviving €30.50 vests 3 (LB4).

**First failing test.** Deposit €1,200, withdraw €100, advance twelve months, sweep,
assert 110. An implementation reading the deposited amount returns 120.

---

## LB-4 — Forfeiture is recorded as an explicit fact

**Outcome.** When a withdrawal gives up a bonus in progress, the customer's history
says so, naming the points forfeited.

**Design decision this ticket must make.** D28 says forfeiture is recorded "at
withdrawal time", but not on which ledger. The withdrawn portion never mints points,
so there is nothing to subtract from the points ledger — this is most likely a
deposit-side fact with a new `DepositEntryReason`, surfaced through the seam. Settle
it in the ticket and record the reasoning.

**Acceptance.**
- Withdrawing €100 of a €1,200 lot four months before its anniversary records a
  forfeiture of 10 points for the year in progress (LB7).
- Nothing already vested is touched: €1,000 deposited 1 Feb 2026 vests 100 on
  1 Feb 2027; withdrawing the full €1,000 on 1 Mar 2027 leaves those 100 spendable
  (example D, LB6).
- The fact is readable at the seam, not by inspecting ledger rows (T1).

**First failing test.** Withdraw part of a lot, assert a forfeiture fact naming 10
points is returned by the seam's history read.

---

## LB-5 — Clamped anniversaries hold across years

**Outcome.** A 29 February deposit vests on 28 February in common years and returns
to 29 February in the next leap year.

**Expect little or no production code.** `anniversary_after()` already behaves
correctly — confirmed by running it against the real implementation: a 29 Feb 2028
deposit gives 28 Feb 2029, 28 Feb 2030, 28 Feb 2031, then 29 Feb 2032, because
`add_months` measures from the original date each time rather than from the clamped
one. This ticket is mostly about pinning that with a test so a later refactor cannot
quietly break it.

**Acceptance.** The four-year table of example F, and a clamped year never advancing
into March (LB11).

**First failing test.** If the behaviour is already correct, this ticket's test
passes on arrival — say so in the ticket rather than manufacturing a failure.

---

## LB-6 — A late sweep catches up without distorting dates

**Outcome.** If the sweep does not run for three nights, the bonuses still land on
the dates they were due.

**Acceptance.**
- Anniversary 15 Mar 2027 with the sweep missing 15–17 Mar: catching up processes
  the dates in order and dates the bonus 15 Mar, so its expiry runs from 15 Mar
  (example G, LB9, D20).
- Running any of those dates twice vests once and reports zeros (T6).
- The sweep never vests past `min(swept_at, now)` — a lot due tonight at 03:00 is
  not vested by a sweep run at 02:00.

**First failing test.** Skip three business dates, catch up, assert the bonus lot's
expiry is twelve months after the *anniversary*, not after the catch-up date.

---

## LB-7 — The customer can see the bonus

**Outcome.** The dashboard shows what each deposit will pay and when, and vested
bonuses are distinguishable from base points in history.

**Changes.** The savings-activity table already shows `NEXT ANNIVERSARY` per lot;
add the projected bonus for the surviving portion. History labels vested entries.
All values read straight from the seam — the UI holds no rule of its own (D45).

**Acceptance.** Verified by driving the running app end to end, not by asserting on
components or UI state (T9), using the existing browser smoke harness in
`project_starter/readme.md`. Anke's dashboard shows 110 against her 20 Jun lot.

---

## Blocked — LB-8 Reversal of a deposit whose bonus already vested

Open question 1 in the spec. D11 claws back "exactly the points it credited" on a
reversal, and D26 protects vested bonuses from *withdrawal* — but a reversal is a
different event, and the spec does not say whether a vested bonus survives a deposit
that never really happened. Not startable until that is decided.

## Order and dependencies

LB-1 → LB-2 → LB-3 → LB-4 run in sequence; each builds on the one before.
LB-5 and LB-6 can be taken any time after LB-1. LB-7 should follow LB-3, so the
number on screen is the surviving-portion number rather than one that changes later.
LB-8 stays blocked.
