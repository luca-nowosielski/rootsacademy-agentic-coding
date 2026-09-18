"""The deposit-lot ledger: the money side, append-only, and entirely its own.

Spec D6. There are **two** ledgers and they touch at exactly one point — a
vesting deposit lot minting a points lot on the other side (spec D22), which the
nightly sweep does at the seam by *reading* this ledger. Nothing crosses in the
other direction and nothing crosses into this one: no claim, no gift, no vesting
and no points movement of any kind reaches a deposit lot, and no withdrawal
reaches the points balance (spec D10). The two ledgers do not even share a
vocabulary of reasons; this one speaks `DepositEntryReason`.

A positive entry opens a **deposit lot** — money from one deposit, carrying its
own recurring twelve-month anniversary clock from the day it landed. A negative
entry consumes deposit lots, and like the points ledger it consumes them
**oldest first** (spec D8). One ordering rule, applied on both sides.

What is outstanding is not `SUM(amount)`, per lot or in total. It is derived by
replaying the entries in the order they happened and draining the open lots,
which is what makes the split of spec D25 fall out rather than being a special
case: a withdrawal that only partially covers a lot takes part of it and leaves
the rest standing, and the part left standing is still the *same lot*, with the
deposit date — and therefore the anniversary — it was opened with. Nothing is
rewritten, nothing is closed and reopened, and no lot is ever split into a row
of its own.

Every entry records **what core banking said happened**, never what the ledger
worked out at the time it was written: a withdrawal entry carries the amount
that was asked to leave, not the part of it that found a lot. Which lots a
withdrawal actually drained is a conclusion, and a conclusion has to be redrawn
every replay, because a later entry can change it — a deposit reversed after a
withdrawal has already crossed it never stood at all, and the money the
withdrawal took from it has to come off the lots behind it instead. Freezing
that conclusion into the row is how phantom money survives a reversal.

Money is held in whole cents. Euros are decimal and points are integers (spec
D4); storing cents keeps a float out of the ledger while still letting a
customer withdraw €30.50 of a €100 lot.

This module is an injected collaborator behind the seam of spec D42. Callers
outside the application service have no business reading it, and tests must not
assert on its internals — rows, entry identifiers, or the order of drains (T1).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

from .clock import add_months, in_brussels
from .db import atomically
from .events import DepositEntryReason

SCHEMA = """
CREATE TABLE IF NOT EXISTS deposit_ledger (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id   TEXT    NOT NULL,
    occurred_at   TEXT    NOT NULL,   -- ISO 8601, Europe/Brussels (spec D5)
    amount_cents  INTEGER NOT NULL,   -- > 0: a deposit lot opened; < 0: lots consumed
    reason        TEXT    NOT NULL,
    description   TEXT    NOT NULL,
    deposit_id    TEXT,               -- the deposit this lot came from, or that is reversed
    withdrawal_id TEXT                -- the withdrawal that consumed
);
CREATE INDEX IF NOT EXISTS ix_deposit_ledger_customer
    ON deposit_ledger (customer_id, id);
CREATE INDEX IF NOT EXISTS ix_deposit_ledger_deposit
    ON deposit_ledger (customer_id, deposit_id);
CREATE INDEX IF NOT EXISTS ix_deposit_ledger_withdrawal
    ON deposit_ledger (customer_id, withdrawal_id);
"""

#: A deposit lot's anniversary comes round every this many months (spec D22).
ANNIVERSARY_MONTHS = 12

#: Money is held in whole cents, so nothing in this ledger is ever a float.
_CENT = Decimal("0.01")


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the deposit-lot ledger's table if it is not there yet."""
    conn.executescript(SCHEMA)


def cents_landing(amount_eur: Decimal) -> int:
    """Money arriving, floored to the cent.

    Floored for the same reason a deposit's points are (spec D4): the system
    never credits the customer with more than really landed. A sub-cent
    deposit opens no lot at all, exactly as a sub-euro one earns no point.
    """
    return int((amount_eur / _CENT).to_integral_value(rounding=ROUND_FLOOR))


def cents_leaving(amount_eur: Decimal) -> int:
    """Money going out, rounded up to the cent.

    The other direction, and for the same reason: a fraction of a cent that
    has left the account must not be left standing as a deposit lot still
    earning a bonus. Rounding both sides the same way would leave phantom
    money outstanding; rounding each against the customer's advantage by a
    fraction of a cent keeps the ledger honest about what is really there.
    """
    return int((amount_eur / _CENT).to_integral_value(rounding=ROUND_CEILING))


