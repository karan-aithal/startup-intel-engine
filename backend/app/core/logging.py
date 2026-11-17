import logging
import re

import structlog

# Telegram puts the bot token in the URL path, so any exception text that echoes a URL can leak it.
_PATTERNS = [re.compile(r"bot\d+:[A-Za-z0-9_-]{20,}"), re.compile(r"(?i)bearer\s+\S+"), re.compile(r"://[^/\s:@]+:[^/\s@]+@")]


def redact(value: str) -> str:
    """Remove configured secrets and well-known credential shapes from text headed for logs or the database."""
    from app.core.config import get_settings  # lazy: logging must work before settings load

    try:
        c = get_settings()
        secrets = [c.telegram_bot_token, c.imap_password, c.github_token, c.jooble_api_key]
    except Exception:  # noqa: BLE001 - settings invalid: still apply the patterns
        secrets = []
    for sec in secrets:
        if len(sec) >= 8:
            value = value.replace(sec, "[redacted]")
    for p in _PATTERNS:
        value = p.sub("[redacted]", value)
    return value


def env_file_warning() -> str | None:
    """.env holds every credential: it should be readable by the owner only."""
    from pathlib import Path

    for f in (Path("../.env"), Path(".env")):
        if f.is_file() and f.stat().st_mode & 0o077:
            return f"{f.resolve()} is readable by other users (mode {oct(f.stat().st_mode & 0o777)}); run: chmod 600 {f.resolve()}"
    return None


def _redact_event(_logger, _name, event_dict: dict) -> dict:
    return {k: redact(v) if isinstance(v, str) else v for k, v in event_dict.items()}


def setup_logging(level: str = "INFO", json_logs: bool = False) -> None:
    logging.basicConfig(format="%(message)s", level=level.upper())
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.format_exc_info,  # tracebacks can echo URLs too, so render before redacting
            _redact_event,
            structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
    )
