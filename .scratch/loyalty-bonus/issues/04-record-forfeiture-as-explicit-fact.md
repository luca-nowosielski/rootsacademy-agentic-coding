# 04: Forfeiture is recorded as an explicit fact

**What to build:** When a withdrawal gives up a bonus in progress, the customer's history
says so, naming the points forfeited. Nothing is silently dropped (LB7, D28).

**Design decision this ticket must make and record.** D28 says forfeiture is recorded "at
withdrawal time" but not on which ledger. The withdrawn portion never minted points, so
there is nothing to subtract from the points ledger — this is most likely a deposit-side
fact with a new `DepositEntryReason`, surfaced through the seam. `DepositEntryReason`'s
docstring is explicit that the two ledgers deliberately do not share a vocabulary of
reasons, which is an argument for the deposit side. Settle it in this ticket and write down
the reasoning where the next reader will find it.

**Blocked by:** 03 (A partially withdrawn lot vests on its surviving portion) — the
surviving/withdrawn split is what there is to forfeit.

**Status:** ready-for-agent

- [ ] Withdrawing €100 of a €1,200 lot four months before its anniversary records a
      forfeiture of 10 points for the year in progress (LB7). Asserting that fact is
      returned by the seam's history read is the first failing test.
- [ ] Nothing already vested is touched: €1,000 deposited 1 Feb 2026 vests 100 on
      1 Feb 2027; withdrawing the full €1,000 on 1 Mar 2027 leaves those 100 spendable, and
      no further anniversaries vest because nothing of the lot survives (example D, LB6).
- [ ] The fact is readable at the seam, not by inspecting ledger rows (T1).
- [ ] The chosen ledger and the reasoning behind it are recorded alongside the code, citing
      the spec clause as the surrounding code does.
- [ ] Deposit and points work stay in one transaction where they touch — `atomically(conn)`,
      both commit or neither (D6).
- [ ] `pytest` and `ruff check .` pass in `app/backend`.
