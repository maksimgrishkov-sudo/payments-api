# payments-api

Небольшой сервис приёма платежей на **FastAPI + PostgreSQL**: создание платежа, вебхуки банка, возвраты.
Задачи, на которых в платёжке теряют деньги, решены явно и закрыты тестами:

| Ситуация | Что происходит |
|---|---|
| Клиент нажал «Оплатить» дважды / сеть повторила запрос | `Idempotency-Key`: тот же ключ — тот же платёж, второй не создаётся. Проверено 10 одновременными запросами |
| Тот же ключ, но другая сумма | 409 — молча списывать нельзя |
| Банк повторил вебхук | событие применяется один раз (`event_id` — первичный ключ) |
| Вебхук с чужой подписью | 401, HMAC-SHA256 от сырого тела, сравнение за постоянное время |
| «Отмена» пришла после «оплачен» | игнорируется: статус двигается только вперёд |
| Пять возвратов одновременно | проходит только тот, что укладывается в сумму (`SELECT … FOR UPDATE` + CHECK в базе) |
| Повтор запроса на возврат | второй раз деньги не возвращаются |
| `0.1 + 0.2` | деньги хранятся в целых копейках, `float` нет нигде; `"10.005"` — ошибка, а не округление |

## Стек
Python 3.11+, FastAPI, Pydantic v2, SQLAlchemy 2.0 (async, asyncpg), PostgreSQL 17, Alembic, pytest + httpx, ruff, Docker, GitHub Actions.

## Запуск
```bash
docker compose up --build          # API на http://localhost:8000, документация — /docs
```

## API
| Метод | Путь | Заголовки |
|---|---|---|
| `POST` | `/payments` — `{"amount": "1500.00", "description": "..."}` | `Idempotency-Key` |
| `GET` | `/payments/{id}` | |
| `POST` | `/payments/{id}/refunds` — `{"amount": "500.00"}` | `Idempotency-Key` |
| `POST` | `/webhooks/bank` — `{"event_id", "type": "payment.succeeded" \| "payment.canceled", "payment_id"}` | `X-Signature` |

## Тесты
Тесты идут против настоящего PostgreSQL, не против моков: блокировки строк и unique-ограничения в SQLite ведут себя иначе.
```bash
python -m venv .venv && .venv/Scripts/pip install -r requirements.txt pytest pytest-asyncio httpx ruff
docker compose up -d db
pytest -q          # 29 тестов: логика денег и статусов, сценарии API, гонки, откат миграций
```
Тест миграций (`downgrade base` → `upgrade head`) нашёл реальную ошибку: автогенерация Alembic
не удаляет enum-тип Postgres при откате, и повторный накат падал.

## Как сделано
Код написан вместе с Claude Code: правила для ассистента — в [`CLAUDE.md`](CLAUDE.md)
(деньги в копейках, инварианты, «готово» = тесты прошли). Архитектуру, сценарии и проверку
результата задавал я.

Автор — Максим Гришков · [grishkovpro.ru](https://grishkovpro.ru) · Telegram [@LoneGunman81](https://t.me/LoneGunman81)
