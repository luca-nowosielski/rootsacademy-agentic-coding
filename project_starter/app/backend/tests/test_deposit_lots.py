"""Deposit lots, and withdrawals that consume them oldest first (spec D6/D8/D25).

The money side of spec D6. A deposit opens a deposit lot carrying its own
recurring twelve-month anniversary clock; a withdrawal consumes lots oldest
first and splits the one it only partially covers. Nothing pays out yet — the
loyalty-rate bonus is a later ticket — so what is asserted here is what the
customer can see: how much of each deposit is still standing, and when each one
next comes round.

Everything goes through the seam of spec D42: events in, deposit lots and
balances out. Nothing asserts on the deposit ledger's rows, on an entry
identifier, or on the order in which the service calls anything (spec T1).

Time travel is the injected clock, never a sleep and never a wall-clock read
(spec T2/T3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from conftest import PINNED_NOW, FakeVoucherIssuer
from saving_streak.claims import ClaimRecords
from saving_streak.clock import LAST_READABLE_INSTANT, FixedClock, in_brussels
from saving_streak.db import connect
from saving_streak.deposits import DepositLedger
from saving_streak.events import (
    ClaimReward,
    DepositReversed,
    DepositSource,
    DomainError,
    MoneyDeposited,
    MoneyWithdrawn,
)
from saving_streak.ledger import PointsLedger
from saving_streak.migrations import ensure_schema
from saving_streak.service import SavingStreakService

CUSTOMER = "cust-alice"
OTHER_CUSTOMER = "cust-bob"

CINEMA = "cinema-ticket"  # 100 points
CHARITY = "charity-donation"  # 10 points

#: A lot opened at `PINNED_NOW` comes round on this day, and every year after.
FIRST_ANNIVERSARY = in_brussels(datetime(2027, 3, 14, 10, 30))


def deposit(
    service,
    *,
    euros: str,
    deposit_id: str = "dep-1",
    customer_id: str = CUSTOMER,
    account_id: str = "acc-1",
    source: DepositSource = DepositSource.EXTERNAL,
):
    return service.handle(
        MoneyDeposited(
            customer_id=customer_id,
            account_id=account_id,
            deposit_id=deposit_id,
            amount_eur=Decimal(euros),
            source=source,
        )
    )


def withdraw(
    service,
    *,
    euros: str,
    withdrawal_id: str = "wd-1",
    customer_id: str = CUSTOMER,
    account_id: str = "acc-1",
):
    return service.handle(
        MoneyWithdrawn(
            customer_id=customer_id,
            account_id=account_id,
            withdrawal_id=withdrawal_id,
            amount_eur=Decimal(euros),
        )
    )


def reverse(service, *, deposit_id: str, customer_id: str = CUSTOMER):
    return service.handle(DepositReversed(customer_id=customer_id, deposit_id=deposit_id))


def standing(service, customer_id: str = CUSTOMER, at=None):
    """What the customer sees of their deposit lots: how much, and when next.

    Closest anniversary first, which is the order the seam hands them back in.
    """
    return [
        (lot.outstanding_eur, lot.next_anniversary.date().isoformat())
        for lot in service.deposit_lots(customer_id, at)
    ]


def points_page(service, customer_id: str = CUSTOMER):
    """Everything the points side can show, as plain values two runs compare."""
    return (
        service.balance(customer_id),
        [(m.occurred_at, m.points, m.reason, m.description) for m in service.history(customer_id)],
    )


@pytest.fixture
def worlds(tmp_path):
    """Independent copies of the whole system on their own storage.

    The only place in this file that reaches behind the seam, and it reaches
    there to *build* the system, never to assert on it (spec T1).
    """
    connections = []

    def make(name: str, at=PINNED_NOW) -> tuple[SavingStreakService, FixedClock]:
        conn = connect(tmp_path / f"{name}.db")
        ensure_schema(conn)
        connections.append(conn)
        clock = FixedClock(at)
        return (
            SavingStreakService(
                ledger=PointsLedger(conn),
                clock=clock,
                claims=ClaimRecords(conn),
                voucher_issuer=FakeVoucherIssuer(),
                deposit_ledger=DepositLedger(conn),
            ),
            clock,
        )

    yield make
    for conn in connections:
        conn.close()


# ------------------------------------------------------ a deposit opens a lot --


def test_a_deposit_opens_a_lot_for_its_amount_and_its_deposit_date(service):
    deposit(service, euros="100")

    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]
    assert service.outstanding_eur(CUSTOMER) == Decimal("100.00")


def test_a_deposit_lot_keeps_the_cents_the_points_side_floors_away(service):
    """Spec D4 floors *points* to whole euros. The money is money."""
    deposit(service, euros="10.99")

    assert service.balance(CUSTOMER) == 10
    assert standing(service) == [(Decimal("10.99"), "2027-03-14")]


def test_a_deposit_too_small_to_earn_a_point_still_opens_a_lot(service):
    """A €0.99 lot vests nothing, and a lot that vests nothing is still a lot.

    Spec D24 already accepts a €5 lot vesting 0. What must not happen is the
    money vanishing from the money side because the points side had nothing
    to say about it.
    """
    result = deposit(service, euros="0.99")

    assert result.points_delta == 0
    assert service.balance(CUSTOMER) == 0
    assert standing(service) == [(Decimal("0.99"), "2027-03-14")]


def test_each_deposit_runs_its_own_clock(service, clock):
    """User story 22: a new deposit never resets an older one's progress."""
    deposit(service, euros="100", deposit_id="dep-old")
    clock.advance(timedelta(days=30))
    deposit(service, euros="50", deposit_id="dep-new")

    assert standing(service) == [
        (Decimal("100.00"), "2027-03-14"),
        (Decimal("50.00"), "2027-04-13"),
    ]


