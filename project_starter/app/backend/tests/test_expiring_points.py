"""Oldest points first, and points that die at twelve months (spec D8/D17).

Everything here goes through the seam of spec D42: events and commands in,
balances, history and sweep results out. Nothing asserts on a lot identifier,
on the ledger's rows, or on the order in which the service calls anything —
those are the details the exercises refactor (spec T1).

Time travel is the injected clock and `run_daily_sweep`, never a sleep and
never a wall-clock read (spec T2/T3).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal

import pytest

from conftest import PINNED_NOW, FakeVoucherIssuer
from saving_streak.claims import ClaimRecords
from saving_streak.clock import FixedClock, in_brussels
from saving_streak.db import connect
from saving_streak.deposits import DepositLedger
from saving_streak.events import ClaimReward, DepositReversed, DomainError, MoneyDeposited
from saving_streak.events import MovementReason as Reason
from saving_streak.ledger import PointsLedger
from saving_streak.migrations import ensure_schema
from saving_streak.service import SavingStreakService

CUSTOMER = "cust-alice"
OTHER_CUSTOMER = "cust-bob"

CHARITY = "charity-donation"  # 10 points
COFFEE = "coffee-or-snack-voucher"  # 40 points
CINEMA = "cinema-ticket"  # 100 points

#: A lot earned at `PINNED_NOW` stops being spendable on this day, at the same
#: time of day. Spelled out rather than computed, so the test says what twelve
#: months means instead of repeating the code that decides it.
TWELVE_MONTHS_ON = in_brussels(datetime(2027, 3, 14, 10, 30))


def deposit(service, *, euros: str, deposit_id: str, customer_id: str = CUSTOMER):
    return service.handle(
        MoneyDeposited(
            customer_id=customer_id,
            account_id="acc-1",
            deposit_id=deposit_id,
            amount_eur=Decimal(euros),
        )
    )


def claim(service, item_id: str, *, key: str, customer_id: str = CUSTOMER):
    return service.claim(ClaimReward(customer_id=customer_id, item_id=item_id, idempotency_key=key))


def expiries(service, customer_id: str = CUSTOMER):
    """The expiries the customer can read, materialised or not."""
    return [m for m in service.history(customer_id) if m.reason is Reason.EXPIRY]


def expiries_at(service, at, customer_id: str = CUSTOMER):
    """The same, read at an instant the clock has not reached."""
    return [m for m in service.history(customer_id, at) if m.reason is Reason.EXPIRY]


def readable(service, customer_id: str = CUSTOMER):
    """Everything the customer can see, as plain values two runs can compare."""
    return (
        service.balance(customer_id),
        [(m.occurred_at, m.points, m.reason, m.description) for m in service.history(customer_id)],
    )


@pytest.fixture
def worlds(tmp_path):
    """Build independent copies of the whole system on their own storage.

    Running a timeline two ways needs two ledgers. This is the only place in
    the file that reaches behind the seam, and it reaches there to *build* the
    system, never to assert on it (spec T1).
    """
    connections = []

    def make(name: str) -> tuple[SavingStreakService, FixedClock]:
        conn = connect(tmp_path / f"{name}.db")
        ensure_schema(conn)
        connections.append(conn)
        clock = FixedClock(PINNED_NOW)
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


# ------------------------------------------------- twelve months, then gone --


def test_a_lot_is_gone_from_the_balance_on_day_366_without_any_sweep(service, clock):
    """Spec D18: expiry happens when the clock passes, not when the job runs."""
    deposit(service, euros="10", deposit_id="dep-1")

    clock.advance(timedelta(days=366))

    assert service.balance(CUSTOMER) == 0


def test_a_lot_is_gone_from_the_balance_on_day_366_when_the_sweep_has_run(service, clock):
    deposit(service, euros="10", deposit_id="dep-1")

    # Every night, in order, exactly the way production would run it (spec T3).
    for _ in range(366):
        clock.advance(timedelta(days=1))
        service.run_daily_sweep()

    # The ten base points are gone. The single point left is the loyalty bonus
    # the deposit lot vested on the anniversary it passed on the way (spec
    # D22), which is a new lot on its own twelve-month clock.
    assert service.balance(CUSTOMER) == 1


def test_points_are_still_spendable_the_day_before_they_expire(service, clock):
    """User story 17: twelve months is meant to be generous, so it is a full one."""
    deposit(service, euros="10", deposit_id="dep-1")

    clock.advance(timedelta(days=364))

    assert service.balance(CUSTOMER) == 10
    assert claim(service, CHARITY, key="key-1").points_delta == -10


def test_the_lot_dies_twelve_calendar_months_on_not_365_days(service, clock):
    deposit(service, euros="10", deposit_id="dep-1")

    clock.set(TWELVE_MONTHS_ON - timedelta(minutes=1))
    assert service.balance(CUSTOMER) == 10

    clock.set(TWELVE_MONTHS_ON)
    assert service.balance(CUSTOMER) == 0


def test_a_leap_day_lot_expires_on_the_last_day_the_month_has(worlds):
    """Spec D27: an anniversary clamps to a day the target month really has."""
    service, clock = worlds("leap-day")
    clock.set(in_brussels(datetime(2028, 2, 29, 9, 0)))
    deposit(service, euros="10", deposit_id="dep-1")

    clock.set(in_brussels(datetime(2029, 2, 28, 8, 59)))
    assert service.balance(CUSTOMER) == 10

    clock.set(in_brussels(datetime(2029, 2, 28, 9, 0)))
    assert service.balance(CUSTOMER) == 0


# -------------------------------------------------------------- oldest first --


def test_claiming_drains_the_oldest_lot_first(service, clock):
    """Spec D8/D18: what expires is exactly what nobody touched for a year.

    Two lots a month apart, and one claim that could be paid for out of
    either. Asserting which lot was drained means asking what survives its
    anniversary — there is nothing else at the seam to ask, and nothing else
    the customer can see.
    """
    deposit(service, euros="10", deposit_id="dep-old")
    clock.advance(timedelta(days=30))
    deposit(service, euros="10", deposit_id="dep-new")

    claim(service, CHARITY, key="key-1")

    # Day 365: the older lot's anniversary. It was the one that got spent, so
    # nothing dies and the newer lot is untouched.
    clock.set(TWELVE_MONTHS_ON)
    assert service.balance(CUSTOMER) == 10
    assert expiries(service) == []

    # Day 395: the newer lot's own anniversary comes round and takes it.
    clock.advance(timedelta(days=30))
    assert service.balance(CUSTOMER) == 0


def test_the_next_lot_pays_the_rest_once_the_oldest_is_empty(service, clock):
    """Spec D8: oldest first, then the next — one claim across two lots."""
    deposit(service, euros="30", deposit_id="dep-old")
    clock.advance(timedelta(days=30))
    deposit(service, euros="30", deposit_id="dep-new")

    claim(service, COFFEE, key="key-1")  # 40 points: all of the first, 10 of the second

    clock.set(TWELVE_MONTHS_ON)
    assert service.balance(CUSTOMER) == 20
    assert expiries(service) == []

    clock.advance(timedelta(days=30))
    assert service.balance(CUSTOMER) == 0


def test_only_what_is_left_of_a_part_spent_lot_expires(service, clock):
    deposit(service, euros="40", deposit_id="dep-1")
    claim(service, CHARITY, key="key-1")

    clock.set(TWELVE_MONTHS_ON)

    (expiry,) = expiries(service)
    assert expiry.points == -30
    assert service.balance(CUSTOMER) == 0


# ------------------------------------------------- expired points cannot pay --


def test_a_claim_cannot_be_paid_for_with_expired_points_before_any_sweep(service, clock):
    """Spec D18: a late batch must never let someone spend dead points."""
    deposit(service, euros="100", deposit_id="dep-1")

    clock.advance(timedelta(days=366))

    with pytest.raises(DomainError) as refused:
        claim(service, CHARITY, key="key-1")
    assert "the balance is 0" in str(refused.value)
    assert service.balance(CUSTOMER) == 0
    assert service.claims(CUSTOMER) == []


def test_a_claim_is_paid_for_out_of_what_is_still_alive(service, clock):
    deposit(service, euros="100", deposit_id="dep-old")
    clock.advance(timedelta(days=366))
    deposit(service, euros="100", deposit_id="dep-new")

    claim(service, CINEMA, key="key-1")

    assert service.balance(CUSTOMER) == 0


# --------------------------------------------------------------- the sweep ---


def test_the_sweep_writes_the_expiry_down_and_changes_nothing_by_running(service, clock):
    """Spec D18 and user story 38, in one: the sweep records, it does not decide.

    True of expiry, and deliberately not true of everything the sweep does:
    vesting a loyalty bonus is the sweep deciding (spec D22). This deposit is
    under €10, so its bonus floors to nothing (spec D24) and the night has
    only the expiry to write down.
    """
    deposit(service, euros="9", deposit_id="dep-1")
    clock.advance(timedelta(days=366))

    before = readable(service)
    result = service.run_daily_sweep()

    assert result.lots_expired == 1
    assert result.points_expired == 9
    assert (result.anniversaries_vested, result.points_vested) == (0, 0)
    assert result.customers_affected == 1
    assert readable(service) == before


def test_the_sweep_is_re_runnable_for_the_same_business_date(service, clock):
    """Spec T6. A failed night is replayed without double-crediting anyone."""
    deposit(service, euros="10", deposit_id="dep-1")
    clock.advance(timedelta(days=366))
    on = clock.today()

    first = service.run_daily_sweep(on)
    settled = readable(service)
    second = service.run_daily_sweep(on)
    third = service.run_daily_sweep(on)

    assert first.points_expired == 10
    assert (second.lots_expired, second.points_expired) == (0, 0)
    assert (third.lots_expired, third.points_expired) == (0, 0)
    assert readable(service) == settled


def test_catching_two_nights_up_lands_where_running_each_night_lands(worlds):
    """Spec D20: missed dates are caught up by running them in order."""
    nightly, nightly_clock = worlds("nightly")
    caught_up, caught_up_clock = worlds("caught-up")

    for service, clock in ((nightly, nightly_clock), (caught_up, caught_up_clock)):
        deposit(service, euros="10", deposit_id="dep-1")
        clock.set(TWELVE_MONTHS_ON + timedelta(days=2))

    missed = date(2027, 3, 15)
    tonight = date(2027, 3, 16)

    nightly.run_daily_sweep(missed)
    nightly.run_daily_sweep(tonight)

    caught_up.run_daily_sweep(tonight)

    assert readable(nightly) == readable(caught_up)
    # The base points expired and the anniversary the catch-up passed vested
    # its bonus, on its own date either way (spec D22).
    assert nightly.balance(CUSTOMER) == 1


def test_the_sweep_runs_against_the_date_it_is_given(service, clock):
    """Spec D43: a pinned date, called directly — no scheduler in the way."""
    deposit(service, euros="10", deposit_id="dep-1")
    clock.set(TWELVE_MONTHS_ON + timedelta(days=2))

    # The morning of the anniversary: the sweep runs at 03:00, and the lot had
    # until 10:30. That night had nothing to write down.
    early = service.run_daily_sweep(date(2027, 3, 14))
    assert early.lots_expired == 0

    # The night after it is the one that finds it — whichever night actually
    # makes the call.
    tonight = service.run_daily_sweep(date(2027, 3, 15))
    assert (tonight.lots_expired, tonight.points_expired) == (1, 10)
    assert tonight.business_date == date(2027, 3, 15)


def test_the_sweep_takes_todays_date_from_the_injected_clock(service, clock):
    deposit(service, euros="10", deposit_id="dep-1")

    assert service.run_daily_sweep().lots_expired == 0

    clock.advance(timedelta(days=366))

    assert service.run_daily_sweep().business_date == clock.today()
    assert service.balance(CUSTOMER) == 1  # the base expired, the bonus vested


def test_one_sweep_covers_every_customer(service, clock):
    deposit(service, euros="10", deposit_id="dep-a")
    deposit(service, euros="25", deposit_id="dep-b", customer_id=OTHER_CUSTOMER)

    clock.advance(timedelta(days=366))
    result = service.run_daily_sweep()

    assert (result.customers_affected, result.lots_expired, result.points_expired) == (2, 2, 35)
    assert (result.anniversaries_vested, result.points_vested) == (2, 3)
    # Each customer is left with their own lot's anniversary bonus (spec D22).
    assert service.balance(CUSTOMER) == 1
    assert service.balance(OTHER_CUSTOMER) == 2


def test_a_lot_spent_before_its_anniversary_is_never_written_off(service, clock):
    deposit(service, euros="10", deposit_id="dep-1")
    claim(service, CHARITY, key="key-1")

    clock.advance(timedelta(days=366))
    result = service.run_daily_sweep()

    assert (result.lots_expired, result.points_expired) == (0, 0)
    assert expiries(service) == []


# ------------------------------------------------------- balance and history --


def test_history_names_what_expired_and_when_before_the_sweep_runs(service, clock):
    """The customer reads the history to check the number (user story 5/19)."""
    deposit(service, euros="30", deposit_id="dep-1")
    clock.advance(timedelta(days=366))

    (expiry,) = expiries(service)

    assert expiry.points == -30
    assert expiry.occurred_at == TWELVE_MONTHS_ON
    assert "30 points" in expiry.description
    assert "2026-03-14" in expiry.description
    assert service.balance(CUSTOMER) == 0


def test_the_expiry_the_sweep_writes_is_the_one_already_shown(service, clock):
    deposit(service, euros="30", deposit_id="dep-1")
    clock.advance(timedelta(days=366))
    shown = expiries(service)

    service.run_daily_sweep()

    assert expiries(service) == shown


def test_the_expiry_reads_above_the_deposit_it_killed(service, clock):
    """Newest first, on when it happened — the expiry is a year later."""
    deposit(service, euros="30", deposit_id="dep-1")
    clock.advance(timedelta(days=366))
    service.run_daily_sweep()

    newest, bonus, oldest = service.history(CUSTOMER)

    assert (newest.reason, newest.points) == (Reason.EXPIRY, -30)
    # The same night vested the anniversary bonus, dated the start of the
    # anniversary; the expiry happened later that day and reads above it.
    assert (bonus.reason, bonus.points) == (Reason.VESTING, 3)
    assert (oldest.reason, oldest.points) == (Reason.DEPOSIT, 30)
    assert newest.occurred_at > bonus.occurred_at > oldest.occurred_at


def test_a_balance_read_at_a_future_instant_answers_for_that_instant(service, clock):
    """The seam decides what has expired; `as_of` only says when to ask."""
    deposit(service, euros="10", deposit_id="dep-1")

    assert service.balance(CUSTOMER) == 10
    assert service.balance(CUSTOMER, TWELVE_MONTHS_ON - timedelta(minutes=1)) == 10
    assert service.balance(CUSTOMER, TWELVE_MONTHS_ON) == 0
    assert [m.points for m in service.history(CUSTOMER, TWELVE_MONTHS_ON)] == [-10, 10]
    # Reading a future instant decides nothing and writes nothing.
    assert service.balance(CUSTOMER) == 10


def test_an_expiry_does_not_take_points_the_customer_had_already_spent(service, clock):
    """The balance stays the sum the customer can check (user story 5)."""
    deposit(service, euros="30", deposit_id="dep-1")
    claim(service, CHARITY, key="key-1")
    clock.advance(timedelta(days=366))
    service.run_daily_sweep()

    # Thirty earned, ten claimed, twenty expired, three vested on the
    # anniversary the sweep passed (spec D22) — and it still adds up.
    assert service.balance(CUSTOMER) == 3
    assert service.balance(CUSTOMER) == sum(m.points for m in service.history(CUSTOMER))


def test_a_clawback_against_expired_points_still_reads_back_negative(service, clock):
    """Spec D11 survives expiry: nothing is floored, and nothing is taken twice.

    The reversal claws back **exactly what the deposit credited** — its 30
    base points — and not the 3 the anniversary vested before it bounced. That
    is D11 read strictly, and it is an open question rather than a settled
    rule: whether a deposit that never stood should keep having paid a bonus
    is open question 1 of the agreed bonus specification, and the ticket that
    would settle it is blocked on it. What is settled either way is that the
    reversed lot stops standing, so no further anniversary vests.
    """
    deposit(service, euros="30", deposit_id="dep-1")
    clock.advance(timedelta(days=366))
    service.run_daily_sweep()

    service.handle(DepositReversed(customer_id=CUSTOMER, deposit_id="dep-1"))

    assert service.balance(CUSTOMER) == -27

    deposit(service, euros="30", deposit_id="dep-2")
    assert service.balance(CUSTOMER) == 3


def test_a_sweep_cannot_be_run_for_a_night_that_has_not_happened(service, clock):
    """The sweep records deaths; it is never asked to predict one.

    What a lot is worth on a night that has not come depends on what the
    customer spends between now and then, and a lot is written off exactly
    once (spec D20) — so a forward-dated run would freeze an amount that is
    not settled yet and no later night could correct it. Reading forward is
    free and decides nothing; writing forward is refused.
    """
    deposit(service, euros="200", deposit_id="dep-1")

    with pytest.raises(DomainError) as refused:
        service.run_daily_sweep(date(2027, 12, 1))
    assert "2027-12-01" in str(refused.value)

    # Nothing was written, and the future is still readable.
    assert service.balance(CUSTOMER) == 200
    assert expiries(service) == []
    assert service.balance(CUSTOMER, TWELVE_MONTHS_ON) == 0
    assert len(expiries_at(service, TWELVE_MONTHS_ON)) == 1

    # The customer goes on spending the lot, which is exactly what a
    # forward-dated write-off would have got wrong.
    claim(service, CINEMA, key="key-1")
    assert service.balance(CUSTOMER) == 100

    # A year on, only the hundred nobody touched is gone — and the customer
    # can still add the history up to the balance (user story 5, spec D36).
    clock.set(TWELVE_MONTHS_ON + timedelta(days=1))
    (expiry,) = expiries(service)
    assert expiry.points == -100
    assert "100 points" in expiry.description
    assert service.balance(CUSTOMER) == 0
    assert service.balance(CUSTOMER) == sum(m.points for m in service.history(CUSTOMER))

    # And the night it really dies writes exactly that, once.
    tonight = service.run_daily_sweep()
    assert (tonight.lots_expired, tonight.points_expired) == (1, 100)
    settled = readable(service)
    assert service.run_daily_sweep().lots_expired == 0
    assert readable(service) == settled
    assert service.balance(CUSTOMER) == sum(m.points for m in service.history(CUSTOMER))


def test_tonights_sweep_leaves_a_lot_that_is_still_alive_when_it_runs(service, clock):
    """Spec D20: the sweep's stamp is 03:00, and it looks no further than now.

    A lot that dies later today is still the customer's to spend, so a sweep
    started before it dies must not write it off — the same rule as the
    refusal above, inside a single business date.
    """
    clock.set(in_brussels(datetime(2026, 3, 14, 2, 30)))
    deposit(service, euros="200", deposit_id="dep-1")

    # An hour and a half before the lot's anniversary instant.
    clock.set(in_brussels(datetime(2027, 3, 14, 1, 0)))
    tonight = service.run_daily_sweep()
    assert (tonight.lots_expired, tonight.points_expired) == (0, 0)

    claim(service, CINEMA, key="key-1")

    clock.set(in_brussels(datetime(2027, 3, 15, 3, 30)))
    caught_up = service.run_daily_sweep()

    assert (caught_up.lots_expired, caught_up.points_expired) == (1, 100)
    # €200 standing vested 20 on the anniversary the first sweep ran into.
    assert service.balance(CUSTOMER) == 20
    assert service.balance(CUSTOMER) == sum(m.points for m in service.history(CUSTOMER))


def test_two_lots_dying_in_the_same_instant_read_the_same_before_and_after_the_sweep(
    service, clock
):
    """The sweep writes the history down; it does not re-order it."""
    deposit(service, euros="10", deposit_id="dep-a")
    deposit(service, euros="40", deposit_id="dep-b")
    clock.set(TWELVE_MONTHS_ON + timedelta(days=1))

    before = expiries(service)
    result = service.run_daily_sweep()

    assert (result.lots_expired, result.points_expired) == (2, 50)
    # The two expiries read exactly as they did before the job ran. The sweep
    # also vested the two anniversaries it passed, which is new history rather
    # than a re-ordering of this.
    assert expiries(service) == before
    assert (result.anniversaries_vested, result.points_vested) == (2, 5)


def test_the_balance_is_the_sum_of_the_history_at_every_instant(service, clock):
    """What the customer adds up on screen is the number on screen (story 5)."""
    deposit(service, euros="30", deposit_id="dep-1")
    clock.advance(timedelta(days=30))
    deposit(service, euros="30", deposit_id="dep-2")
    claim(service, COFFEE, key="key-1")

    for day in (0, 100, 335, 336, 365, 366, 396, 500):
        at = PINNED_NOW + timedelta(days=day)
        assert service.balance(CUSTOMER, at) == sum(
            m.points for m in service.history(CUSTOMER, at)
        ), f"day {day}"


# ------------------------------------------------------- table-driven (T4) ----


@dataclass(frozen=True)
class Timeline:
    """One run of the calendar: what happened, when, and what is left."""

    name: str
    #: (day offset, euros deposited)
    deposits: tuple[tuple[int, str], ...]
    #: (day offset, catalogue item claimed)
    claims: tuple[tuple[int, str], ...]
    read_on: int
    balance: int
    expired: int
    #: The loyalty bonus the sweep vests for the anniversaries this timeline
    #: has passed (spec D22). It is not in `balance`, which is read before the
    #: sweep runs: expiry has already happened by then and vesting has not.
    vested: int


TIMELINES = (
    Timeline("untouched, read the day before", ((0, "10"),), (), 364, 10, 0, 0),
    Timeline("untouched, read on the anniversary", ((0, "10"),), (), 365, 0, 10, 1),
    Timeline("untouched, read the day after", ((0, "10"),), (), 366, 0, 10, 1),
    Timeline(
        "the older lot was spent, so nothing dies",
        ((0, "10"), (30, "10")),
        ((60, CHARITY),),
        365,
        10,
        0,
        1,
    ),
    Timeline(
        "the newer lot was left, so the older one dies",
        ((0, "10"), (30, "10")),
        (),
        365,
        10,
        10,
        1,
    ),
    Timeline(
        "one claim across two lots leaves the remainder to die later",
        ((0, "30"), (30, "30")),
        ((60, COFFEE),),
        396,
        0,
        20,
        6,
    ),
    Timeline(
        "part of a lot spent, the rest expires",
        ((0, "40"),),
        ((10, CHARITY),),
        366,
        0,
        30,
        4,
    ),
    Timeline(
        "a lot earned after the first one died",
        ((0, "10"), (400, "25")),
        (),
        400,
        25,
        10,
        1,
    ),
    Timeline("nothing earned, nothing to expire", (), (), 400, 0, 0, 0),
)


@pytest.mark.parametrize("timeline", TIMELINES, ids=lambda t: t.name)
def test_expiry_timelines(service, clock, timeline: Timeline):
    """Spec T4: timelines in, balances and write-offs out."""
    moments = sorted(
        [(day, "deposit", value) for day, value in timeline.deposits]
        + [(day, "claim", value) for day, value in timeline.claims]
    )
    for index, (day, kind, value) in enumerate(moments):
        clock.set(PINNED_NOW + timedelta(days=day))
        if kind == "deposit":
            deposit(service, euros=value, deposit_id=f"dep-{index}")
        else:
            claim(service, value, key=f"key-{index}")

    clock.set(PINNED_NOW + timedelta(days=timeline.read_on))

    assert service.balance(CUSTOMER) == timeline.balance
    assert sum(-m.points for m in expiries(service)) == timeline.expired

    # The sweep writes down the expiry the read above already showed, and
    # vests whatever anniversaries the timeline has passed (spec D22). Running
    # it again does neither a second time (spec T6).
    expired_before = expiries(service)
    swept = service.run_daily_sweep()
    settled = readable(service)
    again = service.run_daily_sweep()

    # Not `points_expired`: the read above is taken at 10:30 and the sweep
    # stamps 03:00, so a lot that dies during the anniversary day is already
    # out of the balance and is written off the following night (spec D18).
    assert swept.points_vested == timeline.vested
    assert expiries(service) == expired_before
    assert service.balance(CUSTOMER) == timeline.balance + timeline.vested
    assert (again.points_expired, again.points_vested) == (0, 0)
    assert readable(service) == settled
