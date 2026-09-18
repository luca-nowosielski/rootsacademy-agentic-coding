"""The domain events Saving Streak consumes, the commands it accepts, and the
vocabulary around them.

Core banking owns the money and is the system of record (spec D1). It emits
`MoneyDeposited`, `MoneyWithdrawn` and `DepositReversed`; Saving Streak
consumes them at the single application-service seam (spec D42) and owns
points. The customer's own intentions arrive at that same seam as commands —
`ClaimReward` today, `Gift` in a later ticket.

Nothing here reads a clock or touches storage: these are plain facts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum


class DomainError(ValueError):
    """A command or event the domain refuses. The HTTP adapter maps it to 400."""


class DepositSource(str, Enum):
    """Why money landed in the account.

    Only external, customer-initiated credits earn points (spec D3). Interest,
    internal transfers between the customer's own accounts and refunds earn
    nothing — otherwise shuffling money between your own accounts prints points.
    """

    EXTERNAL = "external"
    INTEREST = "interest"
    INTERNAL_TRANSFER = "internal_transfer"
    REFUND = "refund"

    @property
    def earns_points(self) -> bool:
        return self is DepositSource.EXTERNAL


class MovementReason(str, Enum):
    """Why a points movement happened. Every ledger entry carries one.

    The points ledger holds points lots (earned, vested) and consumption
    entries (claimed, gifted, expired, clawed back) — spec D6. Five of those
    reasons exist so far; the rest arrive with the tickets that own them, and a
    reason is never reused to stand in for another, because a ledger entry is
    what the customer and a support agent read back as fact.
    """

    DEPOSIT = "deposit"
    DEPOSIT_REVERSAL = "deposit_reversal"
    #: A redemption against the catalogue.
    CLAIM = "claim"
    #: A points lot that reached twelve months with points still on it. The
    #: entry is written by the nightly sweep, but the points are gone from the
    #: balance the moment the clock passes, sweep or no sweep (spec D18).
    EXPIRY = "expiry"
    #: The loyalty-rate bonus: a deposit lot reaching an anniversary with money
    #: still standing on it mints an ordinary points lot for 10% of that
    #: money's base points (spec D19/D22). This is the one reason on this side
    #: that a deposit lot causes, and the only crossing between the two ledgers
    #: of spec D6 — it still mints a *points* entry, in this ledger's own
    #: vocabulary, rather than borrowing the money side's.
    VESTING = "vesting"


class DepositEntryReason(str, Enum):
    """Why a deposit-lot entry happened. Every money-side entry carries one.

    The deposit-lot ledger is the *other* ledger of spec D6, and this enum is
    deliberately not `MovementReason`: the two ledgers touch at exactly one
    point — a vesting deposit lot minting a points lot, which is written on the
    points side as `MovementReason.VESTING` and leaves no entry here at all —
    and sharing a vocabulary of reasons between them would make it look as
    though a claim could reach a deposit lot or a withdrawal could reach the
    balance. They cannot.

    A positive entry opens a **deposit lot**; a negative one consumes deposit
    lots, oldest first (spec D8).
    """

    #: Money landed and became a deposit lot with its own anniversary clock.
    DEPOSIT = "deposit"
    #: Money left, consuming deposit lots oldest first and splitting the lot it
    #: only partially covers (spec D25).
    WITHDRAWAL = "withdrawal"
    #: A deposit that never really happened, so its lot stops standing (D11).
    DEPOSIT_REVERSAL = "deposit_reversal"


#: The largest amount Saving Streak will carry into a ledger. A retail savings
#: movement above a billion euros is a bad event, not a windfall: it is refused
#: cleanly rather than being carried into a ledger, where an unbounded Python
#: int would not fit a 64-bit SQLite INTEGER and would crash the seam instead
#: of returning an answer. The ceiling exists for that reason and no other, so
#: it guards exactly the amounts that reach a ledger — both of them (spec D6).
MAX_AMOUNT_EUR = Decimal("1000000000")


def _positive_amount(amount: Decimal | int | str, what: str) -> Decimal:
    """A finite amount above zero, or the domain refuses the event."""
    try:
        value = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    except ArithmeticError as exc:
        raise DomainError(f"{what} must be an amount in euros, got {amount!r}") from exc
    if not value.is_finite() or value <= 0:
        raise DomainError(f"{what} must be a positive amount in euros, got {amount!r}")
    return value


def _ledgered_amount(amount: Decimal | int | str, what: str) -> Decimal:
    """A positive amount that will also fit the ledger it reaches (spec D4).

    Both ledgers of spec D6 are behind this: a deposit becomes points on one
    side and cents on the other, and a withdrawal becomes cents. An amount no
    ledger row could hold is refused at the edge of the domain rather than
    part-way through a write.
    """
    value = _positive_amount(amount, what)
    if value > MAX_AMOUNT_EUR:
        raise DomainError(
            f"{what} of {value} EUR is above the {MAX_AMOUNT_EUR} EUR this system accepts"
        )
    return value


def identifier(value: str, what: str) -> str:
    """A non-empty identifier, trimmed, or the domain refuses the command.

    Trimmed because the event or command *stores* what this returns: validating
    on the trimmed value and keeping the raw one would make `"k1"` and `" k1"`
    two different idempotency keys, and a second key is a second voucher for a
    claim that can never be undone (spec D15).

    One rule for every identifier in this module, because they are matched
    against each other across events and commands. A `customer_id` trimmed on
    the way in to a claim but not on the way in to a deposit would be two
    customers: the money would earn points for one of them and the claim would
    be refused for the other.
    """
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{what} must be given")
    return value.strip()


@dataclass(frozen=True)
class MoneyDeposited:
    """Money landed in one of the customer's savings accounts."""

    customer_id: str
    account_id: str
    deposit_id: str
    amount_eur: Decimal
    source: DepositSource = DepositSource.EXTERNAL

    def __post_init__(self) -> None:
        for name in ("customer_id", "account_id", "deposit_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        object.__setattr__(self, "amount_eur", _ledgered_amount(self.amount_eur, "deposit"))


@dataclass(frozen=True)
class MoneyWithdrawn:
    """Money left one of the customer's savings accounts.

    Withdrawals never cost base points (spec D10); only the loyalty-rate bonus
    of a later ticket is at risk.

    The same ceiling as a deposit applies, and for the same single reason. A
    withdrawal used to move no points and reach no ledger, so there was nothing
    for an oversized number to overflow; now it consumes deposit lots, its
    amount is written to the deposit-lot ledger in cents, and an amount no row
    could hold has to be refused at the edge of the domain. Refusing it is the
    only clean answer available: the alternative is quietly recording a
    different number than core banking sent, and a ledger that rewrites the
    fact it records is worse than one that says no.
    """

    customer_id: str
    account_id: str
    withdrawal_id: str
    amount_eur: Decimal

    def __post_init__(self) -> None:
        for name in ("customer_id", "account_id", "withdrawal_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
        object.__setattr__(self, "amount_eur", _ledgered_amount(self.amount_eur, "withdrawal"))


@dataclass(frozen=True)
class DepositReversed:
    """A deposit never really happened — a bounced transfer, say.

    It must not leave points behind (spec D11): exactly what it credited is
    clawed back, and the balance may go negative.
    """

    customer_id: str
    deposit_id: str

    def __post_init__(self) -> None:
        for name in ("customer_id", "deposit_id"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))


@dataclass(frozen=True)
class ClaimReward:
    """The customer spends points on one catalogue item.

    Instant and final: the voucher is issued immediately and there is no
    reversal path. The `idempotency_key` is the caller's (spec D15), and is
    what makes a double submission return the original voucher rather than
    issue a second one — non-negotiable given that issuance is irreversible.
    """

    customer_id: str
    item_id: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for name in ("customer_id", "item_id", "idempotency_key"):
            object.__setattr__(self, name, identifier(getattr(self, name), name))
