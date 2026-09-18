"""The HTTP adapter in front of the seam.

These tests prove the seam is correctly surfaced over HTTP — the shape the
demo UI reads (spec D45/T9). The domain assertions live in
`test_earning_points.py` and `test_claiming_rewards.py`, at the seam, where
spec T1 puts them.
"""

from __future__ import annotations

from datetime import timedelta

import pytest


def post_claim(client, **body):
    payload = {
        "customer_id": "cust-alice",
        "item_id": "cinema-ticket",
        "idempotency_key": "key-1",
    }
    payload.update(body)
    return client.post("/api/claims", json=payload)


def post_deposit(client, **body):
    payload = {
        "customer_id": "cust-alice",
        "account_id": "acc-1",
        "deposit_id": "dep-1",
        "amount_eur": "10.00",
    }
    payload.update(body)
    return client.post("/api/events/money-deposited", json=payload)


def test_a_deposit_over_http_credits_points(client):
    response = post_deposit(client, amount_eur="10.99")

    assert response.status_code == 200
    assert response.json() == {"customer_id": "cust-alice", "points_delta": 10, "balance": 10}


def test_the_balance_endpoint_reports_the_derived_balance(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="10.99")
    post_deposit(client, deposit_id="dep-2", account_id="acc-2", amount_eur="40")

    response = client.get("/api/customers/cust-alice/points/balance")

    assert response.status_code == 200
    assert response.json() == {"customer_id": "cust-alice", "balance": 50}


def test_the_history_endpoint_lists_every_movement_newest_first(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="10")
    post_deposit(client, deposit_id="dep-2", amount_eur="40")
    client.post(
        "/api/events/deposit-reversed",
        json={"customer_id": "cust-alice", "deposit_id": "dep-1"},
    )

    movements = client.get("/api/customers/cust-alice/points/history").json()["movements"]

    assert [(m["points"], m["reason"]) for m in movements] == [
        (-10, "deposit_reversal"),
        (40, "deposit"),
        (10, "deposit"),
    ]
    assert all(m["description"] for m in movements)
    assert all(m["occurred_at"] for m in movements)


def test_a_withdrawal_over_http_costs_no_points(client):
    post_deposit(client, amount_eur="100")

    response = client.post(
        "/api/events/money-withdrawn",
        json={
            "customer_id": "cust-alice",
            "account_id": "acc-1",
            "withdrawal_id": "wd-1",
            "amount_eur": "80",
        },
    )

    assert response.json() == {"customer_id": "cust-alice", "points_delta": 0, "balance": 100}


def test_a_negative_balance_is_served_as_negative(client):
    """Nothing between the ledger and the wire clamps the balance at zero.

    Every step is a real request: EUR 45 deposited, 40 points spent on a real
    claim, and then the EUR 30 deposit bounces, which takes the customer below
    zero over HTTP. That is what the demo UI has to render as negative.
    """
    post_deposit(client, deposit_id="dep-1", amount_eur="30")
    post_deposit(client, deposit_id="dep-2", amount_eur="15")
    post_claim(client, item_id="coffee-or-snack-voucher")

    client.post(
        "/api/events/deposit-reversed",
        json={"customer_id": "cust-alice", "deposit_id": "dep-1"},
    )

    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == -25


def test_a_redelivered_reversal_over_http_claws_back_once(client):
    post_deposit(client, amount_eur="30")
    deltas = [
        client.post(
            "/api/events/deposit-reversed",
            json={"customer_id": "cust-alice", "deposit_id": "dep-1"},
        ).json()["points_delta"]
        for _ in range(4)
    ]

    assert deltas == [-30, 0, 0, 0]
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 0


def test_an_unknown_customer_reads_as_empty_rather_than_missing(client):
    assert client.get("/api/customers/cust-nobody/points/balance").json()["balance"] == 0
    assert client.get("/api/customers/cust-nobody/points/history").json()["movements"] == []


def test_a_non_earning_source_is_accepted_and_credits_nothing(client):
    response = post_deposit(client, amount_eur="500", source="interest")

    assert response.json()["points_delta"] == 0


