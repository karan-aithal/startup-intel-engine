from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated settings. Loaded from the repo-root .env; startup fails if invalid."""

    model_config = SettingsConfigDict(env_file=("../.env", ".env"), extra="ignore")

    app_env: str = Field("dev", pattern="^(dev|prod)$")
    log_level: str = "INFO"
    api_host: str = "127.0.0.1"
    api_port: int = Field(8000, ge=1, le=65535)
    cors_origins: str = "http://127.0.0.1:3000,http://localhost:3000"
    database_url: str  # postgresql+asyncpg://user:pass@127.0.0.1:5432/startup_intel
    display_timezone: str = "Asia/Kolkata"
    telegram_bot_token: str = ""  # empty = alerts print to the console instead
    telegram_chat_id: str = ""
    telegram_owner_id: int = 0  # your Telegram user id; only you can feed the share-to-bot route
    telegram_topic_ids: str = "{}"  # JSON {"India Funding": 12, ...} message_thread_id per topic name
    imap_host: str = "imap.gmail.com"
    imap_user: str = ""  # dedicated alerts inbox; empty = the email route stays off
    imap_password: str = ""  # Gmail app password (needs 2-step verification), never the account password
    imap_folder: str = "INBOX"
    sec_contact_email: str = ""  # required by SEC EDGAR in the User-Agent

    @field_validator("database_url")
    @classmethod
    def _check_db(cls, v: str) -> str:
        if not v.startswith("postgresql+asyncpg://"):
            raise ValueError("DATABASE_URL must start with postgresql+asyncpg://")
        return v

    @property
    def cors_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