def test_a_customer_reads_only_their_own_deposit_lots(service):
    deposit(service, euros="100", deposit_id="dep-a")
    deposit(service, euros="25", deposit_id="dep-b", customer_id=OTHER_CUSTOMER)

    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]
    assert standing(service, OTHER_CUSTOMER) == [(Decimal("25.00"), "2027-03-14")]


def test_only_an_external_credit_opens_a_deposit_lot(service):
    """Spec D3, on the money side: a lot that could vest is a lot that earns.

    Interest, an internal transfer and a refund earn no points, so they must
    open nothing either — a lot vesting 10% of an interest credit would print
    points by exactly the route D3 closes.
    """
    for index, source in enumerate(
        (DepositSource.INTEREST, DepositSource.INTERNAL_TRANSFER, DepositSource.REFUND)
    ):
        deposit(service, euros="100", deposit_id=f"dep-{index}", source=source)

    assert standing(service) == []
    assert service.outstanding_eur(CUSTOMER) == Decimal("0.00")


def test_a_redelivered_deposit_opens_exactly_one_lot(service):
    """The feed is at-least-once; the lot is opened once (glossary: exactly-once)."""
    deposit(service, euros="100")
    deposit(service, euros="100")
    deposit(service, euros="100")

    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]


def test_a_redelivered_sub_euro_deposit_opens_exactly_one_lot(service):
    """The money side's guard is the money side's own (spec D6).

    A €0.99 deposit leaves no points movement behind, so the points ledger has
    nothing to recognise a redelivery by. The deposit-lot ledger does.
    """
    deposit(service, euros="0.99")
    deposit(service, euros="0.99")

    assert standing(service) == [(Decimal("0.99"), "2027-03-14")]


# ------------------------------------------------ withdrawals consume lots ----


def test_a_withdrawal_consumes_the_oldest_lot_first(service, clock):
    """Spec D8: one ordering rule, and the money side obeys it too."""
    deposit(service, euros="100", deposit_id="dep-old")
    clock.advance(timedelta(days=30))
    deposit(service, euros="50", deposit_id="dep-new")

    withdraw(service, euros="100")

    assert standing(service) == [(Decimal("50.00"), "2027-04-13")]


def test_a_withdrawal_spanning_two_lots_empties_the_older_and_bites_the_newer(service, clock):
    deposit(service, euros="100", deposit_id="dep-old")
    clock.advance(timedelta(days=30))
    deposit(service, euros="50", deposit_id="dep-new")

    withdraw(service, euros="120")

    assert standing(service) == [(Decimal("30.00"), "2027-04-13")]


def test_a_partial_withdrawal_splits_the_lot_and_the_rest_keeps_its_anniversary(service, clock):
    """Spec D25, the load-bearing decision on the bonus.

    Taking out €30 leaves €70 standing under the *original* deposit date, so
    the surviving portion vests on schedule. Whole-lot forfeiture would punish
    a €1 withdrawal exactly as hard as a €100 one.
    """
    deposit(service, euros="100")

    clock.advance(timedelta(days=180))
    withdraw(service, euros="30")

    assert standing(service) == [(Decimal("70.00"), "2027-03-14")]
    # And the clock really did not restart: it comes round a year after the
    # deposit, not a year after the withdrawal.
    assert service.deposit_lots(CUSTOMER)[0].next_anniversary == FIRST_ANNIVERSARY


