"""Сценарии API против настоящего PostgreSQL."""

import asyncio
import json
import uuid

from app.security import sign
from tests.conftest import count

SECRET = "test-secret"


async def create(client, amount="1500.00", key=None, description="Курс Python"):
    return await client.post(
        "/payments",
        json={"amount": amount, "description": description},
        headers={"Idempotency-Key": key or uuid.uuid4().hex},
    )


async def webhook(client, payment_id, type_, event_id=None, secret=SECRET):
    body = json.dumps({"event_id": event_id or uuid.uuid4().hex, "type": type_, "payment_id": str(payment_id)}).encode()
    return await client.post(
        "/webhooks/bank", content=body, headers={"X-Signature": sign(body, secret), "Content-Type": "application/json"}
    )


async def paid(client, amount="1500.00"):
    p = (await create(client, amount)).json()
    assert (await webhook(client, p["id"], "payment.succeeded")).json()["result"] == "applied"
    return p["id"]


async def refund(client, pid, amount, key=None):
    return await client.post(
        f"/payments/{pid}/refunds", json={"amount": amount}, headers={"Idempotency-Key": key or uuid.uuid4().hex}
    )


# --- создание и идемпотентность ---


async def test_create_payment(client):
    r = await create(client, "1500.5")
    assert r.status_code == 201
    p = r.json()
    assert p["amount"] == "1500.50" and p["status"] == "pending" and p["refunded_amount"] == "0.00"
    assert (await client.get(f"/payments/{p['id']}")).json()["id"] == p["id"]


async def test_same_key_same_body_returns_same_payment(client):
    a = await create(client, key="order-42-attempt")
    b = await create(client, key="order-42-attempt")
    assert a.json()["id"] == b.json()["id"]
    assert await count("payments") == 1


async def test_same_key_other_body_is_conflict(client):
    await create(client, "1500.00", key="order-43-attempt")
    r = await create(client, "9999.00", key="order-43-attempt")
    assert r.status_code == 409
    assert await count("payments") == 1


async def test_parallel_requests_with_same_key_create_one_payment(client):
    """Клиент нажал «Оплатить» дважды или сеть повторила запрос — два запроса одновременно."""
    rs = await asyncio.gather(*[create(client, key="double-click-key") for _ in range(10)])
    assert {r.status_code for r in rs} <= {200, 201}
    assert len({r.json()["id"] for r in rs}) == 1
    assert await count("payments") == 1


async def test_validation(client):
    assert (await create(client, "10.005")).status_code == 422  # три знака после точки
    assert (await create(client, "0")).status_code == 422
    assert (await create(client, "-5")).status_code == 422
    r = await client.post("/payments", json={"amount": "100"})  # без Idempotency-Key
    assert r.status_code == 422
    assert (await client.get(f"/payments/{uuid.uuid4()}")).status_code == 404
    assert await count("payments") == 0


# --- вебхуки банка ---


async def test_webhook_bad_signature_rejected(client):
    p = (await create(client)).json()
    r = await webhook(client, p["id"], "payment.succeeded", secret="угаданный секрет")
    assert r.status_code == 401
    assert (await client.get(f"/payments/{p['id']}")).json()["status"] == "pending"


async def test_webhook_applies_once(client):
    p = (await create(client)).json()
    first = await webhook(client, p["id"], "payment.succeeded", event_id="evt-1")
    again = await webhook(client, p["id"], "payment.succeeded", event_id="evt-1")
    assert first.json() == {"result": "applied", "status": "succeeded"}
    assert again.json() == {"result": "duplicate"}
    assert await count("webhook_events") == 1


async def test_webhook_out_of_order_is_ignored(client):
    """«canceled» пришёл позже «succeeded» — деньги уже списаны, статус не откатываем."""
    pid = await paid(client)
    r = await webhook(client, pid, "payment.canceled")
    assert r.json()["result"] == "ignored"
    assert (await client.get(f"/payments/{pid}")).json()["status"] == "succeeded"


async def test_webhook_unknown_payment(client):
    assert (await webhook(client, uuid.uuid4(), "payment.succeeded")).status_code == 404


# --- возвраты ---


async def test_refund_before_payment_is_conflict(client):
    p = (await create(client)).json()
    assert (await refund(client, p["id"], "100.00")).status_code == 409


async def test_partial_then_full_refund(client):
    pid = await paid(client, "1500.00")
    r = await refund(client, pid, "500.00")
    assert r.json()["status"] == "succeeded" and r.json()["refunded_amount"] == "500.00"
    r = await refund(client, pid, "1000.00")
    assert r.json()["status"] == "refunded" and r.json()["refunded_amount"] == "1500.00"
    assert len(r.json()["refunds"]) == 2


async def test_refund_more_than_paid_rejected(client):
    pid = await paid(client, "1500.00")
    await refund(client, pid, "1000.00")
    r = await refund(client, pid, "600.00")
    assert r.status_code == 422
    assert (await client.get(f"/payments/{pid}")).json()["refunded_amount"] == "1000.00"


async def test_refund_retry_does_not_refund_twice(client):
    pid = await paid(client)
    await refund(client, pid, "500.00", key="refund-key-1")
    r = await refund(client, pid, "500.00", key="refund-key-1")
    assert r.json()["refunded_amount"] == "500.00"
    assert await count("refunds") == 1
    assert (await refund(client, pid, "700.00", key="refund-key-1")).status_code == 409


async def test_parallel_refunds_cannot_exceed_amount(client):
    """Пять возвратов по 1000 ₽ одновременно на платёж в 1500 ₽ — пройти может только один."""
    pid = await paid(client, "1500.00")
    rs = await asyncio.gather(*[refund(client, pid, "1000.00") for _ in range(5)])
    assert sorted(r.status_code for r in rs) == [200, 422, 422, 422, 422]
    assert (await client.get(f"/payments/{pid}")).json()["refunded_amount"] == "1000.00"
    assert await count("refunds") == 1
