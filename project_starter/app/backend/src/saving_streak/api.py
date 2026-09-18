"""HTTP adapter.

Routes translate HTTP into calls on the application-service seam (spec D42)
and back. No domain rule belongs in this module: it parses a request, hands an
event or a query to `SavingStreakService`, and serialises what comes back.
"""

from __future__ import annotations

import math
import sqlite3
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from . import __version__, demo
from .banking import DemoBanking
from .catalogue import Catalogue, CatalogueItem
from .claims import ClaimRecord, ClaimRecords
from .clock import SystemClock
from .db import connection
from .deposits import DepositLedger, DepositLot
from .events import (
    ClaimReward,
    DepositReversed,
    DepositSource,
    DomainError,
    MoneyDeposited,
    MoneyWithdrawn,
    MovementReason,
)
from .ledger import PointsLedger, PointsMovement
from .migrations import migrate
from .service import ClaimResult, EventResult, SavingStreakService, SweepResult
from .vouchers import LocalVoucherIssuer, VoucherIssuanceFailed


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Migrate once, before the first request, never per request."""
    migrate()
    yield


def get_service() -> Iterator[SavingStreakService]:
    """One service per request, over the configured SQLite file and real time.

    Overridden in tests, which inject a pinned clock (spec D5/T2).
    """
    with connection() as conn:
        yield SavingStreakService(
            ledger=PointsLedger(conn),
            clock=SystemClock(),
            # One connection, so a claim and the points it spends are written
            # inside the single transaction the ledger opens — and so are the
            # points lot and the deposit lot one deposit opens across the two
            # ledgers of spec D6.
            claims=ClaimRecords(conn),
            voucher_issuer=LocalVoucherIssuer(),
            deposit_ledger=DepositLedger(conn),
        )


#: The seam, injected into every route. Nothing else reaches the domain.
Service = Annotated[SavingStreakService, Depends(get_service)]


# ------------------------------------------------------------- wire format --


class DepositRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    deposit_id: str = Field(min_length=1)
    amount_eur: Decimal = Field(gt=0, description="Euros; floored to whole euros (spec D4)")
    source: DepositSource = DepositSource.EXTERNAL


class DemoLoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=254)


class TransferRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    transfer_id: str = Field(min_length=1, max_length=200)
    direction: Literal["deposit", "withdrawal"]
    amount_eur: Decimal = Field(gt=0, max_digits=12, decimal_places=2)


class WithdrawalRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    account_id: str = Field(min_length=1)
    withdrawal_id: str = Field(min_length=1)
    amount_eur: Decimal = Field(gt=0)


class DepositReversalRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    deposit_id: str = Field(min_length=1)


class EventResponse(BaseModel):
    customer_id: str
    points_delta: int
    balance: int

    @classmethod
    def of(cls, result: EventResult) -> EventResponse:
        return cls(
            customer_id=result.customer_id,
            points_delta=result.points_delta,
            balance=result.balance,
        )


class BalanceResponse(BaseModel):
    customer_id: str
    balance: int


#: The instant to read a balance or a history at. A bare date is read as
#: midnight in Europe/Brussels (spec D5). It is how the demo asks the seam what
#: the customer will hold next March without waiting for next March; every
#: decision about what has expired by then is the seam's (spec D18).
AsOf = Annotated[
    datetime | None,
    Query(description="Read the answer at this instant; defaults to now (spec D5)"),
]


class MovementResponse(BaseModel):
    occurred_at: datetime
    points: int
    reason: MovementReason
    description: str

    @classmethod
    def of(cls, movement: PointsMovement) -> MovementResponse:
        return cls(
            occurred_at=movement.occurred_at,
            points=movement.points,
            reason=movement.reason,
            description=movement.description,
        )


class HistoryResponse(BaseModel):
    customer_id: str
    movements: list[MovementResponse]


class DepositLotResponse(BaseModel):
    """One deposit lot still standing, and when it next comes round."""

    deposit_id: str
    deposited_at: datetime
    amount_eur: Decimal
    outstanding_eur: Decimal
    next_anniversary: datetime

    @classmethod
    def of(cls, lot: DepositLot) -> DepositLotResponse:
        return cls(
            deposit_id=lot.deposit_id,
            deposited_at=lot.deposited_at,
            amount_eur=lot.amount_eur,
            outstanding_eur=lot.outstanding_eur,
            next_anniversary=lot.next_anniversary,
        )


class DepositLotsResponse(BaseModel):
    customer_id: str
    outstanding_eur: Decimal
    lots: list[DepositLotResponse]


class CatalogueItemResponse(BaseModel):
    item_id: str
    name: str
    price_points: int

    @classmethod
    def of(cls, item: CatalogueItem) -> CatalogueItemResponse:
        return cls(item_id=item.item_id, name=item.name, price_points=item.price_points)


class CatalogueResponse(BaseModel):
    version: int
    items: list[CatalogueItemResponse]

    @classmethod
    def of(cls, catalogue: Catalogue) -> CatalogueResponse:
        return cls(
            version=catalogue.version,
            items=[CatalogueItemResponse.of(item) for item in catalogue.items],
        )


class ClaimRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    item_id: str = Field(min_length=1)
    idempotency_key: str = Field(
        min_length=1,
        description="Caller-supplied; replaying it returns the original voucher (spec D15)",
    )


class ClaimResponse(BaseModel):
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

    @classmethod
    def of(cls, result: ClaimResult) -> ClaimResponse:
        return cls(
            customer_id=result.customer_id,
            item_id=result.item_id,
            item_name=result.item_name,
            price_points=result.price_points,
            catalogue_version=result.catalogue_version,
            voucher_code=result.voucher_code,
            claimed_at=result.claimed_at,
            points_delta=result.points_delta,
            balance=result.balance,
            replayed=result.replayed,
        )


class ClaimSummary(BaseModel):
    item_id: str
    item_name: str
    price_points: int
    catalogue_version: int
    voucher_code: str
    claimed_at: datetime

    @classmethod
    def of(cls, claim: ClaimRecord) -> ClaimSummary:
        return cls(
            item_id=claim.item_id,
            item_name=claim.item_name,
            price_points=claim.price_points,
            catalogue_version=claim.catalogue_version,
            voucher_code=claim.voucher_code,
            claimed_at=claim.claimed_at,
        )


class ClaimsResponse(BaseModel):
    customer_id: str
    claims: list[ClaimSummary]


class SweepRequest(BaseModel):
    business_date: date | None = Field(
        default=None,
        description="The night to run; defaults to today on the injected clock (spec D43)",
    )


class SweepResponse(BaseModel):
    business_date: date
    swept_at: datetime
    lots_vested: int
    points_vested: int
    lots_expired: int
    points_expired: int
    customers_affected: int

    @classmethod
    def of(cls, result: SweepResult) -> SweepResponse:
        return cls(
            business_date=result.business_date,
            swept_at=result.swept_at,
            lots_vested=result.lots_vested,
            points_vested=result.points_vested,
            lots_expired=result.lots_expired,
            points_expired=result.points_expired,
            customers_affected=result.customers_affected,
        )


# -------------------------------------------------------------------- app ---


def _refusals(exc: RequestValidationError) -> list[dict[str, object]]:
    """Why the wire format refused the request, in a body that can be sent.

    Field, message and the offending value — the same three things the default
    handler reports, but rendered defensively: the input is whatever the JSON
    parser produced, which for `1e400` is `inf`, and `json.dumps` refuses that.
    """
    return [
        {
            "loc": [str(part) for part in error.get("loc", ())],
            "msg": str(error.get("msg", "")),
            "input": _json_safe(error.get("input")),
        }
        for error in exc.errors()
    ]


def _json_safe(value: object) -> object:
    """Whatever JSON can carry, unchanged; everything else as its repr."""
    if isinstance(value, bool) or value is None or isinstance(value, int | str):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else repr(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_json_safe(item) for item in value]
    return repr(value)


def create_app() -> FastAPI:
    app = FastAPI(title="Saving Streak", version=__version__, lifespan=lifespan)

    # The demo UI is served by Vite on another port during development.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5273", "http://127.0.0.1:5273"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(DomainError)
    def _domain_error(_request: Request, exc: DomainError) -> JSONResponse:
        """A rule the domain refused is a client error, not a crash."""
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(RequestValidationError)
    def _malformed_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """A request the wire format refused, rendered so it can be sent.

        The default handler echoes the offending input back, and a JSON number
        too large for a float — core banking posting `1e400` — parses to
        `inf`, which is not JSON and cannot be serialised. Refusing an amount
        must not itself become a 500.
        """
        return JSONResponse(status_code=422, content={"detail": _refusals(exc)})

    @app.exception_handler(sqlite3.OperationalError)
    def _ledger_busy(_request: Request, exc: sqlite3.OperationalError) -> JSONResponse:
        """The ledger was held too long by another writer.

        Nothing was decided and nothing was written, so this is "come back",
        not "no". An at-least-once feed redelivering is exactly the right
        response, and a 503 says so where a 500 would not.

        It names no ledger, because there are two (spec D6) and this reaches
        both: a withdrawal never opens the points ledger, and a lock timeout
        on the deposit-lot ledger arrives here just the same.
        """
        return JSONResponse(status_code=503, content={"detail": f"the ledger is busy: {exc}"})

    @app.exception_handler(VoucherIssuanceFailed)
    def _issuance_failed(_request: Request, exc: VoucherIssuanceFailed) -> JSONResponse:
        """The supplier said no, so the claim failed as a unit (spec D16).

        Nothing was deducted and no voucher exists. It is not the customer's
        request that is wrong, so it is not a 400: the upstream the seam
        depends on is unavailable, and the same claim may be sent again under
        the same idempotency key.
        """
        return JSONResponse(status_code=502, content={"detail": f"voucher issuance failed: {exc}"})

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """Liveness probe. The lab waits on this before handing over."""
        return {"status": "ok", "version": __version__}

    @app.get("/api/demo/customers")
    def demo_customers() -> list[dict[str, str]]:
        return demo.directory()

    @app.post("/api/demo/session")
    def demo_login(body: DemoLoginRequest) -> dict[str, str]:
        with connection() as conn:
            return demo.open_demo(body.email, conn, SystemClock().now())

    @app.get("/api/demo/customers/{customer_id}/accounts")
    def demo_accounts(customer_id: str) -> dict:
        with connection() as conn:
            return DemoBanking(conn, SystemClock()).accounts(customer_id)

    @app.post("/api/demo/transfers")
    def demo_transfer(body: TransferRequest) -> dict:
        with connection() as conn:
            return DemoBanking(conn, SystemClock()).transfer(**body.model_dump())

    @app.post("/api/events/money-deposited", response_model=EventResponse)
    def money_deposited(body: DepositRequest, service: Service) -> EventResponse:
        """Core banking says money landed (spec D1)."""
        return EventResponse.of(
            service.handle(
                MoneyDeposited(
                    customer_id=body.customer_id,
                    account_id=body.account_id,
                    deposit_id=body.deposit_id,
                    amount_eur=body.amount_eur,
                    source=body.source,
                )
            )
        )

    @app.post("/api/events/money-withdrawn", response_model=EventResponse)
    def money_withdrawn(body: WithdrawalRequest, service: Service) -> EventResponse:
        """Core banking says money left (spec D1)."""
        return EventResponse.of(
            service.handle(
                MoneyWithdrawn(
                    customer_id=body.customer_id,
                    account_id=body.account_id,
                    withdrawal_id=body.withdrawal_id,
                    amount_eur=body.amount_eur,
                )
            )
        )

    @app.post("/api/events/deposit-reversed", response_model=EventResponse)
    def deposit_reversed(body: DepositReversalRequest, service: Service) -> EventResponse:
        """Core banking says a deposit never really happened (spec D1/D11)."""
        return EventResponse.of(
            service.handle(
                DepositReversed(customer_id=body.customer_id, deposit_id=body.deposit_id)
            )
        )

    @app.get("/api/customers/{customer_id}/points/balance", response_model=BalanceResponse)
    def points_balance(customer_id: str, service: Service, as_of: AsOf = None) -> BalanceResponse:
        """One balance per customer, across every account (spec D2).

        Never counting points that have expired by `as_of`, sweep or no sweep.
        """
        return BalanceResponse(customer_id=customer_id, balance=service.balance(customer_id, as_of))

    @app.get("/api/customers/{customer_id}/points/history", response_model=HistoryResponse)
    def points_history(customer_id: str, service: Service, as_of: AsOf = None) -> HistoryResponse:
        """Every points movement with its reason, newest first.

        Including an expiry the sweep has not written down yet: the points went
        the moment the clock passed them, so the reason goes with them.
        """
        return HistoryResponse(
            customer_id=customer_id,
            movements=[MovementResponse.of(m) for m in service.history(customer_id, as_of)],
        )

    @app.get("/api/customers/{customer_id}/deposit-lots", response_model=DepositLotsResponse)
    def deposit_lots(customer_id: str, service: Service, as_of: AsOf = None) -> DepositLotsResponse:
        """The deposit lots still standing, closest anniversary first (D6/D22).

        The money side of spec D6, which no points movement ever touches. The
        seam decides the order and the anniversaries; this route asks once and
        serialises what comes back.
        """
        standing = service.deposit_standing(customer_id, as_of)
        return DepositLotsResponse(
            customer_id=standing.customer_id,
            outstanding_eur=standing.outstanding_eur,
            lots=[DepositLotResponse.of(lot) for lot in standing.lots],
        )

    @app.get("/api/catalogue", response_model=CatalogueResponse)
    def catalogue(service: Service) -> CatalogueResponse:
        """The reward catalogue and the version it was published as (spec D12)."""
        return CatalogueResponse.of(service.catalogue())

    @app.post("/api/claims", response_model=ClaimResponse)
    def claim_reward(body: ClaimRequest, service: Service) -> ClaimResponse:
        """Spend points on a catalogue item. Instant, final, idempotency-keyed."""
        return ClaimResponse.of(
            service.claim(
                ClaimReward(
                    customer_id=body.customer_id,
                    item_id=body.item_id,
                    idempotency_key=body.idempotency_key,
                )
            )
        )

    @app.post("/api/sweeps/daily", response_model=SweepResponse)
    def run_daily_sweep(body: SweepRequest, service: Service) -> SweepResponse:
        """Run the nightly sweep for one business date (spec D20/D43).

        Scheduling is infrastructure and lives outside this app; the sweep
        itself is a plain call on the seam, which is what makes it callable
        with a pinned date and safely re-runnable.
        """
        return SweepResponse.of(service.run_daily_sweep(body.business_date))

    @app.get("/api/customers/{customer_id}/claims", response_model=ClaimsResponse)
    def customer_claims(customer_id: str, service: Service) -> ClaimsResponse:
        """Every voucher this customer has been issued, newest first."""
        return ClaimsResponse(
            customer_id=customer_id,
            claims=[ClaimSummary.of(claim) for claim in service.claims(customer_id)],
        )

    return app


app = create_app()