def test_a_withdrawal_of_cents_takes_cents(service):
    deposit(service, euros="100.55")

    withdraw(service, euros="30.05")

    assert standing(service) == [(Decimal("70.50"), "2027-03-14")]


def test_a_withdrawal_larger_than_everything_outstanding_leaves_no_negative_lot(service):
    """Saving Streak does not own the money (spec D1).

    An account can hold euros that never arrived as a deposit lot — money from
    before the customer enrolled, or interest, which earns nothing. More can
    leave than this system ever saw arrive, and what that must never produce is
    a lot with a negative amount on it.
    """
    deposit(service, euros="100")

    withdraw(service, euros="500")

    assert standing(service) == []
    assert service.outstanding_eur(CUSTOMER) == Decimal("0.00")

    # And the next deposit stands in full: nothing was carried over as a debt.
    deposit(service, euros="40", deposit_id="dep-2")
    assert standing(service) == [(Decimal("40.00"), "2027-03-14")]


def test_a_withdrawal_too_large_for_the_ledger_is_refused_rather_than_crashing(service):
    """The amount now reaches a ledger, so the ledger's ceiling applies to it.

    A refusal is a `DomainError`, which the HTTP adapter maps to a 400. What
    this pins is that the arithmetic never raises something else out of the
    seam — the deposit lot the customer really has is untouched either way.
    """
    deposit(service, euros="100")

    with pytest.raises(DomainError):
        withdraw(service, euros="1E+5000")

    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]


def test_a_withdrawal_against_nothing_outstanding_is_accepted(service):
    withdraw(service, euros="80")

    assert standing(service) == []
    assert service.outstanding_eur(CUSTOMER) == Decimal("0.00")


def test_a_redelivered_withdrawal_consumes_once(service):
    deposit(service, euros="100")

    withdraw(service, euros="30")
    withdraw(service, euros="30")
    withdraw(service, euros="30")

    assert standing(service) == [(Decimal("70.00"), "2027-03-14")]


def test_two_different_withdrawals_both_consume(service):
    deposit(service, euros="100")

    withdraw(service, euros="30", withdrawal_id="wd-1")
    withdraw(service, euros="25", withdrawal_id="wd-2")

    assert standing(service) == [(Decimal("45.00"), "2027-03-14")]


# --------------------------------------------------------- reversed deposits --


def test_a_reversed_deposit_removes_its_deposit_lot(service):
    """Spec D11 on the money side: a bounced transfer vests nothing."""
    deposit(service, euros="100")

    reverse(service, deposit_id="dep-1")

    assert standing(service) == []


def test_a_reversal_removes_only_its_own_lot_whatever_its_place_in_the_queue(service, clock):
    deposit(service, euros="100", deposit_id="dep-old")
    clock.advance(timedelta(days=30))
    deposit(service, euros="50", deposit_id="dep-new")

    reverse(service, deposit_id="dep-old")

    assert standing(service) == [(Decimal("50.00"), "2027-04-13")]


def test_a_reversal_gives_back_what_a_withdrawal_took_from_a_lot_that_never_stood(
    service, clock
):
    """Spec D11, and the reason the drain is derived rather than frozen.

    €40 left the account while the customer appeared to hold €150. Once the
    first €100 is revealed never to have arrived, only €50 ever did, so the €40
    came out of *that* — and €10 is what is standing. Taking the reversal as
    "remove whatever is left of its own lot" instead would leave the €50 lot
    untouched and the customer holding €50 of money nobody paid in, on a live
    anniversary clock the loyalty-rate bonus would vest against.
    """
    deposit(service, euros="100", deposit_id="dep-old")
    clock.advance(timedelta(days=30))
    deposit(service, euros="50", deposit_id="dep-new")
    withdraw(service, euros="40")

    reverse(service, deposit_id="dep-old")

    assert standing(service) == [(Decimal("10.00"), "2027-04-13")]


