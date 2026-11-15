"""Build an adapter from a source_registry row's `config` JSON."""
from app.connectors.api import AtsAdapter, HackerNewsAdapter, SecFormDAdapter
from app.connectors.base import SourceAdapter
from app.connectors.feed import FeedAdapter


def build_adapter(name: str, config: dict, *, sec_email: str = "", imap: dict | None = None) -> SourceAdapter:
    kind = config.get("kind")
    if kind == "feed":
        return FeedAdapter(name, config["url"], config.get("region", "global"))
    if kind == "hn":
        return HackerNewsAdapter(name, config["queries"])
    if kind == "sec_form_d":
        return SecFormDAdapter(name, sec_email)
    if kind == "imap":
        from app.connectors.imap import ImapAdapter

        return ImapAdapter(name, **(imap or {}))
    if kind == "ats":
        return AtsAdapter(config["provider"], config["token"])
    raise ValueError(f"unknown connector kind {kind!r} for {name}")