def euros(cents: int) -> Decimal:
    """Whole cents as an exact euro amount. Never a float."""
    return (Decimal(cents) * _CENT).quantize(_CENT)


def anniversary_after(deposited_at: datetime, as_of: datetime) -> datetime:
    """The deposit lot's next anniversary strictly after `as_of` (spec D22).

    The clock recurs: every twelve months the lot is still there it comes
    round again, so this is the first anniversary that has not yet passed, not
    only the first one. It is always counted from the original deposit date
    (spec D27 clamping via `clock.add_months`), so a 29 February lot alternates
    between 28 and 29 February instead of drifting a day earlier every leap
    cycle.

    The walk lands at most one year past `as_of`, so it stays inside the
    calendar for every instant the seam will read at: `clock.readable_instant`
    refuses anything later, at the seam, before it reaches here. Without that
    guard this loop is where the calendar runs out — it would build year 10000
    and raise a `ValueError` that is not a `DomainError`, which is a 500.
    """
    deposited_at, as_of = in_brussels(deposited_at), in_brussels(as_of)
    # The calendar years between the two dates are the answer or one short of
    # it — never more — and never fewer than one, because the lot's own
    # deposit day is not an anniversary of itself. The loop settles the rest.
    years = max(as_of.year - deposited_at.year, 1)
    anniversary = add_months(deposited_at, ANNIVERSARY_MONTHS * years)
    while anniversary <= as_of:
        years += 1
        anniversary = add_months(deposited_at, ANNIVERSARY_MONTHS * years)
    return anniversary


def anniversaries_through(deposited_at: datetime, business_date: date) -> list[date]:
    """Every anniversary of this lot that has arrived by `business_date`, oldest first.

    The other end of `anniversary_after`: that one asks which anniversary is
    coming, this one asks which have been reached and are therefore owed a
    bonus (spec D22). The sweep needs the list rather than the next one,
    because a night that did not run leaves more than one due at once (spec
    D20) and each is paid separately, on its own date.

    **An anniversary is a date, not an instant.** A lot deposited at 03:30 is
    twelve months old at 03:30, half an hour after the 03:00 sweep, and the
    bonus is still paid that morning: the rule customers are told is "every
    year on the day you paid in", and the clamping of spec D27 is calendar
    arithmetic already. Comparing instants would make the hour a deposit
    happened to land at decide which night it vests on, and would hold a
    03:30 deposit's bonus back a full day every year. So this answers in
    dates, and the caller decides which instant on the day to stamp.

    Counted from the original deposit date every time, like `anniversary_after`
    and for the same reason: a 29 February lot alternates between 28 and 29
    February rather than drifting a day earlier every leap cycle (spec D27).
    """
    deposited_at = in_brussels(deposited_at)
    due: list[date] = []
    years = 1
    anniversary = add_months(deposited_at, ANNIVERSARY_MONTHS).date()
    while anniversary <= business_date:
        due.append(anniversary)
        years += 1
        anniversary = add_months(deposited_at, ANNIVERSARY_MONTHS * years).date()
    return due


@dataclass(frozen=True)
class DepositLot:
    """A deposit lot as the replay left it: what landed, what is still there.

    `outstanding_cents` is what survived every withdrawal that reached it. A
    lot a withdrawal only partially covered is this same lot with less on it
    (spec D25) — `deposited_at`, and therefore `next_anniversary`, are the
    ones it was opened with.
    """

    deposit_id: str
    deposited_at: datetime
    amount_cents: int
    outstanding_cents: int
    #: The next anniversary after the instant the position was read at.
    next_anniversary: datetime

    @property
    def amount_eur(self) -> Decimal:
        """What the deposit was, in euros."""
        return euros(self.amount_cents)

    @property
    def outstanding_eur(self) -> Decimal:
        """What is left of it, in euros."""
        return euros(self.outstanding_cents)


@dataclass(frozen=True)
class OpenedLot:
    """A deposit lot as its deposit opened it, before anything drained it."""

    deposited_at: datetime
    amount_cents: int

    @property
    def amount_eur(self) -> Decimal:
        """What the deposit was, in euros."""
        return euros(self.amount_cents)


