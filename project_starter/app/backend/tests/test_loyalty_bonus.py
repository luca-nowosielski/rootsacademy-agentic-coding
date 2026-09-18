"""The loyalty-rate bonus: a deposit lot vesting on its anniversary (spec D19-D24).

Ticket 01. One deposit lot, never withdrawn from, paying 10% of its base points
on the first anniversary it reaches. This is the single crossing between the two
ledgers of spec D6 — a vesting deposit lot minting a points lot — and it cannot
avoid the expiry interaction: base points earned on day zero reach twelve months
on exactly that anniversary, so the vest-before-expire ordering of spec D21 is
settled here.

Everything goes through the seam of spec D42: events in, balances, history and
sweep results out. Nothing asserts on a lot identifier or a ledger row (spec T1),
and time travel is the injected clock plus `run_daily_sweep`, never a sleep
(spec T2/T3).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from saving_streak.clock import in_brussels
from saving_streak.events import MoneyDeposited
from saving_streak.events import MovementReason as Reason

CUSTOMER = "cust-alice"

#: The deposit lands before the 03:00 sweep hour, so its base points reach
#: twelve months *before* the sweep on the anniversary rather than later the
#: same day. That is what makes the ordering of spec D21 observable: the vest
#: and the expiry of the money that earned it fall in the same night.
DEPOSITED_AT = in_brussels(datetime(2026, 3, 15, 2, 0))

#: Twelve calendar months on (spec D22). The bonus is dated the anniversary,
#: and an anniversary is a *date*: it is stamped at the start of that day
#: rather than at the hour the money happened to land.
ANNIVERSARY_DATE = date(2027, 3, 15)
FIRST_ANNIVERSARY = in_brussels(datetime(2027, 3, 15))

#: The bonus is an ordinary points lot with its own fresh twelve months (D19).
BONUS_EXPIRES = in_brussels(datetime(2028, 3, 15))


def deposit(service, *, euros: str, deposit_id: str = "dep-1", customer_id: str = CUSTOMER):
    return service.handle(
        MoneyDeposited(
            customer_id=customer_id,
            account_id="acc-1",
            deposit_id=deposit_id,
            amount_eur=Decimal(euros),
        )
    )


def readable(service, customer_id: str = CUSTOMER):
    """Everything the customer can see, as plain values two runs can compare."""
    return (
        service.balance(customer_id),
        service.outstanding_eur(customer_id),
        [(m.occurred_at, m.points, m.reason, m.description) for m in service.history(customer_id)],
    )


def vestings(service, customer_id: str = CUSTOMER):
    return [m for m in service.history(customer_id) if m.reason is Reason.VESTING]


@pytest.fixture
def anniversary_morning(service, clock):
    """A €1,000 deposit a year ago, and the clock on the morning it comes round.

    The sweep has not run yet: every test decides for itself what it asks of it.
    """
    clock.set(DEPOSITED_AT)
    deposit(service, euros="1000")
    clock.set(FIRST_ANNIVERSARY + timedelta(hours=10))
    return service


# ----------------------------------------------------- the first anniversary --


def test_an_untouched_deposit_vests_its_first_anniversary_bonus(anniversary_morning):
    """Spec D22 and example A, with the expiry of spec D21 in the same night.

    The 1,000 base points reached twelve months at 02:00 and the sweep runs at
    03:00, so this one night both vests the bonus and writes off the points the
    deposit originally earned. The customer is left holding the bonus.
    """
    result = anniversary_morning.run_daily_sweep(ANNIVERSARY_DATE)

    assert (result.lots_vested, result.points_vested) == (1, 100)
    assert (result.lots_expired, result.points_expired) == (1, 1000)
    assert anniversary_morning.balance(CUSTOMER) == 100


def test_nothing_vests_before_the_anniversary_arrives(service, clock):
    """Twelve months means twelve months: the night before pays nothing."""
    clock.set(DEPOSITED_AT)
    deposit(service, euros="1000")
    clock.set(FIRST_ANNIVERSARY - timedelta(hours=14))

    result = service.run_daily_sweep()

    assert (result.lots_vested, result.points_vested) == (0, 0)
    assert service.balance(CUSTOMER) == 1000


def test_the_bonus_is_dated_the_anniversary_and_runs_its_own_twelve_months(anniversary_morning):
    """Spec D19/LB9: dated the anniversary, with a fresh expiry clock from it.

    Read at the seam by asking what the balance will be either side of the
    bonus's own twelfth month — the ledger's expiry column is not this test's
    business (spec T1).
    """
    anniversary_morning.run_daily_sweep(ANNIVERSARY_DATE)

    assert [(m.occurred_at, m.points) for m in vestings(anniversary_morning)] == [
        (FIRST_ANNIVERSARY, 100)
    ]
    assert anniversary_morning.balance(CUSTOMER, BONUS_EXPIRES - timedelta(seconds=1)) == 100
    assert anniversary_morning.balance(CUSTOMER, BONUS_EXPIRES) == 0


def test_vesting_leaves_the_money_side_exactly_where_it_was(anniversary_morning):
    """Spec D6: the crossing mints points; it never spends a deposit lot."""
    before = anniversary_morning.outstanding_eur(CUSTOMER)

    anniversary_morning.run_daily_sweep(ANNIVERSARY_DATE)

    assert anniversary_morning.outstanding_eur(CUSTOMER) == before == Decimal("1000.00")


# ------------------------------------------------------------ whole points --


@pytest.mark.parametrize(
    ("deposited", "base_points", "bonus"),
    [
        ("5", 5, 0),
        ("9.99", 9, 0),
        ("10", 10, 1),
        ("30.50", 30, 3),
        ("1205", 1205, 120),
    ],
)
def test_the_bonus_is_floored_to_whole_points(service, clock, deposited, base_points, bonus):
    """Spec D24 and example B: floored to whole euros, then floored again.

    Ten per cent of a lot's base points, not ten per cent of its euros: €9.99
    is nine base points and nine tenths of a point, which is nothing.
    """
    clock.set(DEPOSITED_AT)
    deposit(service, euros=deposited)
    clock.set(FIRST_ANNIVERSARY + timedelta(hours=10))

    result = service.run_daily_sweep(ANNIVERSARY_DATE)

    assert (result.points_vested, result.points_expired) == (bonus, base_points)
    assert service.balance(CUSTOMER) == bonus


# ------------------------------------------------------------- re-runnable --


def test_re_running_the_anniversary_night_vests_the_bonus_once(anniversary_morning):
    """Spec D20/T6: a failed night is replayed without paying twice."""
    first = anniversary_morning.run_daily_sweep(ANNIVERSARY_DATE)
    settled = readable(anniversary_morning)
    second = anniversary_morning.run_daily_sweep(ANNIVERSARY_DATE)
    third = anniversary_morning.run_daily_sweep(ANNIVERSARY_DATE)

    assert (first.lots_vested, first.points_vested) == (1, 100)
    assert (second.lots_vested, second.points_vested) == (0, 0)
    assert (third.lots_vested, third.points_vested) == (0, 0)
    assert readable(anniversary_morning) == settled


def test_the_night_after_the_anniversary_vests_nothing_more(anniversary_morning, clock):
    """The bonus is paid for the anniversary, not for every night after it."""
    anniversary_morning.run_daily_sweep(ANNIVERSARY_DATE)

    clock.advance(timedelta(days=1))
    after = anniversary_morning.run_daily_sweep()

    assert (after.lots_vested, after.points_vested) == (0, 0)
    assert anniversary_morning.balance(CUSTOMER) == 100
