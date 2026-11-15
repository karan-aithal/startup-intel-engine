"""Per-sender email parsers. Register one per sender once a real sample email exists.

A parser takes the message dict ({uid, from_email, subject, date, html, text, message_id}) and the source
name, and returns events. No parsers are registered yet: unknown senders use `fallback`, which keeps only
the subject. Never follow links found in an email here; resolve them later, only for items that pass filters.
"""
from collections.abc import Callable

import structlog

from app.connectors.base import NormalizedEvent

log = structlog.get_logger()
Parser = Callable[[dict, str], list[NormalizedEvent]]
_PARSERS: list[tuple[tuple[str, ...], Parser]] = []


def register(*domains: str):
    """@register("linkedin.com") matches the sender's domain and its subdomains."""
    def deco(fn: Parser) -> Parser:
        _PARSERS.append((tuple(d.lower() for d in domains), fn))
        return fn
    return deco


def sender_domain(msg: dict) -> str:
    return (msg.get("from_email") or "").rpartition("@")[2].lower()


def fallback(msg: dict, source: str) -> list[NormalizedEvent]:
    """Unknown sender or failed parser: store the subject only, flagged so a parser can be written."""
    return [NormalizedEvent(
        source_name=source, source_event_id=msg.get("message_id") or f"uid:{msg['uid']}", event_type="email",
        title=msg.get("subject"), published_at=msg.get("date"),
        raw_metadata={"parser": "fallback", "sender_domain": sender_domain(msg)},
        permissions_metadata={"store": "subject only (own mailbox)"})]


def parse(msg: dict, source: str) -> list[NormalizedEvent]:
    dom = sender_domain(msg)
    for domains, fn in _PARSERS:
        if any(dom == d or dom.endswith("." + d) for d in domains):
            try:
                return fn(msg, source)
            except Exception as exc:  # noqa: BLE001 - one bad template must not lose the email
                log.warning("email_parser_failed", sender=dom, error=str(exc))
                break
    return fallback(msg, source)
