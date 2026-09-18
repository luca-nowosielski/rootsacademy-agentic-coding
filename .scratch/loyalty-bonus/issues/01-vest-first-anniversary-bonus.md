# 01: An untouched deposit vests its first anniversary bonus

**What to build:** A customer who deposited €1,000 a year ago and has never withdrawn
sees 100 points appear on the anniversary. This is the single crossing between the two
ledgers (spec D6) — a vesting deposit lot minting a points lot — and it cannot dodge the
expiry interaction: base points earned on day zero expire at exactly twelve months, which
*is* the first anniversary. So the vest-before-expire ordering (LB10, D21) has to be right
here, not in a later ticket.

The insertion point is already marked in the code: inside `run_daily_sweep`, within the
existing `atomically()` block and before the expiry loop, a comment names this ticket.
Reuse rather than rebuild: `DepositLedger.position()` already returns each lot's
outstanding amount and its `next_anniversary`, correctly split on partial withdrawal
(D25); `anniversary_after()` and `add_months()` already clamp month-ends (D27); the points
ledger already mints lots with their own expiry clocks. What does not exist yet: a
`MovementReason` for vesting, vesting counters on `SweepResult`, a query for lots due on or
before the sweep horizon, and the idempotency record that stops a vest being written twice
— the expiry path's `materialised_lot_ids` is the pattern to follow, and any new DDL is
owned by its storage module's `ensure_schema()` and registered in `migrations.py`.

**Decision this ticket carries (spec open question 2, now settled):** an anniversary is a
**date**, not an instant. A deposit made at 03:30 on 15 Mar 2026 vests in the 03:00 sweep
on 15 Mar 2027. This is consistent with LB11's clamping, which is calendar-based. The
vesting pass is still bounded by the same `horizon = min(swept_at, clock.now())` the expiry
pass uses; that bound decides which *business dates* are in scope, not which instant within
the anniversary date has passed.

**Blocked by:** None (can start immediately).

**Status:** ready-for-agent

- [ ] €1,000 deposited 15 Mar 2026; sweep run for 15 Mar 2027 leaves a balance of 100 —
      the 1,000 base points expired that same night and the vest still happened (example A,
      LB10, D21).
- [ ] The bonus lot is dated the anniversary and expires 15 Mar 2028 (LB8, LB9).
- [ ] Flooring: €5 → 0, €9.99 → 0, €10 → 1, €1,205 → 120 (example B, LB4).
- [ ] Re-running the sweep for 15 Mar 2027 vests nothing more and the second `SweepResult`
      reports zeros (T6).
- [ ] First failing test: deposit €1,000, advance the injected clock twelve months, run the
      sweep for that date, assert the balance **at the seam** is 100. Before the change it
      is 0 — the base expired and nothing vested.
- [ ] Assertions sit at the D42 seam only: no test asserts on ledger rows, lot identifiers
      or the order of internal drains (T1).
- [ ] The clock is injected — no `sleep`, no wall-clock reads in domain code (T2). Time
      travel is advancing the clock and calling `run_daily_sweep` per elapsed business date,
      never a test-only shortcut (T3).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
