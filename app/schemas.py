import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models import PaymentStatus
from app.money import to_kopecks, to_rubles


class PaymentCreate(BaseModel):
    # Сумма строкой, как в API банков: '1500.50'. Число с плавающей точкой не принимаем.
    amount: str = Field(examples=["1500.50"])
    currency: Literal["RUB"] = "RUB"
    description: str = Field(default="", max_length=255)

    @field_validator("amount")
    @classmethod
    def amount_valid(cls, v: str) -> str:
        if to_kopecks(v) <= 0:
            raise ValueError("сумма должна быть больше нуля")
        return v


class RefundCreate(BaseModel):
    amount: str = Field(examples=["500.00"])

    @field_validator("amount")
    @classmethod
    def amount_valid(cls, v: str) -> str:
        if to_kopecks(v) <= 0:
            raise ValueError("сумма должна быть больше нуля")
        return v


class RefundOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    amount: str
    created_at: datetime

    @field_validator("amount", mode="before")
    @classmethod
    def rubles(cls, v: int | str) -> str:
        return to_rubles(v) if isinstance(v, int) else v


class PaymentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    amount: str
    refunded_amount: str
    currency: str
    description: str
    status: PaymentStatus
    created_at: datetime
    refunds: list[RefundOut] = []

    @field_validator("amount", "refunded_amount", mode="before")
    @classmethod
    def rubles(cls, v: int | str) -> str:
        return to_rubles(v) if isinstance(v, int) else v


class WebhookIn(BaseModel):
    event_id: str = Field(max_length=64)
    type: str
    payment_id: uuid.UUID
