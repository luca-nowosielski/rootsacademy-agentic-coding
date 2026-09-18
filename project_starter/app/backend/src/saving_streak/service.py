"""The single application-service seam (spec D42).

Everything in, everything out. The seam accepts domain events and commands and
returns results; the two ledgers of spec D6 and the clock sit behind it as
injected collaborators. There is no domain rule in the HTTP adapter and none in
the UI — they are windows onto this module.

The two ledgers stay two. A deposit is the one event that writes to both — a
points lot on one side, a deposit lot on the other — and it writes to each
through that ledger's own guards, never using one to decide something about the
other. A claim touches only the points ledger; a withdrawal touches only the
deposit-lot ledger (spec D10). The single crossing of spec D6 — a vesting
deposit lot minting a points lot — happens in the nightly sweep, and it crosses
one way: the money side is read, the points side is written, and no deposit lot
is ever spent to pay a bonus.

Later tickets extend the same seam with `gift`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_FLOOR, Decimal

from .catalogue import Catalogue, CatalogueItem, current_catalogue
from .claims import ClaimRecord, ClaimRecords
from .clock import Clock, in_brussels, readable_instant
from .deposits import (
    DepositLedger,
    DepositLot,
    OpenedLot,
    anniversaries_through,
    cents_landing,
    cents_leaving,
    euros,
)
from .events import (
    ClaimReward,
    DepositEntryReason,
    DepositReversed,
    DomainError,
    MoneyDeposited,
    MoneyWithdrawn,
    MovementReason,
    identifier,
)
from .ledger import EXPIRY_MONTHS, PointsLedger, PointsLot, PointsMovement
from .vouchers import IssuanceRequest, VoucherIssuanceFailed, VoucherIssuer

#: The base mechanic: one point per euro deposited (spec D9).
POINTS_PER_EURO = 1

#: The loyalty rate: each anniversary a deposit lot survives pays this share of
#: the surviving portion's base points (spec D22). Of *base*, always — never of
#: base-plus-accrued, so the bonus does not compound (spec D23).
BONUS_RATE = Decimal("0.10")

#: The hour the nightly sweep runs, Europe/Brussels (spec D20). It is the
#: business date that identifies a run, not the moment it happens to start:
#: a night that failed is replayed by asking for the same date again.
SWEEP_HOUR = 3


def base_points_for(amount_eur: Decimal) -> int:
    """Floor a deposit to whole euros, then a point per euro (spec D4).

    €10.99 earns 10. Points are integers everywhere; there is no fractional
    point anywhere in the system.
    """
    whole_euros = int(amount_eur.to_integral_value(rounding=ROUND_FLOOR))
    return whole_euros * POINTS_PER_EURO


def bonus_points_for(outstanding_eur: Decimal) -> int:
    """The loyalty-rate bonus a lot pays on one anniversary (spec D22/D24).

    Floored twice, and the two floors are different questions. The first is
    the one every amount of money in this system answers: whole euros make
    whole base points, so a surviving €30.50 is 30 base points (spec D4). The
    second is the rate: a tenth of 30 points is 3 points, and a tenth of 9 is
    nothing at all. Neither floor can stand in for the other — €9.99 would
    round its way to a point through the first, and a €10 lot would lose its
    single point to the second if the rate were taken on the euros.

    A lot too small to pay anything pays nothing, and that is the intended
    answer rather than a rounding accident: a €5 deposit vests 0 (spec D24).
    """
    base = base_points_for(outstanding_eur)
    return int((base * BONUS_RATE).to_integral_value(rounding=ROUND_FLOOR))


@dataclass(frozen=True)
class EventResult:
    """What handling one event did to the customer's points."""

    customer_id: str
    points_delta: int
    balance: int


@dataclass(frozen=True)
class SweepResult:
    """What one night of the sweep did.

    A re-run of the same business date reports zeros: everything it would have
    written is already written, and nothing about the customer changed (user
    story 38).
    """

    business_date: date
    swept_at: datetime
    #: Anniversaries that paid a bonus (spec D22) — not lots, because one lot
    #: caught up over three years pays three of them. An anniversary whose
    #: bonus floored to nothing is not counted: nothing was written, so there
    #: is nothing for a re-run to report differently.
    anniversaries_vested: int
    points_vested: int
    lots_expired: int
    points_expired: int
    customers_affected: int


@dataclass(frozen=True)
class DepositStanding:
    """What one customer has standing on the money side at one instant.

    `lots` are ordered **closest anniversary first** — a reading order, not the
    consumption one (see `SavingStreakService.deposit_standing`).
    """

    customer_id: str
    outstanding_eur: Decimal
    lots: list[DepositLot]


