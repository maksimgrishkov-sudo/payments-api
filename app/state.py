"""Переходы статусов платежа. Отдельно от базы — чтобы проверять тестами без Postgres.

Банк может прислать события не по порядку и повторно. Правило: статус двигается
только вперёд; событие, которое тянет назад (например, «pending» после «succeeded»),
игнорируется, а не ломает платёж.
"""

from app.models import PaymentStatus as S

ALLOWED: dict[S, set[S]] = {
    S.pending: {S.succeeded, S.canceled},
    S.succeeded: {S.refunded},
    S.canceled: set(),
    S.refunded: set(),
}

# Какой статус означает событие банка.
EVENT_STATUS: dict[str, S] = {
    "payment.succeeded": S.succeeded,
    "payment.canceled": S.canceled,
}


def can_move(current: S, new: S) -> bool:
    return new in ALLOWED[current]
