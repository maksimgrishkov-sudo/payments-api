import enum
import uuid
from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Enum, ForeignKey, Index, Integer, String, func, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class PaymentStatus(enum.StrEnum):
    pending = "pending"  # создан, ждём ответа банка
    succeeded = "succeeded"  # деньги списаны
    canceled = "canceled"  # банк отклонил или клиент не оплатил
    refunded = "refunded"  # возвращено полностью


class Payment(Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("refunded_amount >= 0 AND refunded_amount <= amount", name="refund_within_amount"),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    # Деньги — только целые копейки: float теряет копейки на суммах вроде 0.1 + 0.2.
    amount: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3), default="RUB")
    description: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus, name="payment_status"), default=PaymentStatus.pending
    )
    refunded_amount: Mapped[int] = mapped_column(Integer, default=0)
    # Куда сообщать магазину о смене статуса; нет адреса — уведомлений нет.
    callback_url: Mapped[str | None] = mapped_column(String(500), default=None)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    refunds: Mapped[list["Refund"]] = relationship(back_populates="payment", order_by="Refund.created_at")


class Refund(Base):
    __tablename__ = "refunds"
    __table_args__ = (CheckConstraint("amount > 0", name="refund_amount_positive"),)

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    amount: Mapped[int] = mapped_column(Integer)
    idempotency_key: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    payment: Mapped[Payment] = relationship(back_populates="refunds")


class NotificationStatus(enum.StrEnum):
    pending = "pending"  # ждёт отправки или повтора
    sent = "sent"  # магазин ответил 2xx
    failed = "failed"  # попытки кончились — нужен человек


class Notification(Base):
    """Исходящее уведомление магазину (transactional outbox).

    Строка пишется в той же транзакции, что и смена статуса платежа: статус не может поменяться
    «без уведомления», даже если Redis в этот момент лежит. Отправляет воркер Celery, с повторами."""

    __tablename__ = "notifications"
    __table_args__ = (
        Index("ix_notifications_pending", "next_attempt_at", postgresql_where=text("status = 'pending'")),
    )

    id: Mapped[uuid.UUID] = mapped_column(primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    event: Mapped[str] = mapped_column(String(32))
    url: Mapped[str] = mapped_column(String(500))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[NotificationStatus] = mapped_column(
        Enum(NotificationStatus, name="notification_status"), default=NotificationStatus.pending
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300), default=None)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=None)


class WebhookEvent(Base):
    """Каждое событие банка — один раз: банк повторяет вебхуки, пока не получит 200."""

    __tablename__ = "webhook_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("payments.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSONB)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
