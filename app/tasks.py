"""Фоновая отправка уведомлений магазину: Celery + Redis.

Почему не прямо из запроса: магазин может лежать или отвечать минуту — запрос банка нельзя держать.
Почему outbox, а не просто .delay(): если Redis упал между commit и .delay(), задача потеряется,
а строка в notifications останется — её подберёт sweep().

Гарантия — «хотя бы один раз»: магазин может получить уведомление дважды (воркер упал после отправки,
до commit), поэтому шлём X-Notification-Id — по нему магазин отбрасывает повтор.
"""

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta

import httpx
from celery import Celery
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config import settings
from app.models import Notification, NotificationStatus
from app.security import sign

celery = Celery("payments", broker=settings.redis_url)
celery.conf.update(
    task_acks_late=True,  # задача снимается с очереди после выполнения: упал воркер — её возьмёт другой
    task_reject_on_worker_lost=True,
    beat_schedule={"sweep": {"task": "app.tasks.sweep", "schedule": 60.0}},
    timezone="UTC",
)

MAX_ATTEMPTS = 6
TIMEOUT = 10.0
# Свой engine без пула: каждая задача крутит asyncio.run() в своём event loop.
SessionLocal = async_sessionmaker(
    create_async_engine(settings.database_url, poolclass=NullPool), expire_on_commit=False
)


def backoff(attempt: int) -> int:
    """10 с, 20 с, 40 с… — всего около 5 минут на 6 попыток."""
    return 10 * 2 ** (attempt - 1)


async def deliver_once(notification_id: uuid.UUID, client: httpx.AsyncClient | None = None) -> str:
    """Одна попытка: sent / retry / failed / skip."""
    async with SessionLocal() as session, session.begin():
        # SKIP LOCKED: если эту же строку прямо сейчас шлёт другой воркер (повтор + sweep) — уходим.
        n = (
            await session.execute(
                select(Notification).where(Notification.id == notification_id).with_for_update(skip_locked=True)
            )
        ).scalar_one_or_none()
        if n is None or n.status != NotificationStatus.pending:
            return "skip"
        body = json.dumps(n.payload, ensure_ascii=False, separators=(",", ":")).encode()
        headers = {
            "Content-Type": "application/json",
            "X-Signature": sign(body, settings.callback_secret),
            "X-Notification-Id": str(n.id),
        }
        n.attempts += 1
        # Строка заблокирована, пока ждём магазин (до TIMEOUT) — так второй воркер её не возьмёт.
        own = client is None  # чужой клиент (тесты, общий пул) не закрываем
        c = client or httpx.AsyncClient(timeout=TIMEOUT)
        try:
            r = await c.post(n.url, content=body, headers=headers)
            error = None if r.is_success else f"HTTP {r.status_code}"
        except httpx.HTTPError as e:
            error = f"{type(e).__name__}: {e}"[:300]
        finally:
            if own:
                await c.aclose()
        if error is None:
            n.status, n.sent_at, n.last_error = NotificationStatus.sent, datetime.now(UTC), None
            return "sent"
        n.last_error = error
        if n.attempts >= MAX_ATTEMPTS:
            n.status = NotificationStatus.failed
            return "failed"
        n.next_attempt_at = datetime.now(UTC) + timedelta(seconds=backoff(n.attempts))
        return "retry"


async def overdue_ids(grace: int = 60) -> list[uuid.UUID]:
    """Ждут отправки дольше срока + запас: значит, задача в Redis потерялась."""
    async with SessionLocal() as session:
        edge = datetime.now(UTC) - timedelta(seconds=grace)
        rows = await session.execute(
            select(Notification.id)
            .where(Notification.status == NotificationStatus.pending, Notification.next_attempt_at <= edge)
            .limit(500)
        )
        return list(rows.scalars())


@celery.task(bind=True, max_retries=MAX_ATTEMPTS)
def deliver(self, notification_id: str) -> str:
    result = asyncio.run(deliver_once(uuid.UUID(notification_id)))
    if result == "retry":
        raise self.retry(countdown=backoff(self.request.retries + 1))
    return result


@celery.task
def sweep() -> int:
    ids = asyncio.run(overdue_ids())
    for i in ids:
        deliver.delay(str(i))
    return len(ids)


def enqueue(ids: list[uuid.UUID]) -> None:
    """Вызывать после commit. Redis недоступен — не страшно: строки в базе, их подберёт sweep()."""
    for i in ids:
        try:
            deliver.delay(str(i))
        except Exception:  # noqa: BLE001 — брокер лёг; платёж уже сохранён, уведомление догонит sweep
            pass