def test_a_reversal_of_a_lot_a_withdrawal_already_drained_leaves_nothing_standing(
    service, clock
):
    """Two €100 deposits, €150 withdrawn, and the first deposit bounced.

    Only the second €100 ever really arrived and €150 left, so nothing is
    standing at all and €50 of that withdrawal matched no deposit lot. The
    drained lot must not go on absorbing a withdrawal it was never there for.
    """
    deposit(service, euros="100", deposit_id="dep-a")
    clock.advance(timedelta(days=1))
    deposit(service, euros="100", deposit_id="dep-b")
    withdraw(service, euros="150")
    assert standing(service) == [(Decimal("50.00"), "2027-03-15")]

    reverse(service, deposit_id="dep-a")

    assert standing(service) == []
    assert service.outstanding_eur(CUSTOMER) == Decimal("0.00")
    # And the points side clawed its 100 back, as spec D11 has always required.
    assert service.balance(CUSTOMER) == 100


def test_a_reversal_that_arrives_before_its_deposit_leaves_no_lot_standing(service):
    """The feed is unordered, so a reversal can land first (glossary)."""
    reverse(service, deposit_id="dep-1")

    deposit(service, euros="100")

    assert standing(service) == []


def test_a_redelivered_reversal_removes_the_lot_once(service):
    deposit(service, euros="100", deposit_id="dep-old")
    reverse(service, deposit_id="dep-old")
    reverse(service, deposit_id="dep-old")

    deposit(service, euros="50", deposit_id="dep-new")

    assert standing(service) == [(Decimal("50.00"), "2027-03-14")]


# --------------------------------------------------- the ledgers stay two ----


def test_a_claim_never_touches_a_deposit_lot(service):
    """Spec D6: two ledgers, touching at exactly one point — and not this one."""
    deposit(service, euros="100")
    before = standing(service)

    result = service.claim(
        ClaimReward(customer_id=CUSTOMER, item_id=CINEMA, idempotency_key="key-1")
    )

    assert result.points_delta == -100
    assert service.balance(CUSTOMER) == 0
    assert standing(service) == before == [(Decimal("100.00"), "2027-03-14")]


def test_spending_every_point_leaves_the_money_standing(service):
    """The customer's points are gone; the money never left the account."""
    deposit(service, euros="100")
    service.claim(ClaimReward(customer_id=CUSTOMER, item_id=CINEMA, idempotency_key="key-1"))

    assert service.balance(CUSTOMER) == 0
    assert service.outstanding_eur(CUSTOMER) == Decimal("100.00")


def test_a_points_lot_expiring_never_touches_a_deposit_lot(service, clock):
    """Twelve months of points expiry, and the money is still there.

    Spec T6: the sweep is asserted re-runnable here as everywhere, and what it
    must leave alone includes the whole of the other ledger.
    """
    deposit(service, euros="100")
    clock.advance(timedelta(days=366))
    before = standing(service)

    first = service.run_daily_sweep()
    after_one = standing(service)
    second = service.run_daily_sweep()

    assert (first.lots_expired, first.points_expired) == (1, 100)
    assert (second.lots_expired, second.points_expired) == (0, 0)
    # The anniversary the sweep passed vested 10 points against the €100 still
    # standing (spec D22), and took nothing off the money side to pay them.
    assert (first.anniversaries_vested, first.points_vested) == (1, 10)
    assert (second.anniversaries_vested, second.points_vested) == (0, 0)
    assert service.balance(CUSTOMER) == 10
    assert before == after_one == standing(service) == [(Decimal("100.00"), "2028-03-14")]


def test_a_withdrawal_never_touches_the_points_side(service):
    """Spec D10: withdrawals never cost base points, and leave no movement."""
    deposit(service, euros="100")
    before = points_page(service)

    withdraw(service, euros="100")

    assert points_page(service) == before
    assert service.balance(CUSTOMER) == 100
    assert standing(service) == []


def test_a_withdrawal_does_not_stop_the_customer_claiming(service):
    deposit(service, euros="100")
    withdraw(service, euros="100")

    result = service.claim(
        ClaimReward(customer_id=CUSTOMER, item_id=CINEMA, idempotency_key="key-1")
    )

    assert result.points_delta == -100


def test_a_reversal_undoes_both_sides(service):
    """The one event that wrote to both ledgers is undone on both."""
    deposit(service, euros="100")
    service.claim(ClaimReward(customer_id=CUSTOMER, item_id=CHARITY, idempotency_key="key-1"))

    reverse(service, deposit_id="dep-1")

    assert service.balance(CUSTOMER) == -10
    assert standing(service) == []


# ------------------------------------------------------- the recurring clock --


