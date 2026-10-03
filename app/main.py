import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import settings
from app.db import get_session
from app.models import Payment, PaymentStatus, Refund, WebhookEvent
from app.money import to_kopecks
from app.schemas import PaymentCreate, PaymentOut, RefundCreate, WebhookIn
from app.security import request_hash, signature_ok
from app.state import EVENT_STATUS, can_move

app = FastAPI(title="payments-api", version="1.0.0")

IdemKey = Header(alias="Idempotency-Key", min_length=8, max_length=64)


async def load_payment(session: AsyncSession, payment_id: uuid.UUID, *, lock: bool = False) -> Payment:
    q = select(Payment).where(Payment.id == payment_id).options(selectinload(Payment.refunds))
    if lock:
        q = q.with_for_update()  # строка заблокирована до конца транзакции: два возврата сразу не пройдут
    payment = (await session.execute(q)).scalar_one_or_none()
    if payment is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "платёж не найден")
    return payment


@app.get("/health")
async def health() -> dict:
    return {"ok": True}


@app.post("/payments", response_model=PaymentOut, status_code=status.HTTP_201_CREATED)
async def create_payment(
    body: PaymentCreate,
    idempotency_key: str = IdemKey,
    session: AsyncSession = Depends(get_session),
) -> Payment:
    """Повтор с тем же ключом и тем же телом вернёт тот же платёж, а не создаст второй.
    Тот же ключ с другим телом — 409: клиент перепутал ключи, молча списывать нельзя."""
    h = request_hash(body.amount, body.currency, body.description)
    existing = (
        await session.execute(
            select(Payment).where(Payment.idempotency_key == idempotency_key).options(selectinload(Payment.refunds))
        )
    ).scalar_one_or_none()
    if existing is not None:
        return _same_or_conflict(existing, h)

    payment = Payment(
        amount=to_kopecks(body.amount),
        currency=body.currency,
        description=body.description,
        idempotency_key=idempotency_key,
        request_hash=h,
        status=PaymentStatus.pending,
    )
    session.add(payment)
    try:
        await session.commit()
    except IntegrityError:
        # Два одинаковых запроса пришли одновременно: второй упёрся в unique-ключ.
        await session.rollback()
        existing = (
            await session.execute(
                select(Payment).where(Payment.idempotency_key == idempotency_key).options(selectinload(Payment.refunds))
            )
        ).scalar_one()
        return _same_or_conflict(existing, h)
    return await load_payment(session, payment.id)


def _same_or_conflict(existing: Payment, h: str) -> Payment:
    if existing.request_hash != h:
        raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency-Key уже использован с другими данными")
    return existing


@app.get("/payments/{payment_id}", response_model=PaymentOut)
async def get_payment(payment_id: uuid.UUID, session: AsyncSession = Depends(get_session)) -> Payment:
    return await load_payment(session, payment_id)


@app.post("/payments/{payment_id}/refunds", response_model=PaymentOut)
async def refund(
    payment_id: uuid.UUID,
    body: RefundCreate,
    idempotency_key: str = IdemKey,
    session: AsyncSession = Depends(get_session),
) -> Payment:
    amount = to_kopecks(body.amount)
    async with session.begin():
        payment = await load_payment(session, payment_id, lock=True)
        done = next((r for r in payment.refunds if r.idempotency_key == idempotency_key), None)
        if done is not None:
            if done.amount != amount:
                raise HTTPException(status.HTTP_409_CONFLICT, "Idempotency-Key уже использован с другой суммой")
        else:
            if payment.status != PaymentStatus.succeeded:
                raise HTTPException(status.HTTP_409_CONFLICT, f"возврат невозможен: статус {payment.status.value}")
            if payment.refunded_amount + amount > payment.amount:
                raise HTTPException(422, "сумма возвратов больше суммы платежа")
            payment.refunds.append(Refund(amount=amount, idempotency_key=idempotency_key))
            payment.refunded_amount += amount
            if payment.refunded_amount == payment.amount:
                payment.status = PaymentStatus.refunded
    return await load_payment(session, payment_id)


@app.post("/webhooks/bank")
async def bank_webhook(
    request: Request,
    signature: str | None = Header(default=None, alias="X-Signature"),
    session: AsyncSession = Depends(get_session),
) -> dict:
    raw = await request.body()
    if not signature_ok(raw, signature, settings.webhook_secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "неверная подпись")
    event = WebhookIn.model_validate_json(raw)
    new_status = EVENT_STATUS.get(event.type)

    async with session.begin():
        payment = await load_payment(session, event.payment_id, lock=True)
        # Банк повторяет вебхук, пока не получит 200: второй раз то же событие не применяем.
        inserted = (
            await session.execute(
                insert(WebhookEvent)
                .values(
                    event_id=event.event_id,
                    payment_id=payment.id,
                    type=event.type,
                    payload=event.model_dump(mode="json"),
                )
                .on_conflict_do_nothing(index_elements=["event_id"])
                .returning(WebhookEvent.event_id)
            )
        ).scalar_one_or_none()
        if inserted is None:
            return {"result": "duplicate"}
        if new_status is None:
            return {"result": "ignored", "reason": f"неизвестный тип {event.type}"}
        if not can_move(payment.status, new_status):
            # Пришло не по порядку или противоречит текущему статусу — фиксируем, но не применяем.
            return {"result": "ignored", "reason": f"{payment.status.value} → {new_status.value} нельзя"}
        payment.status = new_status
    return {"result": "applied", "status": new_status.value}
