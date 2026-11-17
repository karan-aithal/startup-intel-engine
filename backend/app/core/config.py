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
    cors_origins: str = "http://127.0.0.1:3010,http://localhost:3010,http://127.0.0.1:3000,http://localhost:3000"
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
    llm_enabled: bool = False  # turn on after the model server (LM Studio or llama-server) is running
    llm_base_url: str = "http://127.0.0.1:8080/v1"  # llama-server (LM Studio server is :1234 but cannot turn thinking off)
    llm_embed_base_url: str = "http://127.0.0.1:8081/v1"  # llama-server runs one model per port (scripts/llm_servers.sh); the chat server returns 501 for embeddings
    llm_model: str = "qwen3.5-4b"
    llm_embed_model: str = "nomic-embed"
    auto_watch_cap: int = 200  # max companies the discovery loop may add to polling on its own
    auto_board_cap: int = 300  # max active auto-registered ATS boards (a company can have several); enforced separately
    auto_source_cap: int = 0  # max discovered company feeds/careers pages the expansion loop may switch on; 0 = expansion off until you choose
    github_token: str = ""  # optional: 5,000 requests/hour instead of 60 (a fine-grained token with no permissions is enough for public repos)
    jooble_api_key: str = ""  # per-country key from jooble.org/api/about; the route also needs terms_confirmed in sources.yaml
    jooble_base_url: str = "https://jooble.org/api"  # each Jooble country has its own domain: confirm yours before enabling
    deletion_ledger: str = "~/.config/startup-intel/deletions.jsonl"  # append-only, outside the database: see services/retention.py
    backup_dir: str = "~/backups/startup-intel"  # where scripts/backup.sh writes; the watchdog alerts when it goes stale
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