@dataclass(frozen=True)
class DepositPosition:
    """What a customer still has standing on the money side at one instant.

    `lots` are the lots with money still on them, **oldest first** — the order
    the next withdrawal will consume them in (spec D8). A read meant for the
    customer re-orders them by anniversary at the seam; this is the ledger's
    own order, and it is the consumption one.
    """

    outstanding_cents: int
    lots: tuple[DepositLot, ...]


@dataclass
class _Draining:
    """A lot mid-replay, with money still coming off it."""

    deposit_id: str
    deposited_at: datetime
    amount_cents: int
    outstanding_cents: int

    def settled(self, as_of: datetime) -> DepositLot:
        return DepositLot(
            deposit_id=self.deposit_id,
            deposited_at=self.deposited_at,
            amount_cents=self.amount_cents,
            outstanding_cents=self.outstanding_cents,
            next_anniversary=anniversary_after(self.deposited_at, as_of),
        )


class DepositLedger:
    """Append-only deposit-lot ledger over SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def atomically(self) -> Iterator[None]:
        """Run reads and appends as one unit, with no writer in between.

        The same contract the points ledger has, for the same reason: this
        ledger is read before it is appended to ("how much of this deposit is
        still standing?"), and core banking's feed is at-least-once.

        Nesting is tracked on the connection (`db.atomically`), so a deposit
        writing a points lot to one ledger and a deposit lot to this one
        commits both or neither when the two share a connection — without
        either ledger knowing the other exists.
        """
        with atomically(self._conn):
            yield

    def append(
        self,
        *,
        customer_id: str,
        occurred_at: datetime,
        amount_cents: int,
        reason: DepositEntryReason,
        description: str,
        deposit_id: str | None = None,
        withdrawal_id: str | None = None,
    ) -> None:
        """Append one entry. Money is whole cents, never a float (spec D4)."""
        if not isinstance(amount_cents, int) or isinstance(amount_cents, bool):
            raise TypeError(f"amount_cents must be an int, got {amount_cents!r}")
        self._conn.execute(
            "INSERT INTO deposit_ledger"
            " (customer_id, occurred_at, amount_cents, reason, description,"
            "  deposit_id, withdrawal_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                in_brussels(occurred_at).isoformat(),
                amount_cents,
                reason.value,
                description,
                deposit_id,
                withdrawal_id,
            ),
        )

    def position(self, customer_id: str, as_of: datetime) -> DepositPosition:
        """Replay the customer's entries and say what is still standing.

        A **reversal is settled before the replay starts**, not during it: a
        deposit that never really happened never stood, so its lot is not
        opened at all rather than opened and taken back (spec D11). That is
        what keeps the queue honest when a withdrawal has already crossed the
        reversed lot. Under the older reading — open the lot, drain it, then
        take back whatever is left of it — the money that withdrawal took out
        of a deposit that never arrived was never given back to the lots behind
        it, and the customer was left holding money nobody ever paid in, on a
        live anniversary clock. Here the withdrawal simply never meets that
        lot, so it reaches the next one and the total settles where it should.
        A reversal arriving before its own deposit is the same rule read in the
        other direction, and needs no second case.

        Then one pass, in the order things happened:

        - a deposit opens a lot, outstanding in full;
        - a withdrawal consumes the lots open **at that moment**, oldest first
          (spec D8), taking part of the lot it only partially covers and
          leaving the rest standing under its original deposit date (spec D25).

        Whatever a withdrawal cannot reach is simply not consumed, and is not
        carried forward either: money can leave an account Saving Streak never
        saw it arrive in (spec D1), and that shortfall is neither a negative
        lot nor a debt against the next deposit. A later deposit is money that
        really did arrive, and it stands in full with its own clock.

        `as_of` says when to read, and on this side nothing expires: every
        entry on record is replayed, exactly as on the points side, and the
        instant is used only to work out which anniversary each surviving lot
        is heading for next.
        """
        moment = in_brussels(as_of)
        rows = self._conn.execute(
            "SELECT id, occurred_at, amount_cents, reason, deposit_id FROM deposit_ledger"
            " WHERE customer_id = ?",
            (customer_id,),
        ).fetchall()
        entries = sorted(
            ((datetime.fromisoformat(row["occurred_at"]), row) for row in rows),
            key=lambda entry: (entry[0], entry[1]["id"]),
        )
        reversed_deposits = {
            row["deposit_id"]
            for _, row in entries
            if row["reason"] == DepositEntryReason.DEPOSIT_REVERSAL.value
        }

        open_lots: list[_Draining] = []
        for occurred_at, row in entries:
            reason, amount = row["reason"], int(row["amount_cents"])
            if reason == DepositEntryReason.DEPOSIT.value:
                if row["deposit_id"] in reversed_deposits:
                    continue
                open_lots.append(
                    _Draining(
                        deposit_id=row["deposit_id"],
                        deposited_at=occurred_at,
                        amount_cents=amount,
                        outstanding_cents=amount,
                    )
                )
            elif reason == DepositEntryReason.WITHDRAWAL.value:
                _consume(open_lots, -amount)

        standing = [lot for lot in open_lots if lot.outstanding_cents > 0]
        return DepositPosition(
            outstanding_cents=sum(lot.outstanding_cents for lot in standing),
            lots=tuple(lot.settled(moment) for lot in standing),
        )

    def lot_opened_by(self, customer_id: str, deposit_id: str) -> OpenedLot | None:
        """The lot this deposit opened, as it was opened, or `None`.

        What the deposit *said*, read straight off its own entry rather than
        out of a replay — a reversal undoes the whole deposit, not the part of
        it a withdrawal had not reached yet, so what is left of the lot today
        is not the question. `None` when the deposit opened no lot at all: it
        was under a cent, it earned nothing (spec D3), or it has not arrived
        yet, because a reversal can land before the deposit it cancels.
        """
        row = self._conn.execute(
            "SELECT occurred_at, amount_cents FROM deposit_ledger"
            " WHERE customer_id = ? AND deposit_id = ? AND reason = ?"
            " ORDER BY id LIMIT 1",
            (customer_id, deposit_id, DepositEntryReason.DEPOSIT.value),
        ).fetchone()
        if row is None:
            return None
        return OpenedLot(
            deposited_at=datetime.fromisoformat(row["occurred_at"]),
            amount_cents=int(row["amount_cents"]),
        )

    def customers_with_lots(self) -> list[str]:
        """Every customer this ledger has ever opened a deposit lot for.

        The vesting sweep's working set, and deliberately a wide one: whether a
        lot still has money on it, and whether its anniversary has come round,
        are both conclusions of the replay (`position`), not facts a row holds.
        A customer who withdrew everything is still in here and vests nothing —
        cheap at demo scale, and the shape a production sweep would replace
        with an index over a due date it maintained as lots drained.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT customer_id FROM deposit_ledger WHERE reason = ?",
            (DepositEntryReason.DEPOSIT.value,),
        ).fetchall()
        return [row["customer_id"] for row in rows]

    def has_entry_for(
        self,
        customer_id: str,
        reason: DepositEntryReason,
        *,
        deposit_id: str | None = None,
        withdrawal_id: str | None = None,
    ) -> bool:
        """Is this deposit or withdrawal already recorded, for this reason?

        The money side's own guard against an at-least-once feed, and
        deliberately its own: it asks this ledger, never the points one, so a
        deposit too small to earn a point — which leaves no points movement
        behind at all — still opens exactly one deposit lot however many times
        core banking delivers it.
        """
        if withdrawal_id is not None:
            row = self._conn.execute(
                "SELECT 1 FROM deposit_ledger"
                " WHERE customer_id = ? AND withdrawal_id = ? AND reason = ? LIMIT 1",
                (customer_id, withdrawal_id, reason.value),
            ).fetchone()
        else:
            row = self._conn.execute(
                "SELECT 1 FROM deposit_ledger"
                " WHERE customer_id = ? AND deposit_id = ? AND reason = ? LIMIT 1",
                (customer_id, deposit_id, reason.value),
            ).fetchone()
        return row is not None


def _consume(open_lots: list[_Draining], wanted: int) -> int:
    """Take `wanted` cents off the open lots, oldest first (spec D8).

    Returns what was actually taken, which is less than `wanted` when the
    withdrawal is larger than everything outstanding. The shortfall is not a
    debt and not a negative lot: unlike points, the money side never owes
    anything, because Saving Streak does not own the money (spec D1) and an
    account can hold euros no deposit lot here ever saw. It is dropped here
    rather than carried to the next lot the replay opens, because that lot is
    money that really did arrive, and a deposit standing in full is exactly
    what the customer is owed a bonus clock on.
    """
    taken_total = 0
    for lot in open_lots:
        if wanted <= 0:
            break
        taken = min(lot.outstanding_cents, wanted)
        lot.outstanding_cents -= taken
        wanted -= taken
        taken_total += taken
    return taken_total
