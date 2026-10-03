import hashlib
import hmac


def sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def signature_ok(body: bytes, signature: str | None, secret: str) -> bool:
    """Подпись считаем от сырого тела: после разбора JSON порядок ключей и пробелы другие.
    compare_digest — сравнение за постоянное время, чтобы подпись нельзя было подбирать по таймингу."""
    if not signature:
        return False
    return hmac.compare_digest(sign(body, secret), signature)


def request_hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()
