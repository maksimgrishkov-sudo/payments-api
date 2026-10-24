"""Уведомления магазину: outbox в той же транзакции, отправка воркером с повторами (Celery + Redis).

HTTP магазина подменяем httpx.MockTransport — проверяем, что и как ушло, без сети.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select, update

from app import tasks
from app.models import Notification, NotificationStatus
from app.security import sign
from tests.conftest import count, engine
from tests.test_api import refund, webhook

SHOP = "https://shop.example/payments/callback"
CALLBACK_SECRET = "change-me-too"


async def create_with_callback(client, amount="1500.00"):
    r = await client.post(
        "/payments",
        json={"amount": amount, "callback_url": SHOP},
        headers={"Idempotency-Key": uuid.uuid4().hex},
    )
    assert r.status_code == 201
    return r.json()["id"]


async def only_notification() -> Notification:
    async with tasks.SessionLocal() as s:
        return (await s.execute(select(Notification))).scalar_one()


def shop(*codes):
    """Магазин, который по очереди отвечает этими кодами; запоминает, что пришло."""
    got, codes = [], list(codes)

    def handler(request: httpx.Request) -> httpx.Response:
        got.append(request)
        code = codes.pop(0) if codes else 200
        if code == "timeout":
            raise httpx.ConnectTimeout("магазин не отвечает")
        return httpx.Response(code)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler)), got


async def test_status_change_writes_outbox_and_enqueues(client, queued):
    pid = await create_with_callback(client)
    assert await count("notifications") == 0  # создание платежа — не событие для магазина
    await webhook(client, pid, "payment.succeeded")
    n = await only_notification()
    assert n.event == "payment.succeeded" and n.url == SHOP and n.status == NotificationStatus.pending
    assert n.payload == {
        "event": "payment.succeeded",
        "payment_id": pid,
        "status": "succeeded",
        "amount": "1500.00",
        "refunded_amount": "0.00",
    }
    assert queued == [str(n.id)]


async def test_no_callback_no_notification(client, queued):
    p = (
        await client.post("/payments", json={"amount": "100.00"}, headers={"Idempotency-Key": uuid.uuid4().hex})
    ).json()
    await webhook(client, p["id"], "payment.succeeded")
    assert await count("notifications") == 0 and queued == []


async def test_duplicate_and_ignored_events_do_not_notify(client, queued):
    pid = await create_with_callback(client)
    await webhook(client, pid, "payment.succeeded", event_id="evt-1")
    await webhook(client, pid, "payment.succeeded", event_id="evt-1")  # банк повторил
    await webhook(client, pid, "payment.canceled")  # против таблицы переходов
    assert await count("notifications") == 1 and len(queued) == 1


async def test_refund_notifies_once_even_if_repeated(client, queued):
    pid = await create_with_callback(client)
    await webhook(client, pid, "payment.succeeded")
    await refund(client, pid, "500.00", key="refund-key-1")
    await refund(client, pid, "500.00", key="refund-key-1")  # повтор запроса — возврата и уведомления нет
    async with tasks.SessionLocal() as s:
        events = (await s.execute(select(Notification.event).order_by(Notification.created_at))).scalars().all()
    assert events == ["payment.succeeded", "refund.succeeded"] and len(queued) == 2


async def test_delivery_signed_and_marked_sent(client):
    pid = await create_with_callback(client)
    await webhook(client, pid, "payment.succeeded")
    n = await only_notification()
    http, got = shop(200)
    assert await tasks.deliver_once(n.id, http) == "sent"
    req = got[0]
    assert json.loads(req.content)["status"] == "succeeded"
    assert req.headers["X-Signature"] == sign(req.content, CALLBACK_SECRET)  # магазин проверит так же
    assert req.headers["X-Notification-Id"] == str(n.id)  # по нему магазин отбросит повтор
    n = await only_notification()
    assert n.status == NotificationStatus.sent and n.attempts == 1 and n.sent_at is not None


async def test_shop_down_then_up_retries_with_backoff(client):
    pid = await create_with_callback(client)
    await webhook(client, pid, "payment.succeeded")
    n = await only_notification()
    http, got = shop(503, "timeout", 200)
    assert await tasks.deliver_once(n.id, http) == "retry"
    n = await only_notification()
    assert n.attempts == 1 and n.last_error == "HTTP 503"
    assert n.next_attempt_at > datetime.now(UTC) + timedelta(seconds=5)  # следующая попытка не сразу
    assert await tasks.deliver_once(n.id, http) == "retry"
    assert "ConnectTimeout" in (await only_notification()).last_error
    assert await tasks.deliver_once(n.id, http) == "sent"
    assert len(got) == 3


async def test_gives_up_after_max_attempts(client):
    pid = await create_with_callback(client)
    await webhook(client, pid, "payment.succeeded")
    n = await only_notification()
    http, got = shop(*[500] * tasks.MAX_ATTEMPTS)
    results = [await tasks.deliver_once(n.id, http) for _ in range(tasks.MAX_ATTEMPTS)]
    assert results == ["retry"] * (tasks.MAX_ATTEMPTS - 1) + ["failed"]
    assert await tasks.deliver_once(n.id, http) == "skip"  # после «failed» больше не стучимся
    assert len(got) == tasks.MAX_ATTEMPTS


async def test_sent_is_not_sent_again(client):
    pid = await create_with_callback(client)
    await webhook(client, pid, "payment.succeeded")
    n = await only_notification()
    http, got = shop(200, 200)
    await tasks.deliver_once(n.id, http)
    assert await tasks.deliver_once(n.id, http) == "skip" and len(got) == 1


async def test_two_workers_same_notification_one_request(client):
    """Повтор Celery и sweep() взяли одну строку одновременно: магазин получает одно уведомление."""
    pid = await create_with_callback(client)
    await webhook(client, pid, "payment.succeeded")
    n = await only_notification()
    got = []

    async def slow(request):
        got.append(request)
        await asyncio.sleep(0.3)  # пока первый воркер ждёт магазин, строка заблокирована
        return httpx.Response(200)

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    results = await asyncio.gather(tasks.deliver_once(n.id, http), tasks.deliver_once(n.id, http))
    assert sorted(results) == ["sent", "skip"] and len(got) == 1


async def test_sweep_finds_only_overdue(client):
    """Redis потерял задачу — строка висит в pending дольше срока; свежие и отправленные не трогаем."""
    for _ in range(3):
        pid = await create_with_callback(client)
        await webhook(client, pid, "payment.succeeded")
    async with tasks.SessionLocal() as s:
        ids = (await s.execute(select(Notification.id).order_by(Notification.created_at))).scalars().all()
    old = datetime.now(UTC) - timedelta(minutes=10)
    async with engine.begin() as con:
        await con.execute(update(Notification).where(Notification.id.in_(ids[:2])).values(next_attempt_at=old))
        await con.execute(update(Notification).where(Notification.id == ids[1]).values(status=NotificationStatus.sent))
    assert await tasks.overdue_ids() == [ids[0]]


async def test_broker_down_payment_still_ok(client, monkeypatch):
    """Redis лежит: вебхук банка всё равно получает 200, уведомление ждёт в базе sweep()."""

    def broken(nid):
        raise ConnectionError("redis down")

    monkeypatch.setattr(tasks.deliver, "delay", broken)
    pid = await create_with_callback(client)
    r = await webhook(client, pid, "payment.succeeded")
    assert r.status_code == 200 and r.json()["result"] == "applied"
    assert (await only_notification()).status == NotificationStatus.pending


async def test_callback_url_part_of_idempotency(client):
    key = uuid.uuid4().hex
    a = await client.post("/payments", json={"amount": "10.00", "callback_url": SHOP}, headers={"Idempotency-Key": key})
    b = await client.post(
        "/payments",
        json={"amount": "10.00", "callback_url": "https://evil.example/x"},
        headers={"Idempotency-Key": key},
    )
    assert a.status_code == 201 and b.status_code == 409


def test_backoff_grows():
    assert [tasks.backoff(i) for i in range(1, 6)] == [10, 20, 40, 80, 160]