def test_the_anniversary_comes_round_every_year_the_lot_survives(service, clock):
    """Spec D22: a recurring clock, not a one-off one (user story 23)."""
    deposit(service, euros="100")

    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]

    clock.set(FIRST_ANNIVERSARY - timedelta(minutes=1))
    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]

    clock.set(FIRST_ANNIVERSARY)
    assert standing(service) == [(Decimal("100.00"), "2028-03-14")]

    clock.advance(timedelta(days=366))
    assert standing(service) == [(Decimal("100.00"), "2029-03-14")]


def test_a_leap_day_lot_comes_round_on_the_last_day_february_has(worlds):
    """Spec D27, and it clamps from the deposit date rather than drifting."""
    service, clock = worlds("leap-day", at=in_brussels(datetime(2028, 2, 29, 9, 0)))
    deposit(service, euros="100")

    assert standing(service) == [(Decimal("100.00"), "2029-02-28")]

    clock.set(in_brussels(datetime(2029, 3, 1, 9, 0)))
    assert standing(service) == [(Decimal("100.00"), "2030-02-28")]

    # Back to a leap year, on the day the deposit really landed on.
    clock.set(in_brussels(datetime(2031, 3, 1, 9, 0)))
    assert standing(service) == [(Decimal("100.00"), "2032-02-29")]


def test_the_lots_read_closest_anniversary_first_not_oldest_first(service, clock):
    """User story 26: which deposits are closest to vesting.

    It is a different question from which one a withdrawal takes next, and
    once a lot has survived an anniversary the two genuinely disagree.
    """
    deposit(service, euros="100", deposit_id="dep-old")
    clock.advance(timedelta(days=300))
    deposit(service, euros="50", deposit_id="dep-new")

    clock.set(PINNED_NOW + timedelta(days=400))

    # The older lot has already passed its first anniversary, so the newer one
    # is the next to come round.
    assert standing(service) == [
        (Decimal("50.00"), "2028-01-08"),
        (Decimal("100.00"), "2028-03-14"),
    ]
    # And a withdrawal still takes the oldest first, whatever the read order.
    withdraw(service, euros="100")
    assert standing(service) == [(Decimal("50.00"), "2028-01-08")]


def test_a_lot_read_at_a_future_instant_answers_for_that_instant(service):
    """`as_of` decides nothing and writes nothing; it says when to ask."""
    deposit(service, euros="100")

    assert standing(service, at=FIRST_ANNIVERSARY - timedelta(minutes=1)) == [
        (Decimal("100.00"), "2027-03-14")
    ]
    assert standing(service, at=FIRST_ANNIVERSARY) == [(Decimal("100.00"), "2028-03-14")]
    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]


def test_a_read_past_the_end_of_the_calendar_is_refused_rather_than_crashing(service):
    """`as_of` is customer input, and the calendar it asks about has an end.

    A deposit lot's anniversary clock recurs (spec D22), so reading at an
    instant late enough asks it for a date no `datetime` can hold. That is a
    refusal — a `DomainError`, which the HTTP adapter maps to a 400 — and not
    an arithmetic error escaping the seam as a crash. What the customer really
    has is untouched either way.
    """
    deposit(service, euros="100")

    with pytest.raises(DomainError):
        service.deposit_lots(CUSTOMER, in_brussels(datetime(9999, 12, 31)))

    assert standing(service) == [(Decimal("100.00"), "2027-03-14")]


def test_every_read_on_the_seam_refuses_the_same_far_future_instant(service):
    """One instant, one answer, whichever question is asked at it.

    The demo reads the balance, the history and the deposit lots together at
    one `as_of` (spec D45). If the money side refused an instant the points
    side answered, a single page would show two readings of two different
    moments under one caption.
    """
    deposit(service, euros="100")
    too_late = in_brussels(datetime(9999, 12, 31))

    for read in (service.balance, service.history, service.deposit_lots):
        with pytest.raises(DomainError):
            read(CUSTOMER, too_late)


def test_the_last_readable_instant_still_answers(service):
    """The refusal starts past the end of the calendar, not before it."""
    deposit(service, euros="100")

    assert standing(service, at=LAST_READABLE_INSTANT) == [(Decimal("100.00"), "9999-03-14")]
    assert service.balance(CUSTOMER, LAST_READABLE_INSTANT) == 0  # long since expired


# ------------------------------------------------------- table-driven (T4) ----


@dataclass(frozen=True)
class Timeline:
    """One run of the calendar on the money side: what happened, what stands."""

    name: str
    #: (day offset, "deposit" | "withdraw" | "reverse", euros or deposit id)
    steps: tuple[tuple[int, str, str], ...]
    read_on: int
    #: (outstanding euros, next anniversary), closest anniversary first.
    standing: tuple[tuple[str, str], ...]


