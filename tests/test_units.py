"""Чистая логика — без базы."""

import pytest

from app.models import PaymentStatus as S
from app.money import to_kopecks, to_rubles
from app.security import sign, signature_ok
from app.state import can_move


@pytest.mark.parametrize(
    "raw, kop",
    [
        ("1500", 150000),
        ("1500.5", 150050),
        ("1500.50", 150050),
        ("0.01", 1),
        (10, 1000),
        ("0.3", 30),  # 0.1 + 0.2 во float = 0.30000000000000004, у нас ровно 30 копеек
    ],
)
def test_to_kopecks(raw, kop):
    assert to_kopecks(raw) == kop


@pytest.mark.parametrize("bad", ["10.005", "abc", "", "NaN", "Infinity"])
def test_to_kopecks_rejects(bad):
    with pytest.raises(ValueError):
        to_kopecks(bad)


def test_to_rubles():
    assert to_rubles(150050) == "1500.50"
    assert to_rubles(1) == "0.01"


def test_status_moves_only_forward():
    assert can_move(S.pending, S.succeeded)
    assert can_move(S.pending, S.canceled)
    assert can_move(S.succeeded, S.refunded)
    assert not can_move(S.succeeded, S.canceled)  # списанные деньги «отменой» не вернуть — только возвратом
    assert not can_move(S.succeeded, S.pending)
    assert not can_move(S.canceled, S.succeeded)
    assert not can_move(S.refunded, S.succeeded)


def test_signature():
    body = b'{"event_id":"e1"}'
    good = sign(body, "s")
    assert signature_ok(body, good, "s")
    assert not signature_ok(body, good, "другой секрет")
    assert not signature_ok(body + b" ", good, "s")  # тело изменено после подписи
    assert not signature_ok(body, None, "s")
    assert not signature_ok(body, "", "s")
