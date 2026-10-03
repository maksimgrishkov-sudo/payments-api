"""Перевод сумм между рублями (как их присылает клиент) и копейками (как хранятся)."""

from decimal import ROUND_HALF_UP, Decimal, InvalidOperation


def to_kopecks(value: str | int | Decimal) -> int:
    """'1500.5' → 150050. Больше двух знаков после точки — ошибка, а не тихое округление."""
    try:
        d = Decimal(str(value))
    except InvalidOperation as e:
        raise ValueError(f"не число: {value!r}") from e
    if not d.is_finite():
        raise ValueError(f"не число: {value!r}")
    if d.as_tuple().exponent < -2:
        raise ValueError(f"больше двух знаков после точки: {value!r}")
    return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def to_rubles(kopecks: int) -> str:
    """150050 → '1500.50'."""
    return f"{Decimal(kopecks) / 100:.2f}"
