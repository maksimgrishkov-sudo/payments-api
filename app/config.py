from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://payments:payments@localhost:5432/payments"
    # Секрет, которым банк подписывает вебхуки (HMAC-SHA256 от сырого тела запроса).
    webhook_secret: str = "change-me"


settings = Settings()