def test_a_non_positive_deposit_is_refused(client):
    assert post_deposit(client, amount_eur="0").status_code == 422
    assert post_deposit(client, amount_eur="-5").status_code == 422


def test_an_absurd_amount_is_refused_cleanly_rather_than_crashing(client):
    """An amount the demo UI's free-text box accepts must not 500 the seam."""
    response = post_deposit(client, amount_eur="1e19")

    assert response.status_code == 400
    assert response.json()["detail"]
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 0


def test_the_history_line_over_http_agrees_with_the_points(client):
    post_deposit(client, amount_eur="99.999")

    (movement,) = client.get("/api/customers/cust-alice/points/history").json()["movements"]

    assert movement["points"] == 99
    assert "€99.99" in movement["description"]
    assert "€100" not in movement["description"]


def test_a_json_number_too_large_for_a_float_is_refused_cleanly(client):
    """Core banking posts JSON, and JSON has no ceiling on a number.

    `1e400` parses to `inf`, which no JSON body can carry back out. Refusing
    an amount must not itself become a 500 — attempt-1 and attempt-2 review
    both sent this class of bug back.
    """
    for literal in ("1e400", "-1e400", "1e400000"):
        response = client.post(
            "/api/events/money-deposited",
            content=(
                '{"customer_id":"cust-alice","account_id":"acc-1",'
                f'"deposit_id":"dep-1","amount_eur":{literal}}}'
            ),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 422, literal
        assert response.json()["detail"], literal

    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 0


def test_a_reversal_that_arrives_before_its_deposit_leaves_nothing_behind_over_http(client):
    reversal = client.post(
        "/api/events/deposit-reversed",
        json={"customer_id": "cust-alice", "deposit_id": "dep-1"},
    )
    late = post_deposit(client, amount_eur="30")

    assert (reversal.json()["points_delta"], late.json()["points_delta"]) == (0, 0)
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 0


@pytest.mark.parametrize("amount", ["1e19", "1E+5000"])
def test_an_oversized_withdrawal_is_refused_cleanly_over_http(client, amount):
    """A refusal, never a 500 — the same answer the deposit route gives.

    A withdrawal's amount now reaches the deposit-lot ledger, so an amount no
    row could hold is a refused event. What must never happen is the arithmetic
    raising something that is not a `DomainError` and escaping the 400 mapping
    as a stack trace, which an at-least-once feed would then replay forever.
    """
    response = client.post(
        "/api/events/money-withdrawn",
        json={
            "customer_id": "cust-alice",
            "account_id": "acc-1",
            "withdrawal_id": "wd-1",
            "amount_eur": amount,
        },
    )

    assert response.status_code == 400
    assert "above the 1000000000 EUR this system accepts" in response.json()["detail"]


def test_a_withdrawal_larger_than_everything_standing_is_accepted_over_http(client):
    """Anything the ledger can hold is taken as sent, however large (spec D1)."""
    post_deposit(client, deposit_id="dep-1", amount_eur="100")

    response = client.post(
        "/api/events/money-withdrawn",
        json={
            "customer_id": "cust-alice",
            "account_id": "acc-1",
            "withdrawal_id": "wd-1",
            "amount_eur": "1000000000",
        },
    )

    assert response.status_code == 200
    assert response.json()["points_delta"] == 0
    body = client.get("/api/customers/cust-alice/deposit-lots").json()
    assert (body["outstanding_eur"], body["lots"]) == ("0.00", [])


# ------------------------------------------------- the catalogue and claims --


def test_the_catalogue_endpoint_lists_the_rewards_and_the_version(client):
    body = client.get("/api/catalogue").json()

    assert body["version"] >= 1
    assert [(item["item_id"], item["price_points"]) for item in body["items"]] == [
        ("cinema-ticket", 100),
        ("coffee-or-snack-voucher", 40),
        ("charity-donation", 10),
        ("family-cinema-pack", 180),
    ]
    assert all(item["name"] for item in body["items"])


def test_a_claim_over_http_issues_a_voucher_and_deducts_the_price(client):
    post_deposit(client, amount_eur="150")

    response = post_claim(client)

    assert response.status_code == 200
    body = response.json()
    assert body["item_name"] == "Cinema ticket"
    assert body["price_points"] == 100
    assert body["points_delta"] == -100
    assert body["balance"] == 50
    assert body["voucher_code"]
    assert body["replayed"] is False
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 50


def test_a_claim_over_http_shows_up_in_the_history(client):
    post_deposit(client, amount_eur="100")
    post_claim(client)

    newest, *_ = client.get("/api/customers/cust-alice/points/history").json()["movements"]

    assert (newest["points"], newest["reason"]) == (-100, "claim")
    assert "Cinema ticket" in newest["description"]


def test_the_claims_endpoint_lists_the_vouchers_issued(client):
    post_deposit(client, amount_eur="140")
    first = post_claim(client, item_id="cinema-ticket", idempotency_key="key-1").json()
    second = post_claim(client, item_id="coffee-or-snack-voucher", idempotency_key="key-2").json()

    body = client.get("/api/customers/cust-alice/claims").json()

    assert [c["item_id"] for c in body["claims"]] == [
        "coffee-or-snack-voucher",
        "cinema-ticket",
    ]
    assert [c["voucher_code"] for c in body["claims"]] == [
        second["voucher_code"],
        first["voucher_code"],
    ]
    assert [c["price_points"] for c in body["claims"]] == [40, 100]


def test_a_replayed_claim_over_http_returns_the_original_voucher(client):
    post_deposit(client, amount_eur="150")
    first = post_claim(client).json()

    replays = [post_claim(client).json() for _ in range(3)]

    assert [r["voucher_code"] for r in replays] == [first["voucher_code"]] * 3
    assert [r["points_delta"] for r in replays] == [0, 0, 0]
    assert all(r["replayed"] for r in replays)
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 50
    assert len(client.get("/api/customers/cust-alice/claims").json()["claims"]) == 1


def test_an_unaffordable_claim_over_http_is_refused_with_a_reason(client):
    post_deposit(client, amount_eur="99")

    response = post_claim(client)

    assert response.status_code == 400
    assert "100" in response.json()["detail"]
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 99
    assert client.get("/api/customers/cust-alice/claims").json()["claims"] == []


def test_a_claim_on_a_negative_balance_over_http_is_refused(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="40")
    post_claim(client, item_id="coffee-or-snack-voucher")
    client.post(
        "/api/events/deposit-reversed",
        json={"customer_id": "cust-alice", "deposit_id": "dep-1"},
    )

    response = post_claim(client, item_id="charity-donation", idempotency_key="key-2")

    assert response.status_code == 400
    assert "negative" in response.json()["detail"]
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == -40


def test_an_unknown_catalogue_item_over_http_is_refused(client):
    post_deposit(client, amount_eur="500")

    response = post_claim(client, item_id="a-pony")

    assert response.status_code == 400
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 500


def test_a_claim_without_an_idempotency_key_is_refused(client):
    post_deposit(client, amount_eur="150")

    assert post_claim(client, idempotency_key="").status_code == 422
    assert (
        client.post(
            "/api/claims", json={"customer_id": "cust-alice", "item_id": "cinema-ticket"}
        ).status_code
        == 422
    )
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 150


def test_a_failed_issuance_over_http_deducts_nothing(client, voucher_issuer):
    """Spec D16, at the wire: the claim fails as a unit and nothing is spent."""
    post_deposit(client, amount_eur="150")
    voucher_issuer.failing = True

    response = post_claim(client)

    assert response.status_code == 502
    assert response.json()["detail"]
    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 150
    assert client.get("/api/customers/cust-alice/claims").json()["claims"] == []


# ----------------------------------------------- expiry and the daily sweep --

#: Twelve months and a day on from the pinned clock, as a bare date. The wire
#: reads a bare date as midnight in Europe/Brussels (spec D5).
AFTER_TWELVE_MONTHS = "2027-03-15"


def test_the_balance_endpoint_reads_at_the_instant_it_is_given(client):
    """The UI asks "and what will this be then?"; the seam answers (spec D18)."""
    post_deposit(client, deposit_id="dep-1", amount_eur="10")

    now = client.get("/api/customers/cust-alice/points/balance")
    later = client.get(
        "/api/customers/cust-alice/points/balance", params={"as_of": AFTER_TWELVE_MONTHS}
    )

    assert now.json()["balance"] == 10
    assert later.json()["balance"] == 0


def test_the_history_endpoint_shows_an_expiry_before_the_sweep_has_run(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="10")

    movements = client.get(
        "/api/customers/cust-alice/points/history", params={"as_of": AFTER_TWELVE_MONTHS}
    ).json()["movements"]

    assert [(m["points"], m["reason"]) for m in movements] == [(-10, "expiry"), (10, "deposit")]
    assert "10 points" in movements[0]["description"]


def test_the_sweep_endpoint_runs_the_business_date_it_is_given(client, clock):
    post_deposit(client, deposit_id="dep-1", amount_eur="10")
    clock.advance(timedelta(days=367))

    response = client.post("/api/sweeps/daily", json={"business_date": AFTER_TWELVE_MONTHS})

    assert response.status_code == 200
    body = response.json()
    assert body["business_date"] == AFTER_TWELVE_MONTHS
    assert (body["lots_expired"], body["points_expired"], body["customers_affected"]) == (1, 10, 1)


def test_the_sweep_endpoint_refuses_a_night_that_has_not_happened(client):
    """A domain refusal, rendered as a 400 like any other (spec D18)."""
    post_deposit(client, deposit_id="dep-1", amount_eur="10")

    response = client.post("/api/sweeps/daily", json={"business_date": AFTER_TWELVE_MONTHS})

    assert response.status_code == 400
    assert AFTER_TWELVE_MONTHS in response.json()["detail"]
    history = client.get(
        "/api/customers/cust-alice/points/history", params={"as_of": AFTER_TWELVE_MONTHS}
    ).json()["movements"]
    assert [(m["points"], m["reason"]) for m in history] == [(-10, "expiry"), (10, "deposit")]


def test_the_sweep_endpoint_is_re_runnable(client, clock):
    post_deposit(client, deposit_id="dep-1", amount_eur="10")
    clock.advance(timedelta(days=367))
    client.post("/api/sweeps/daily", json={"business_date": AFTER_TWELVE_MONTHS})

    again = client.post("/api/sweeps/daily", json={"business_date": AFTER_TWELVE_MONTHS}).json()
    history = client.get(
        "/api/customers/cust-alice/points/history", params={"as_of": AFTER_TWELVE_MONTHS}
    ).json()["movements"]

    assert (again["lots_expired"], again["points_expired"]) == (0, 0)
    assert (again["anniversaries_vested"], again["points_vested"]) == (0, 0)
    # The anniversary the sweep passed vested its loyalty bonus (spec D22),
    # dated the start of that day and so reading under the expiry later on it.
    assert [(m["points"], m["reason"]) for m in history] == [
        (-10, "expiry"),
        (1, "vesting"),
        (10, "deposit"),
    ]


def test_the_sweep_endpoint_defaults_to_today_on_the_injected_clock(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="10")

    body = client.post("/api/sweeps/daily", json={}).json()

    assert body["business_date"] == "2026-03-14"
    assert body["lots_expired"] == 0


# ----------------------------------------------------------- deposit lots ---


def post_withdrawal(client, **body):
    payload = {
        "customer_id": "cust-alice",
        "account_id": "acc-1",
        "withdrawal_id": "wd-1",
        "amount_eur": "30.00",
    }
    payload.update(body)
    return client.post("/api/events/money-withdrawn", json=payload)


def lots(client, customer_id="cust-alice", **params):
    return client.get(f"/api/customers/{customer_id}/deposit-lots", params=params).json()


def test_the_deposit_lots_endpoint_lists_what_is_still_standing(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="100")

    body = lots(client)

    assert body["customer_id"] == "cust-alice"
    assert body["outstanding_eur"] == "100.00"
    (lot,) = body["lots"]
    assert (lot["amount_eur"], lot["outstanding_eur"]) == ("100.00", "100.00")
    assert lot["next_anniversary"].startswith("2027-03-14")


def test_a_withdrawal_over_http_splits_the_lot_it_only_partly_covers(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="100")

    post_withdrawal(client, amount_eur="30")

    (lot,) = lots(client)["lots"]
    assert (lot["amount_eur"], lot["outstanding_eur"]) == ("100.00", "70.00")
    # The surviving portion keeps the anniversary the deposit gave it.
    assert lot["next_anniversary"].startswith("2027-03-14")


def test_the_deposit_lots_endpoint_reads_at_the_instant_it_is_given(client):
    post_deposit(client, deposit_id="dep-1", amount_eur="100")

    now = lots(client)["lots"][0]["next_anniversary"]
    later = lots(client, as_of=AFTER_TWELVE_MONTHS)["lots"][0]["next_anniversary"]

    assert now.startswith("2027-03-14")
    assert later.startswith("2028-03-14")


#: The latest date the demo's `<input type="date">` will offer, and so the
#: latest one a customer can put in front of the seam.
FAR_FUTURE = "9999-12-31"

#: The last date the seam reads at (`clock.LAST_READABLE_INSTANT`).
LAST_READABLE_DAY = "9998-12-31"


@pytest.mark.parametrize("as_of", [FAR_FUTURE, "9999-01-01", "9999-12-31T23:59:59+00:00"])
def test_a_far_future_as_of_is_refused_cleanly_rather_than_crashing(client, as_of):
    """A date past the end of the calendar is a 400, never a 500.

    A deposit lot's anniversary clock recurs, so it can be asked for a date no
    `datetime` holds. The refusal has to arrive as the domain's own words —
    which is what the demo puts on the page — and not as a crash.
    """
    post_deposit(client, deposit_id="dep-1", amount_eur="100")

    response = client.get("/api/customers/cust-alice/deposit-lots", params={"as_of": as_of})

    assert response.status_code == 400
    assert LAST_READABLE_DAY in response.json()["detail"]


def test_one_instant_gets_one_answer_from_every_read_the_demo_makes(client):
    """The demo reads all of these at one `as_of` and shows them under one caption.

    A page cannot have two of them answering and the third crashing, so the
    refusal is the seam's and every read gets it.
    """
    post_deposit(client, deposit_id="dep-1", amount_eur="100")
    paths = [
        "/api/customers/cust-alice/points/balance",
        "/api/customers/cust-alice/points/history",
        "/api/customers/cust-alice/deposit-lots",
    ]

    at_the_end = [client.get(p, params={"as_of": FAR_FUTURE}).status_code for p in paths]
    inside_it = [client.get(p, params={"as_of": LAST_READABLE_DAY}).status_code for p in paths]

    assert at_the_end == [400, 400, 400]
    assert inside_it == [200, 200, 200]


def test_the_last_readable_day_still_reports_a_next_anniversary(client):
    """The refusal begins past the end of the calendar, not short of it."""
    post_deposit(client, deposit_id="dep-1", amount_eur="100")

    (lot,) = lots(client, as_of=LAST_READABLE_DAY)["lots"]

    assert lot["outstanding_eur"] == "100.00"
    assert lot["next_anniversary"].startswith("9999-03-14")


def test_an_unknown_customer_has_no_deposit_lots_rather_than_a_missing_page(client):
    body = lots(client, customer_id="nobody")

    assert body == {"customer_id": "nobody", "outstanding_eur": "0.00", "lots": []}


def test_a_claim_over_http_leaves_the_deposit_lots_alone(client):
    """Spec D6 over the wire: two ledgers, and a claim reaches only one."""
    post_deposit(client, deposit_id="dep-1", amount_eur="100")
    before = lots(client)

    assert post_claim(client).json()["points_delta"] == -100

    assert client.get("/api/customers/cust-alice/points/balance").json()["balance"] == 0
    assert lots(client) == before