TIMELINES = (
    Timeline("nothing deposited, nothing standing", (), 400, ()),
    Timeline(
        "untouched lots stand in full, each on its own clock",
        ((0, "deposit", "100"), (30, "deposit", "50")),
        60,
        (("100.00", "2027-03-14"), ("50.00", "2027-04-13")),
    ),
    Timeline(
        "a withdrawal takes the oldest lot first",
        ((0, "deposit", "100"), (30, "deposit", "50"), (60, "withdraw", "100")),
        90,
        (("50.00", "2027-04-13"),),
    ),
    Timeline(
        "a partial withdrawal splits the oldest and leaves its date alone",
        ((0, "deposit", "100"), (30, "deposit", "50"), (60, "withdraw", "30")),
        90,
        (("70.00", "2027-03-14"), ("50.00", "2027-04-13")),
    ),
    Timeline(
        "a withdrawal spanning two lots empties one and splits the next",
        ((0, "deposit", "100"), (30, "deposit", "50"), (60, "withdraw", "120")),
        90,
        (("30.00", "2027-04-13"),),
    ),
    Timeline(
        "a withdrawal larger than everything leaves nothing, not a negative lot",
        ((0, "deposit", "100"), (60, "withdraw", "500")),
        90,
        (),
    ),
    Timeline(
        "cents are money, and they survive a withdrawal of cents",
        ((0, "deposit", "100.55"), (10, "withdraw", "30.05")),
        60,
        (("70.50", "2027-03-14"),),
    ),
    Timeline(
        "a reversal removes its own lot and leaves the queue behind it",
        ((0, "deposit", "100"), (30, "deposit", "50"), (60, "reverse", "dep-0")),
        90,
        (("50.00", "2027-04-13"),),
    ),
    Timeline(
        "a reversal of the only lot leaves the withdrawal matching nothing",
        ((0, "deposit", "100"), (10, "withdraw", "40"), (20, "reverse", "dep-0")),
        60,
        (),
    ),
    Timeline(
        "a reversal re-settles a withdrawal that had crossed the reversed lot",
        (
            (0, "deposit", "100"),
            (10, "deposit", "50"),
            (20, "withdraw", "40"),
            (30, "reverse", "dep-0"),
        ),
        60,
        (("10.00", "2027-03-24"),),
    ),
    Timeline(
        "a reversal of a lot a withdrawal had already drained leaves nothing",
        (
            (0, "deposit", "100"),
            (10, "deposit", "100"),
            (20, "withdraw", "150"),
            (30, "reverse", "dep-0"),
        ),
        60,
        (),
    ),
    Timeline(
        "an oversized withdrawal is not a debt against the next deposit",
        ((0, "deposit", "100"), (10, "withdraw", "500"), (20, "deposit", "40")),
        60,
        (("40.00", "2027-04-03"),),
    ),
    Timeline(
        "a lot that survived a year is read against its next anniversary",
        ((0, "deposit", "100"),),
        400,
        (("100.00", "2028-03-14"),),
    ),
    Timeline(
        "a withdrawal after an anniversary still splits and still keeps the date",
        ((0, "deposit", "100"), (400, "withdraw", "40")),
        400,
        (("60.00", "2028-03-14"),),
    ),
)


@pytest.mark.parametrize("timeline", TIMELINES, ids=lambda t: t.name)
def test_deposit_lot_timelines(service, clock, timeline: Timeline):
    """Spec T4: timelines in, outstanding amounts and anniversaries out."""
    for index, (day, kind, value) in enumerate(timeline.steps):
        clock.set(PINNED_NOW + timedelta(days=day))
        if kind == "deposit":
            deposit(service, euros=value, deposit_id=f"dep-{index}")
        elif kind == "withdraw":
            withdraw(service, euros=value, withdrawal_id=f"wd-{index}")
        else:
            reverse(service, deposit_id=value)

    clock.set(PINNED_NOW + timedelta(days=timeline.read_on))

    assert standing(service) == [
        (Decimal(amount), anniversary) for amount, anniversary in timeline.standing
    ]
    assert service.outstanding_eur(CUSTOMER) == sum(
        (Decimal(amount) for amount, _ in timeline.standing), Decimal("0.00")
    )

    # Spec T6: the sweep records points expiry and touches no deposit lot,
    # whatever the timeline, however many times it runs.
    before = standing(service)
    service.run_daily_sweep()
    service.run_daily_sweep()
    assert standing(service) == before