@dataclass(frozen=True)
class ClaimResult:
    """What a claim did: the voucher, the price it was claimed at, the balance.

    `points_delta` is `0` and `replayed` is `True` when the idempotency key had
    already made this claim — the original voucher comes back and nothing is
    deducted a second time (spec D15).
    """

    customer_id: str
    item_id: str
    item_name: str
    price_points: int
    catalogue_version: int
    voucher_code: str
    claimed_at: datetime
    points_delta: int
    balance: int
    replayed: bool


def _euros(amount_eur: Decimal) -> str:
    """Format an amount for the customer's history line.

    Floored to the cent, never rounded up: the customer reads this line to
    check the points are right (spec user story 5), so a EUR 99.999 deposit
    that earned 99 points must not describe itself as EUR 100.00.
    """
    return f"€{amount_eur.quantize(Decimal('0.01'), rounding=ROUND_FLOOR):.2f}"


def _expiry_of(lot: PointsLot) -> str:
    """The history line for a points lot that ran out of time.

    It names the amount that died and the deposit day it came from; the
    movement's own stamp is the day it expired, so the customer reads both
    ends of the twelve months (user story 19).
    """
    return (
        f"Expired: {lot.remaining} points earned on {lot.earned_at.date().isoformat()}"
        f" reached {EXPIRY_MONTHS} months unspent"
    )


def _vesting_of(lot: DepositLot, points: int) -> str:
    """The history line for a deposit lot's anniversary bonus.

    It names the money the bonus was worked out on and the day that money
    landed, because those are the two things the customer cannot re-derive
    from the number: the movement's own stamp is the anniversary, and the
    amount standing is what a withdrawal may since have changed (spec D25). A
    support agent reading the line back has the whole sum in front of them.
    """
    rate = f"{(BONUS_RATE * 100).normalize():f}%"  # the rate as told: `10%`
    return (
        f"Loyalty bonus: {points} points, {rate} of the"
        f" {_euros(lot.outstanding_eur)} still standing from the deposit on"
        f" {lot.deposited_at.date().isoformat()}"
    )


def _start_of(business_date: date) -> datetime:
    """Midnight opening `business_date` in Brussels (spec D5)."""
    return in_brussels(datetime.combine(business_date, time()))


def _deposit_lot_of(amount_eur: Decimal, account_id: str) -> str:
    """The money-side line for a deposit lot opening."""
    return f"Deposit lot of {_euros(amount_eur)} into account {account_id}"


def _withdrawal_of(cents: int, account_id: str) -> str:
    """The money-side line for a withdrawal consuming deposit lots.

    It names the amount that left the account, in the same cents the ledger
    consumes by — one rounding, one number, so a support agent reading the line
    back cannot find it a cent away from what the lots actually lost (user
    story 37).

    It says nothing about how much of the withdrawal found a deposit lot,
    because that is not a fact about the withdrawal: it depends on what was
    standing, and a reversal arriving later changes it. A conclusion that can
    be overturned does not belong in an append-only entry; it is re-derived on
    every read of the position.
    """
    return f"Withdrawal of {_euros(euros(cents))} from account {account_id}"


def _lot_reversal_of(lot: OpenedLot | None) -> str:
    """The money-side line for a reversed deposit's lot never having stood.

    It is written from what the deposit-lot ledger knows about the lot, never
    from what the points ledger said about the same deposit: the two ledgers
    of spec D6 touch at one point and this is not it.

    It names the whole lot the deposit opened, not the part of it a withdrawal
    had left alone. A deposit that never really happened never happened at all
    (spec D11), so there is no "what was left" to report — and a line saying
    there was nothing left would tell a support agent the opposite of what the
    position now shows.

    With no lot to name, the line says only what this ledger knows: it is
    holding none for that deposit. Core banking's feed is unordered, so that
    covers two different stories — a deposit that opened no lot because it was
    not an external credit (spec D3), and a reversal that overtook its own
    deposit, whose lot this entry then suppresses when it does arrive. Saying
    the deposit "opened no deposit lot" would be a claim about the deposit,
    and in the second story it is a false one.
    """
    if lot is None:
        return "Reversed: no deposit lot for that deposit had reached this ledger"
    return (
        f"Reversed: the deposit lot of {_euros(lot.amount_eur)} opened on"
        f" {lot.deposited_at.date().isoformat()} never stood"
    )


def _reversal_of(deposit_description: str | None) -> str:
    """The history line for a clawback.

    It quotes what the deposit said about itself, so the customer reads the
    money that bounced rather than the identifier core banking files it under.
    """
    if deposit_description is None:
        return "Reversed: a deposit that earned no points"
    return f"Reversed: {deposit_description}"


