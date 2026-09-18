"""The points ledger: append-only, and the only source of a balance.

Spec D6/D7. A positive entry is a **points lot** — points earned at one moment
from one source, carrying the earn date that drives its expiry. A negative
entry is a consumption of points (claimed, expired or clawed back today;
gifted in a later ticket). There is no mutable balance field anywhere.

The balance is not `SUM(points)`. It is derived by replaying the entries in the
order they happened, draining lots **oldest first** (spec D8) and dropping any
lot that has reached its twelfth month (spec D17). Expiry takes effect the
moment the clock passes it, not when the nightly sweep runs (spec D18), so the
replay decides what has expired and the sweep only writes the fact down. That
is why the replay ignores the expiry entries it finds: they are a
materialisation of what it just computed, and reading them as ordinary
consumption would subtract the same points twice.

This module is an injected collaborator behind the seam of spec D42. Callers
outside the application service have no business reading it, and tests must not
assert on its internals — lots, lot identifiers, or the order of drains (T1).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime

from .clock import add_months, in_brussels
from .db import atomically
from .events import MovementReason

SCHEMA = """
CREATE TABLE IF NOT EXISTS points_ledger (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    customer_id  TEXT    NOT NULL,
    occurred_at  TEXT    NOT NULL,   -- ISO 8601, Europe/Brussels (spec D5)
    points       INTEGER NOT NULL,   -- > 0: a points lot earned; < 0: points consumed
    reason       TEXT    NOT NULL,
    description  TEXT    NOT NULL,
    deposit_id   TEXT,               -- the deposit this entry earned from or reverses
    expires_at   TEXT,               -- lots only: when these points stop being spendable
    lot_id       INTEGER             -- expiry entries only: the lot they materialise
);
CREATE INDEX IF NOT EXISTS ix_points_ledger_customer
    ON points_ledger (customer_id, id);
CREATE INDEX IF NOT EXISTS ix_points_ledger_deposit
    ON points_ledger (customer_id, deposit_id);
"""

#: Indexes over columns this ticket added, so they can be created after the
#: columns exist on a database that predates them.
EXPIRY_INDEXES = """
CREATE INDEX IF NOT EXISTS ix_points_ledger_lot
    ON points_ledger (lot_id);
"""

#: Columns added after the table was first shipped, and their types.
ADDED_COLUMNS = (("expires_at", "TEXT"), ("lot_id", "INTEGER"))

#: A points lot expires this many months after it was earned (spec D17).
EXPIRY_MONTHS = 12


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create the ledger tables if they are not there yet."""
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.executescript(EXPIRY_INDEXES)


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    """Bring a table created before expiry existed up to the current shape.

    `CREATE TABLE IF NOT EXISTS` does nothing at all to a table that is already
    there, so a database file from an earlier ticket would keep the old columns
    and the first read of `expires_at` would fail.
    """
    present = {row["name"] for row in conn.execute("PRAGMA table_info(points_ledger)")}
    for column, column_type in ADDED_COLUMNS:
        if column not in present:
            conn.execute(f"ALTER TABLE points_ledger ADD COLUMN {column} {column_type}")


def expiry_of(earned_at: datetime) -> datetime:
    """The instant a lot earned at `earned_at` stops being spendable (D17).

    Twelve calendar months, not 365 days, and a day that does not exist in the
    target month clamps to the last one that does, the way an anniversary does
    (spec D27): points earned on 29 February expire on 28 February in a common
    year. That calendar arithmetic is `clock.add_months`, which the deposit
    lot's anniversary clock uses too — one rule for both twelve-month clocks.
    """
    return add_months(earned_at, EXPIRY_MONTHS)


@dataclass(frozen=True)
class DepositCredit:
    """What one deposit credited: the points, and how it described itself."""

    points: int
    #: `None` when the deposit credited nothing, so nothing was ever written.
    description: str | None


@dataclass(frozen=True)
class PointsMovement:
    """One movement of points, as the customer sees it in their history."""

    occurred_at: datetime
    points: int
    reason: MovementReason
    description: str


@dataclass(frozen=True)
class PointsLot:
    """A points lot as the replay left it: what it was, what is left of it."""

    #: The identifier of the entry that earned it. Internal to the ledger and
    #: the sweep that materialises its expiry; no test asserts on it (spec T1).
    lot_id: int
    earned_at: datetime
    expires_at: datetime
    points: int
    remaining: int


@dataclass(frozen=True)
class Position:
    """Where a customer stands at one instant.

    `expired_lots` are the lots that reached their twelfth month still holding
    points. They are already out of the balance whether or not the sweep has
    run; the sweep turns each of them into the ledger entry that says so.
    """

    balance: int
    open_lots: tuple[PointsLot, ...]
    expired_lots: tuple[PointsLot, ...]


@dataclass
class _Draining:
    """A lot mid-replay, with points still coming off it."""

    lot_id: int
    earned_at: datetime
    expires_at: datetime
    points: int
    remaining: int = field(default=0)

    def settled(self) -> PointsLot:
        return PointsLot(
            lot_id=self.lot_id,
            earned_at=self.earned_at,
            expires_at=self.expires_at,
            points=self.points,
            remaining=self.remaining,
        )


class PointsLedger:
    """Append-only points ledger over SQLite."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    @contextmanager
    def atomically(self) -> Iterator[None]:
        """Run reads and appends as one unit, with no writer in between.

        Part of the ledger's contract, not an SQLite detail: an append-only
        ledger is read before it is appended to ("has this deposit already
        been reversed?"), and core-banking feeds are at-least-once, so the
        same event can arrive on two workers at the same moment. Without this,
        both read the same ledger and both append.

        Nesting is allowed and means one transaction, not two: a command that
        already holds the ledger can call another that also wants it, and the
        outermost block is the one that commits or rolls back. The nesting is
        tracked on the connection (`db.atomically`) rather than here, so a
        deposit — which writes a points lot to this ledger and a deposit lot
        to the other one of spec D6 — commits both or neither when the two
        share a connection.
        """
        with atomically(self._conn):
            yield

    def append(
        self,
        *,
        customer_id: str,
        occurred_at: datetime,
        points: int,
        reason: MovementReason,
        description: str,
        deposit_id: str | None = None,
        expires_at: datetime | None = None,
        lot_id: int | None = None,
    ) -> PointsMovement:
        """Append one entry. Points are integers everywhere (spec D4).

        A positive entry is a points lot, and it carries its own `expires_at`
        rather than having one computed on read: a gifted lot keeps the earn
        date it was minted with (spec D30), so the expiry cannot be derived
        from the moment the entry was written.
        """
        if not isinstance(points, int) or isinstance(points, bool):
            raise TypeError(f"points must be an int, got {points!r}")
        moment = in_brussels(occurred_at)
        expiry = expires_at
        if expiry is None and points > 0:
            expiry = expiry_of(moment)
        self._conn.execute(
            "INSERT INTO points_ledger"
            " (customer_id, occurred_at, points, reason, description, deposit_id,"
            "  expires_at, lot_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                customer_id,
                moment.isoformat(),
                points,
                reason.value,
                description,
                deposit_id,
                in_brussels(expiry).isoformat() if expiry is not None else None,
                lot_id,
            ),
        )
        return PointsMovement(
            occurred_at=moment, points=points, reason=reason, description=description
        )

    def position(self, customer_id: str, as_of: datetime) -> Position:
        """Replay the customer's entries and say where they stand at `as_of`.

        One pass, in the order things happened:

        - a lot arrives, and first pays off anything the customer owes from a
          consumption that was larger than the points it could reach;
        - a consumption drains the open lots **oldest first** (spec D8), and
          whatever it cannot reach becomes that debt;
        - a lot that has reached its twelfth month is retired before anything
          else happens at that moment, so nothing can be paid for with points
          that had already expired when it happened (spec D18).

        `as_of` is how far forward expiry is judged, and nothing else: every
        entry on record is replayed, exactly as the balance of ticket 01
        summed every entry on record. Asking for a later instant asks what the
        customer will still hold then, which is what the demo and the sweep
        both want to know.

        It is a forward question, not a rewind. Lots are retired as the replay
        passes each entry as well as at `as_of`, so a `as_of` *earlier* than
        the last entry on record answers "what is left of the lots that were
        alive when the last thing happened", not "what the customer held that
        day". Under the injected clock (spec D5) nothing is ever stamped in the
        future, so the two only differ when a caller deliberately reads back
        past twelve months of their own history. What stays true either way is
        that the balance is the sum of the history read at the same instant:
        the entries excluded from one are excluded from the other.

        Expiry entries are skipped: the sweep writes them to record what this
        replay derives, and counting them as consumption too would subtract the
        same points twice. That is also what makes the sweep re-runnable — the
        balance is the same before it, after it, and after it runs again.
        """
        moment = in_brussels(as_of)
        rows = self._conn.execute(
            "SELECT id, occurred_at, expires_at, points, reason FROM points_ledger"
            " WHERE customer_id = ?",
            (customer_id,),
        ).fetchall()
        entries = sorted(
            ((datetime.fromisoformat(row["occurred_at"]), row) for row in rows),
            key=lambda entry: (entry[0], entry[1]["id"]),
        )

        open_lots: list[_Draining] = []
        expired: list[_Draining] = []
        owed = 0

        def retire(at: datetime) -> None:
            kept: list[_Draining] = []
            for lot in open_lots:
                if lot.expires_at <= at:
                    if lot.remaining > 0:
                        expired.append(lot)
                else:
                    kept.append(lot)
            open_lots[:] = kept

        for occurred_at, row in entries:
            if row["reason"] == MovementReason.EXPIRY.value:
                continue
            retire(occurred_at)
            points = int(row["points"])
            if points > 0:
                lot = _Draining(
                    lot_id=int(row["id"]),
                    earned_at=occurred_at,
                    expires_at=_expiry_in(row, occurred_at),
                    points=points,
                    remaining=points,
                )
                repaid = min(owed, lot.remaining)
                lot.remaining -= repaid
                owed -= repaid
                open_lots.append(lot)
            elif points < 0:
                owed += _drain(open_lots, -points)
        retire(moment)

        return Position(
            balance=sum(lot.remaining for lot in open_lots) - owed,
            open_lots=tuple(lot.settled() for lot in open_lots if lot.remaining > 0),
            expired_lots=tuple(
                lot.settled() for lot in sorted(expired, key=lambda lot: lot.expires_at)
            ),
        )

    def balance(self, customer_id: str, as_of: datetime) -> int:
        """Derived, never stored (spec D7), and never counting expired points.

        May be negative after a clawback: a deposit that bounced takes back
        exactly what it gave, and nothing floors the result at zero (D11).
        """
        return self.position(customer_id, as_of).balance

    def movements(self, customer_id: str, as_of: datetime) -> list[PointsMovement]:
        """Every movement written for the customer by `as_of`, newest first.

        Newest by *when it happened*, not by the order the rows were written.
        Under the real clock the two agree; under a clock a test or the sweep
        has moved they do not — an expiry entry is stamped with the instant the
        lot died, which is before the sweep that wrote it — and the customer
        reads this list to check the number is right (spec user story 5).

        The sort is on the parsed instant rather than on the stored string:
        two stamps an hour apart across the Brussels autumn fold share a
        wall-clock reading and differ only in their offset, which sorts the
        wrong way as text. `id` breaks a genuine tie in insertion order, so a
        claim recorded at the same instant as the deposit that funded it still
        reads above it.

        Expiries the sweep has not materialised yet are not here: the seam adds
        them, because a customer whose points have expired must see why the
        balance moved whether or not the batch job has run (spec D18).

        An expiry shows up exactly when the lot it belongs to has died by
        `as_of`, whether the sweep has written it down or not: one rule for the
        materialised row and the derived one, so the balance and the history
        never disagree about whether points are gone. Under the real clock the
        sweep only ever runs for tonight and every expiry it writes is already
        in the past; a demo asking it for a business date that has not arrived
        yet writes one dated the day the lot will die, and it reads as history
        on that day rather than today.
        """
        moment = in_brussels(as_of)
        rows = self._conn.execute(
            "SELECT id, occurred_at, points, reason, description FROM points_ledger"
            " WHERE customer_id = ?",
            (customer_id,),
        ).fetchall()
        movements = [
            (occurred_at, row)
            for occurred_at, row in ((datetime.fromisoformat(r["occurred_at"]), r) for r in rows)
            if occurred_at <= moment or row["reason"] != MovementReason.EXPIRY.value
        ]
        movements.sort(key=lambda entry: (entry[0], entry[1]["id"]), reverse=True)
        return [
            PointsMovement(
                occurred_at=occurred_at,
                points=int(row["points"]),
                reason=MovementReason(row["reason"]),
                description=row["description"],
            )
            for occurred_at, row in movements
        ]

    def materialised_lot_ids(self, customer_id: str) -> set[int]:
        """The lots whose expiry the sweep has already written down.

        The sweep's idempotency key, per business date and across them: a lot
        is expired once however many times the sweep runs (spec D20, user
        story 38).
        """
        rows = self._conn.execute(
            "SELECT lot_id FROM points_ledger"
            " WHERE customer_id = ? AND reason = ? AND lot_id IS NOT NULL",
            (customer_id, MovementReason.EXPIRY.value),
        ).fetchall()
        return {int(row["lot_id"]) for row in rows}

    def vested_anniversaries(self, customer_id: str) -> set[tuple[str, date]]:
        """The deposit-lot anniversaries the sweep has already paid a bonus for.

        The vesting sweep's idempotency key, per business date and across them
        (spec D20): a lot vests once per anniversary however many times the
        sweep runs, and a night that failed is replayed without paying twice.

        The vesting entry *is* the record, the way an expiry entry is the
        record of a lot written off (`materialised_lot_ids`): it names the
        deposit whose lot earned it and is stamped with the anniversary it
        paid for, so there is nothing to keep in a second table that could
        disagree with the ledger. A bonus that floored to nothing (spec D24)
        writes no entry and appears in no key — which needs no special case,
        because re-running pays it nothing a second time too.

        The pair is matched on the anniversary **date**, which is what an
        anniversary is — spec D27 clamps calendar days, not instants. Matching
        the stored stamp as text would be the same key today, because the entry
        is written at the start of that day, and would quietly stop being the
        same key the moment anything moved that stamp.
        """
        rows = self._conn.execute(
            "SELECT deposit_id, occurred_at FROM points_ledger"
            " WHERE customer_id = ? AND reason = ? AND deposit_id IS NOT NULL",
            (customer_id, MovementReason.VESTING.value),
        ).fetchall()
        return {
            (row["deposit_id"], datetime.fromisoformat(row["occurred_at"]).date())
            for row in rows
        }

    def customers_with_unmaterialised_lots(self) -> list[str]:
        """Every customer holding a lot whose expiry has not been written yet.

        The sweep's working set. It shrinks as lots are materialised, and a lot
        that never expires (because it was spent first) stays in it — cheap at
        demo scale, and the shape a production sweep would replace with a range
        scan over a UTC-normalised expiry column. It cannot be a range scan
        here: `expires_at` is stored as a Brussels reading, and two readings an
        hour apart across the autumn fold sort the wrong way as text.
        """
        rows = self._conn.execute(
            "SELECT DISTINCT lot.customer_id AS customer_id FROM points_ledger AS lot"
            " WHERE lot.points > 0 AND NOT EXISTS ("
            "   SELECT 1 FROM points_ledger AS materialised"
            "   WHERE materialised.lot_id = lot.id AND materialised.reason = ?)",
            (MovementReason.EXPIRY.value,),
        ).fetchall()
        return [row["customer_id"] for row in rows]

    def has_movement_for_deposit(
        self, customer_id: str, deposit_id: str, reason: MovementReason
    ) -> bool:
        """Is this deposit already recorded in the ledger for this reason?

        The guard behind exactly-once handling: a deposit earns its points
        once however many times core banking delivers the event, and a
        reversal claws them back once.
        """
        row = self._conn.execute(
            "SELECT 1 FROM points_ledger"
            " WHERE customer_id = ? AND deposit_id = ? AND reason = ? LIMIT 1",
            (customer_id, deposit_id, reason.value),
        ).fetchone()
        return row is not None

    def credit_for_deposit(self, customer_id: str, deposit_id: str) -> DepositCredit:
        """What this deposit put into this customer's balance, and for what.

        A reversal claws back exactly these points and no more (spec D11), and
        describes itself with what the deposit said, so the customer reading
        the clawback back sees the money it undoes rather than an identifier
        only the feed cares about. A deposit that earned nothing — interest,
        an internal transfer, a sum under one euro, or one that has not
        arrived yet — credited nothing, so its reversal takes nothing.
        """
        row = self._conn.execute(
            "SELECT COALESCE(SUM(points), 0) AS credited,"
            " MAX(description) AS description FROM points_ledger"
            " WHERE customer_id = ? AND deposit_id = ? AND reason = ?",
            (customer_id, deposit_id, MovementReason.DEPOSIT.value),
        ).fetchone()
        return DepositCredit(points=int(row["credited"]), description=row["description"])


def _drain(open_lots: list[_Draining], wanted: int) -> int:
    """Take `wanted` points off the open lots, oldest first (spec D8).

    Returns whatever could not be taken. That shortfall is a debt against the
    customer, not a shortfall the ledger hides: it keeps the balance negative
    until points arrive to settle it (spec D11).
    """
    for lot in open_lots:
        if wanted <= 0:
            break
        taken = min(lot.remaining, wanted)
        lot.remaining -= taken
        wanted -= taken
    return max(wanted, 0)


def _expiry_in(row: sqlite3.Row, occurred_at: datetime) -> datetime:
    """The lot's stored expiry, or the one it would have been written with.

    The fallback is for rows written before lots had an expiry column at all.
    """
    stored = row["expires_at"]
    return datetime.fromisoformat(stored) if stored else expiry_of(occurred_at)