class SavingStreakService:
    """The seam. Hand it a ledger and a clock; send it events; read balances."""

    def __init__(
        self,
        ledger: PointsLedger,
        clock: Clock,
        claims: ClaimRecords,
        voucher_issuer: VoucherIssuer,
        deposit_ledger: DepositLedger,
        catalogue: Catalogue | None = None,
    ) -> None:
        self._ledger = ledger
        #: The other ledger of spec D6. Injected separately and kept separate:
        #: the seam is the only thing that knows both exist.
        self._deposit_ledger = deposit_ledger
        self._clock = clock
        self._claims = claims
        self._voucher_issuer = voucher_issuer
        #: The catalogue version customers are shopping from. Injected so a
        #: test can publish a price change and watch history stay put (D12).
        self._catalogue = catalogue if catalogue is not None else current_catalogue()

    # ------------------------------------------------------------- commands --

    def handle(self, event: MoneyDeposited | MoneyWithdrawn | DepositReversed) -> EventResult:
        """Consume one core-banking event (spec D1)."""
        if isinstance(event, MoneyDeposited):
            return self._on_money_deposited(event)
        if isinstance(event, MoneyWithdrawn):
            return self._on_money_withdrawn(event)
        if isinstance(event, DepositReversed):
            return self._on_deposit_reversed(event)
        raise DomainError(f"unknown event {type(event).__name__}")

    def _on_money_deposited(self, event: MoneyDeposited) -> EventResult:
        """Credit base points immediately, and open the deposit lot.

        The one event that writes to both ledgers of spec D6, and it writes to
        each on that ledger's own terms:

        - the points side credits base points immediately and unconditionally
          (spec D9), floored to whole euros (spec D4);
        - the money side opens a **deposit lot** for the same money, carrying
          its own recurring twelve-month anniversary clock from today (spec
          D22). Nothing pays out from it yet; the lot is what the loyalty-rate
          bonus will vest against, and what a withdrawal will consume.

        Unless the money is not an external, customer-initiated credit, in
        which case it earns nothing and opens nothing (spec D3). A lot that
        could vest a bonus on an interest credit or an internal transfer would
        print points by exactly the route D3 closes.

        A deposit earns once and opens one lot, and only if it still stands.
        Core banking's feed is at-least-once and unordered, so the same
        `MoneyDeposited` can arrive twice, and it can arrive *after* the
        reversal that cancels it. Crediting a redelivery would invent points
        and leave the clawback of spec D11 with two credits to undo; crediting
        a deposit already marked reversed would leave points behind from money
        that never really arrived, which is the one thing D11 forbids. The
        same holds lot for lot on the money side, which is why each ledger
        answers that question out of its own entries rather than out of the
        other's: a deposit under a euro earns no point and so leaves no points
        movement to recognise a redelivery by, and it still opens exactly one
        deposit lot.
        """
        if not event.source.earns_points:
            return self._result(event.customer_id, 0)
        points = base_points_for(event.amount_eur)
        cents = cents_landing(event.amount_eur)
        if not points and not cents:
            return self._result(event.customer_id, 0)
        credited = 0
        with self._ledger.atomically(), self._deposit_ledger.atomically():
            if points and not self._deposit_already_settled(event.customer_id, event.deposit_id):
                self._ledger.append(
                    customer_id=event.customer_id,
                    occurred_at=self._clock.now(),
                    points=points,
                    reason=MovementReason.DEPOSIT,
                    description=(
                        f"Deposit of {_euros(event.amount_eur)} into account {event.account_id}"
                    ),
                    deposit_id=event.deposit_id,
                )
                credited = points
            if cents and not self._lot_already_settled(event.customer_id, event.deposit_id):
                self._deposit_ledger.append(
                    customer_id=event.customer_id,
                    occurred_at=self._clock.now(),
                    amount_cents=cents,
                    reason=DepositEntryReason.DEPOSIT,
                    description=_deposit_lot_of(event.amount_eur, event.account_id),
                    deposit_id=event.deposit_id,
                )
        return self._result(event.customer_id, credited)

    def _on_money_withdrawn(self, event: MoneyWithdrawn) -> EventResult:
        """Consume deposit lots, oldest first. No points move (spec D10).

        "No conditions attached" means earning has no conditions: a withdrawal
        never costs a base point, and this handler never opens the points
        ledger at all. What it does touch is the other ledger of spec D6.

        The money comes off the **oldest deposit lot first** (spec D8), the
        same ordering rule the points side spends by. A lot the withdrawal
        only partially covers is **split**: part of it goes and the rest stays
        standing under its original deposit date, so it keeps the anniversary
        it always had and vests on schedule (spec D25). Whole-lot forfeiture is
        rejected — it would punish a €1 withdrawal exactly as hard as a €100
        one. The split needs no special case here: the replay drains lots in
        order, and draining part of one *is* the split.

        A withdrawal larger than everything outstanding takes what there is and
        no more. Saving Streak does not own the money (spec D1), so an account
        can hold euros that never arrived as a deposit lot — money that landed
        before the customer enrolled, or as interest, which earns nothing (spec
        D3). The excess consumes nothing, no lot ever goes negative, and the
        shortfall is not carried against whatever is deposited next.

        What the entry records is the amount core banking says left the
        account, not the part of it that happened to find a lot at the instant
        this handler ran. Which lots it drained is a conclusion drawn afresh on
        every read, because a later event can change it: a deposit reversed
        after this withdrawal already crossed it never stood, and this
        withdrawal has to reach the lot behind it instead. An append-only
        ledger records the fact; the arithmetic is derived.

        Exactly once, however many times core banking delivers it: a
        redelivered withdrawal consumes nothing a second time.
        """
        with self._deposit_ledger.atomically():
            if self._deposit_ledger.has_entry_for(
                event.customer_id,
                DepositEntryReason.WITHDRAWAL,
                withdrawal_id=event.withdrawal_id,
            ):
                return self._result(event.customer_id, 0)
            leaving = cents_leaving(event.amount_eur)
            self._deposit_ledger.append(
                customer_id=event.customer_id,
                occurred_at=self._clock.now(),
                amount_cents=-leaving,
                reason=DepositEntryReason.WITHDRAWAL,
                description=_withdrawal_of(leaving, event.account_id),
                withdrawal_id=event.withdrawal_id,
            )
        return self._result(event.customer_id, 0)

    def _on_deposit_reversed(self, event: DepositReversed) -> EventResult:
        """Claw back exactly the points the deposit credited, once (spec D11).

        A deposit that never really happened must not leave points behind —
        and must not take more than it gave either. However many times core
        banking delivers the reversal, one deposit is clawed back one time.

        The clawback is recorded even when there is nothing to take: a
        reversal that arrives before its own deposit, or one for a deposit
        that earned nothing, still says "this deposit is cancelled", and that
        entry is what stops a late `MoneyDeposited` crediting points for money
        that bounced. An append-only ledger records the fact, not only the
        arithmetic.

        The clawback is not clamped: it is appended and summed like any other
        movement, so a balance the customer has already spent down goes
        negative and is read back negative.

        The money side is undone in the same breath and by the same rule: the
        deposit lot the deposit opened never stood, so a bounced transfer can
        vest no bonus. The *whole* lot goes, not the part of it no withdrawal
        had reached yet — money that never arrived cannot have been withdrawn,
        so a withdrawal that appeared to take some of this lot really took it
        from the lots behind, and the position says so the next time it is
        read. That is the replay's job (`DepositLedger.position`); what this
        writes is the fact that the deposit is cancelled. Each ledger decides
        for itself whether it has already handled this reversal, so neither is
        waiting on the other to know.
        """
        reverted_points = 0
        with self._ledger.atomically(), self._deposit_ledger.atomically():
            if not self._already_handled(
                event.customer_id, event.deposit_id, MovementReason.DEPOSIT_REVERSAL
            ):
                credit = self._ledger.credit_for_deposit(event.customer_id, event.deposit_id)
                self._ledger.append(
                    customer_id=event.customer_id,
                    occurred_at=self._clock.now(),
                    points=-credit.points,
                    reason=MovementReason.DEPOSIT_REVERSAL,
                    description=_reversal_of(credit.description),
                    deposit_id=event.deposit_id,
                )
                reverted_points = credit.points
            if not self._deposit_ledger.has_entry_for(
                event.customer_id,
                DepositEntryReason.DEPOSIT_REVERSAL,
                deposit_id=event.deposit_id,
            ):
                lot = self._deposit_ledger.lot_opened_by(event.customer_id, event.deposit_id)
                self._deposit_ledger.append(
                    customer_id=event.customer_id,
                    occurred_at=self._clock.now(),
                    amount_cents=-(lot.amount_cents if lot is not None else 0),
                    reason=DepositEntryReason.DEPOSIT_REVERSAL,
                    description=_lot_reversal_of(lot),
                    deposit_id=event.deposit_id,
                )
        return self._result(event.customer_id, -reverted_points)

    def claim(self, command: ClaimReward) -> ClaimResult:
        """Spend points on one catalogue item and issue the voucher.

        Instant and final: there is no reversal path, which is why every part
        of this either happens or none of it does.

        - The key comes first (spec D15), before the catalogue is even read. A
          replayed key returns the claim it already made — the original
          voucher, nothing deducted — so a flaky connection never costs the
          customer 100 points, and delisting an item in a later catalogue
          version cannot break the replay of a claim already made for it.
        - A balance that has gone negative from a reversed deposit cannot
          claim at all until it recovers (spec D11), and a balance too small
          for the item is refused in full: no partial redemption, no paying
          the difference, no negative balance (spec D14). The balance it is
          judged against excludes points that have already expired, whether or
          not the sweep has run (spec D18), and the points it spends come off
          the oldest lot first (spec D8).
        - The voucher is issued before anything is written, and issuance
          raising rolls the whole claim back (spec D16). The points are not
          deducted and the claim fails as a unit.
        - The price the item cost *today* is written into the claim, so a
          later catalogue version never rewrites it (spec D12).

        There is no stock limit and no per-customer or per-period claim limit
        (spec D13): the same item can be claimed as often as the balance
        allows, each claim under its own key.
        """
        with self._ledger.atomically():
            already = self._claims.find(command.customer_id, command.idempotency_key)
            if already is not None:
                if already.item_id != command.item_id:
                    raise DomainError(
                        f"idempotency key {command.idempotency_key!r} already claimed"
                        f" {already.item_name}: a second item needs its own key"
                    )
                return self._replay_of(already)
            item = self._catalogue.item(command.item_id)
            self._refuse_unless_affordable(command.customer_id, item)
            voucher_code = self._issue(command, item)
            claimed_at = self._clock.now()
            self._ledger.append(
                customer_id=command.customer_id,
                occurred_at=claimed_at,
                points=-item.price_points,
                reason=MovementReason.CLAIM,
                description=f"Claimed {item.name} for {item.price_points} points",
            )
            self._claims.record(
                ClaimRecord(
                    customer_id=command.customer_id,
                    idempotency_key=command.idempotency_key,
                    item_id=item.item_id,
                    item_name=item.name,
                    price_points=item.price_points,
                    catalogue_version=self._catalogue.version,
                    voucher_code=voucher_code,
                    claimed_at=claimed_at,
                )
            )
        return ClaimResult(
            customer_id=command.customer_id,
            item_id=item.item_id,
            item_name=item.name,
            price_points=item.price_points,
            catalogue_version=self._catalogue.version,
            voucher_code=voucher_code,
            claimed_at=claimed_at,
            points_delta=-item.price_points,
            balance=self._ledger.balance(command.customer_id, self._clock.now()),
            replayed=False,
        )

    def run_daily_sweep(self, business_date: date | None = None) -> SweepResult:
        """Vest the anniversaries and materialise the expiries of one business date.

        A plain function at the seam (spec D43): scheduling is infrastructure,
        and a test calls this directly with a pinned date rather than waiting
        for 03:00 to come round. Omit the date and the injected clock supplies
        today's (spec D5).

        Two passes, and **vesting comes first** (spec D21), so a bonus vesting
        tonight is never swept the same night. They are not the same kind of
        work. Vesting is the sweep *deciding*: a deposit lot reaching an
        anniversary mints a points lot, and until this runs those points do not
        exist. Expiry decides nothing — it takes effect the moment the clock
        passes it (spec D18), so a lot is already out of the balance and
        already in the history before this runs, and what the sweep adds is the
        permanent ledger entry that says so, stamped with the instant the lot
        died rather than the moment the batch got round to it.

        Both are re-runnable in every sense the bank cares about (user story
        38): running the same business date twice writes nothing the second
        time, and running a night that was missed writes exactly what that
        night would have written, so catching two dates up in order lands on
        the state each night would have left (spec D20). Expiry is keyed by the
        lot it writes off and vesting by the deposit lot and the anniversary it
        pays, so neither depends on the sweep's own history of runs.

        It can only be asked for a night that has happened. A business date in
        the future is refused: what a lot will be worth on a night that has not
        come depends on what the customer spends between now and then, so
        materialising it would freeze an amount that is not settled yet — and a
        lot is written off exactly once (spec D20), so no later night could
        ever correct it. The ledger would say 200 points expired where 100 did.
        The bonus has the same reason of its own: what a lot is worth on an
        anniversary depends on what the customer withdraws before it, and a
        bonus paid early could not be taken back either. Tonight and any night
        missed behind it are the only real questions, and both are answered
        here.
        """
        today = self._clock.today()
        on = business_date if business_date is not None else today
        if on > today:
            raise DomainError(
                f"the sweep cannot run for {on.isoformat()}: that night has not happened"
                f" yet, and tonight is {today.isoformat()}"
            )
        swept_at = in_brussels(datetime.combine(on, time(hour=SWEEP_HOUR)))
        #: The furthest the sweep may look: the night it was asked for, and
        #: never past the present (spec D5, D18). The second bound is the same
        #: rule inside a single day — run before 03:00 and tonight's stamp is
        #: still a couple of hours away, and a lot that dies in between is
        #: still the customer's to spend until it does.
        horizon = min(swept_at, self._clock.now())
        lots_expired = points_expired = 0
        with self._ledger.atomically(), self._deposit_ledger.atomically():
            # Spec D21: vesting runs *before* anything expires, so a bonus
            # vesting tonight is never swept the same night.
            vested, points_vested, customers = self._vest_anniversaries(on, horizon)
            for customer_id in self._ledger.customers_with_unmaterialised_lots():
                written = self._ledger.materialised_lot_ids(customer_id)
                for lot in self._ledger.position(customer_id, horizon).expired_lots:
                    if lot.lot_id in written:
                        continue
                    self._ledger.append(
                        customer_id=customer_id,
                        occurred_at=lot.expires_at,
                        points=-lot.remaining,
                        reason=MovementReason.EXPIRY,
                        description=_expiry_of(lot),
                        lot_id=lot.lot_id,
                    )
                    lots_expired += 1
                    points_expired += lot.remaining
                    customers.add(customer_id)
        return SweepResult(
            business_date=on,
            swept_at=swept_at,
            anniversaries_vested=vested,
            points_vested=points_vested,
            lots_expired=lots_expired,
            points_expired=points_expired,
            customers_affected=len(customers),
        )

    # -------------------------------------------------------------- queries --

    def catalogue(self) -> Catalogue:
        """The catalogue customers are shopping from, with its version."""
        return self._catalogue

    def claims(self, customer_id: str) -> list[ClaimRecord]:
        """Every claim this customer made, newest first, as it was made."""
        return self._claims.for_customer(identifier(customer_id, "customer_id"))

    def balance(self, customer_id: str, as_of: datetime | None = None) -> int:
        """The customer's spendable points balance, derived from the ledger (D7).

        One balance per customer, across every savings account they hold (D2),
        and never a point the customer could not actually spend: a lot past its
        twelfth month is out of this number from the instant it expires, sweep
        or no sweep (spec D18, user story 19).

        `as_of` is the instant to read it at, defaulting to the injected clock.
        It is what lets the demo ask "and what will this be next March?" without
        a scheduler and without waiting.
        """
        return self._ledger.balance(identifier(customer_id, "customer_id"), self._at(as_of))

    def deposit_standing(
        self, customer_id: str, as_of: datetime | None = None
    ) -> DepositStanding:
        """Everything the customer has standing on the money side, in one read.

        The whole money-side answer — the total and the lots it is made of —
        settled by a single replay of the deposit-lot ledger. A caller wanting
        both (the demo's deposit-lot page wants exactly both) asks once rather
        than replaying an append-only ledger twice for two halves of the same
        answer, which would also let the two halves disagree if a writer landed
        in between.

        `as_of` is the instant to read at, defaulting to the injected clock
        (spec D5).
        """
        position = self._deposit_ledger.position(
            identifier(customer_id, "customer_id"), self._at(as_of)
        )
        return DepositStanding(
            customer_id=customer_id,
            outstanding_eur=euros(position.outstanding_cents),
            lots=sorted(position.lots, key=lambda lot: (lot.next_anniversary, lot.deposited_at)),
        )

    def deposit_lots(self, customer_id: str, as_of: datetime | None = None) -> list[DepositLot]:
        """The deposit lots still standing, closest anniversary first.

        What the customer needs to decide which money to take out (user story
        26): how much of each deposit is still there, and when each one next
        comes round. Nothing pays out from them yet — this is the structure the
        loyalty-rate bonus of a later ticket vests against, made visible.

        The order here is a *reading* order, not the consumption one. Lots are
        consumed oldest first (spec D8) and the ledger hands them back in that
        order; the customer is asking a different question — what is nearest to
        vesting — and because every lot runs its own clock from its own deposit
        day, the two orders genuinely differ. A lot deposited last June comes
        round after one deposited this March. Ties fall back to the deposit
        date, so two lots sharing an anniversary read in the order a withdrawal
        would take them.

        `as_of` is the instant to read at, defaulting to the injected clock
        (spec D5); it is what makes "next anniversary" mean next.
        """
        return self.deposit_standing(customer_id, as_of).lots

    def outstanding_eur(self, customer_id: str, as_of: datetime | None = None) -> Decimal:
        """Everything the customer still has standing across their deposit lots."""
        return self.deposit_standing(customer_id, as_of).outstanding_eur

    def history(self, customer_id: str, as_of: datetime | None = None) -> list[PointsMovement]:
        """Every points movement with its reason, newest first.

        Including expiries the sweep has not written down yet. A customer whose
        balance dropped last night must be able to read why this morning, and
        the batch job is not the thing that made it drop (spec D18). The
        movement the sweep eventually writes is the one already shown here —
        same amount, same instant, same words — so a night's sweep changes
        nothing the customer can see.
        """
        customer_id = identifier(customer_id, "customer_id")
        moment = self._at(as_of)
        written = self._ledger.movements(customer_id, moment)
        pending = self._pending_expiries(customer_id, moment)
        if not pending:
            return written
        # `written` is already newest-first and the sort is stable, so an entry
        # that shares an instant with an expiry keeps the order it was read in.
        return sorted([*written, *pending], key=lambda m: m.occurred_at, reverse=True)

    # --------------------------------------------------------------- private -

    def _at(self, as_of: datetime | None) -> datetime:
        """The instant to read at: the caller's, or the injected clock's (D5).

        Every read on the seam funnels through here, which is what makes this
        the place to refuse an instant the system's calendar cannot answer at
        (`clock.readable_instant`). A read is the one thing a caller can ask
        for at an arbitrary instant, and a twelve-month clock asked about a
        date past the end of the calendar cannot answer: refusing it here is
        one refusal, in one place, so balance, history and deposit lots all
        answer a far-future `as_of` the same way instead of two of them
        answering and the third crashing.
        """
        moment = as_of if as_of is not None else self._clock.now()
        return readable_instant(moment, "as_of" if as_of is not None else "the clock")

    def _vest_anniversaries(
        self, business_date: date, horizon: datetime
    ) -> tuple[int, int, set[str]]:
        """Pay every deposit lot that has reached an anniversary (spec D22).

        The single crossing of spec D6, and it crosses one way only: the money
        side is **read** and the points side is written. A vesting lot mints an
        ordinary points lot dated the anniversary, with its own fresh twelve
        months from that date (spec D19) — spendable, and later giftable, like
        any other. No deposit lot is consumed, so the customer's money is
        exactly where it was; the bonus is minted, not moved.

        What it pays is 10% of the base points of the portion **still
        standing** (spec D22), which is what the deposit-lot replay already
        works out: a withdrawal that only partly covered a lot left the rest
        of it standing under its original deposit date, so that lot keeps its
        anniversary and vests on the smaller amount (spec D25). A lot nothing
        survives on is not in the position at all and vests nothing.

        The rate is taken on the lot's standing money every year, never on
        base-plus-accrued, so it does not compound: year three on an untouched
        €100 pays 10 points, not 12 (spec D23).

        Idempotent per anniversary rather than per night (spec D20). The pair
        of deposit lot and anniversary is the key, read off the vesting entries
        already written, so running a night twice pays once and a night that
        was missed pays exactly what it owed when it is caught up.

        `business_date` is what decides which anniversaries are due, not
        `horizon`: an anniversary is a date, not an instant, so a lot deposited
        at 03:30 vests in the 03:00 sweep on its anniversary rather than
        waiting a further day. The two agree anyway — the horizon is the
        sweep's own 03:00 stamp, or the present if the sweep is running before
        it, and both fall on the business date — and `horizon` still does the
        job it does for expiry: it is the instant the money side is read at,
        so the sweep never reads a position into the future (spec D5, D18).

        The bonus is therefore stamped at the **start** of the anniversary
        date, not at the hour the deposit happened to land. The sweep must
        never write an entry stamped after the horizon it is running to: the
        balance is a replay of every entry on record, and a lot dated this
        afternoon would retire this morning's points this morning. Starting
        the day is also what makes spec D21 mean something — a year's bonus
        expires at the start of the day the next year's vests, in that order,
        which is the steady state of one year's bonus in hand.
        """
        vested_count = points_vested = 0
        customers: set[str] = set()
        for customer_id in self._deposit_ledger.customers_with_lots():
            vested = self._ledger.vested_anniversaries(customer_id)
            for lot in self._deposit_ledger.position(customer_id, horizon).lots:
                points = bonus_points_for(lot.outstanding_eur)
                if not points:
                    continue
                for anniversary in anniversaries_through(lot.deposited_at, business_date):
                    if (lot.deposit_id, anniversary) in vested:
                        continue
                    self._ledger.append(
                        customer_id=customer_id,
                        occurred_at=_start_of(anniversary),
                        points=points,
                        reason=MovementReason.VESTING,
                        description=_vesting_of(lot, points),
                        deposit_id=lot.deposit_id,
                    )
                    vested_count += 1
                    points_vested += points
                    customers.add(customer_id)
        return vested_count, points_vested, customers

    def _pending_expiries(self, customer_id: str, as_of: datetime) -> list[PointsMovement]:
        """The expiries that have happened but have not been written down.

        Newest lot first, because that is the order the same expiries read in
        once the sweep has written them down: two lots that die in the same
        instant are separated by the ledger's own insertion order, newest
        above oldest. Handing them back oldest-first would make the history
        re-order itself the night the sweep ran — the same rows, the same
        amounts and the same balance, but not the same page (user story 5).
        """
        written = self._ledger.materialised_lot_ids(customer_id)
        return [
            PointsMovement(
                occurred_at=lot.expires_at,
                points=-lot.remaining,
                reason=MovementReason.EXPIRY,
                description=_expiry_of(lot),
            )
            for lot in reversed(self._ledger.position(customer_id, as_of).expired_lots)
            if lot.lot_id not in written
        ]

    def _refuse_unless_affordable(self, customer_id: str, item: CatalogueItem) -> None:
        """Refuse in full, or say nothing and let the claim proceed.

        Read inside the ledger's transaction, so the balance this decides on
        is the balance the deduction lands on: two claims arriving at once
        cannot both find the same points and both spend them. It is the
        spendable balance, so a claim can never be paid for with points that
        have already expired, however long ago the sweep last ran (spec D18).
        """
        balance = self._ledger.balance(customer_id, self._clock.now())
        if balance < 0:
            raise DomainError(
                f"claiming is blocked while the balance is negative:"
                f" it is {balance} points after a reversed deposit"
            )
        if balance < item.price_points:
            raise DomainError(
                f"{item.name} costs {item.price_points} points"
                f" and the balance is {balance}: the claim is refused in full"
            )

    def _issue(self, command: ClaimReward, item: CatalogueItem) -> str:
        """Ask the outbound port for a voucher (spec D16).

        Anything the port raises becomes `VoucherIssuanceFailed` and leaves
        the surrounding transaction to roll back: the points are not deducted
        and the claim fails as a unit. A port that returns without a code has
        not issued anything either, whatever it thinks.
        """
        request = IssuanceRequest(
            customer_id=command.customer_id,
            item_id=item.item_id,
            item_name=item.name,
            price_points=item.price_points,
            idempotency_key=command.idempotency_key,
        )
        try:
            voucher = self._voucher_issuer.issue(request)
        except VoucherIssuanceFailed:
            raise
        except Exception as exc:  # the port is somebody else's code
            raise VoucherIssuanceFailed(f"the voucher issuer failed: {exc}") from exc
        if voucher is None or not voucher.code:
            raise VoucherIssuanceFailed("the voucher issuer returned no voucher code")
        return voucher.code

    def _replay_of(self, claim: ClaimRecord) -> ClaimResult:
        """The claim this key already made: same voucher, nothing deducted."""
        return ClaimResult(
            customer_id=claim.customer_id,
            item_id=claim.item_id,
            item_name=claim.item_name,
            price_points=claim.price_points,
            catalogue_version=claim.catalogue_version,
            voucher_code=claim.voucher_code,
            claimed_at=claim.claimed_at,
            points_delta=0,
            balance=self._ledger.balance(claim.customer_id, self._clock.now()),
            replayed=True,
        )

    def _already_handled(self, customer_id: str, deposit_id: str, reason: MovementReason) -> bool:
        """Has this deposit already been credited, or already been cancelled?"""
        return self._ledger.has_movement_for_deposit(customer_id, deposit_id, reason)

    def _deposit_already_settled(self, customer_id: str, deposit_id: str) -> bool:
        """Has the points side already credited this deposit, or cancelled it?"""
        return self._already_handled(
            customer_id, deposit_id, MovementReason.DEPOSIT
        ) or self._already_handled(customer_id, deposit_id, MovementReason.DEPOSIT_REVERSAL)

    def _lot_already_settled(self, customer_id: str, deposit_id: str) -> bool:
        """Has the money side already opened this deposit's lot, or cancelled it?

        Asked of the deposit-lot ledger alone (spec D6). The points side's
        answer would be a different question wearing the same words: a deposit
        under one euro credits nothing and leaves no points movement behind,
        and it still has a lot.
        """
        return self._deposit_ledger.has_entry_for(
            customer_id, DepositEntryReason.DEPOSIT, deposit_id=deposit_id
        ) or self._deposit_ledger.has_entry_for(
            customer_id, DepositEntryReason.DEPOSIT_REVERSAL, deposit_id=deposit_id
        )

    def _result(self, customer_id: str, points_delta: int) -> EventResult:
        return EventResult(
            customer_id=customer_id,
            points_delta=points_delta,
            balance=self._ledger.balance(customer_id, self._clock.now()),
        )
