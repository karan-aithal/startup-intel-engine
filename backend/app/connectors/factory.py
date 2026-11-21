"""Build an adapter from a source_registry row's `config` JSON."""
from app.connectors.api import AtsAdapter, HackerNewsAdapter, SecFormDAdapter
from app.connectors.base import SourceAdapter
from app.connectors.feed import FeedAdapter


def build_adapter(name: str, config: dict, *, sec_email: str = "", imap: dict | None = None, bot: dict | None = None,
                  secrets: dict | None = None) -> SourceAdapter:
    secrets = secrets or {}
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
    if kind == "share_bot":
        from app.connectors.share_bot import ShareBotAdapter

        return ShareBotAdapter(name, **(bot or {"token": "", "allowed_ids": set()}))
    if kind == "telegram":
        from app.connectors.telegram_preview import TelegramPreviewAdapter

        return TelegramPreviewAdapter(config["channel"])
    if kind == "page_monitor":
        from app.connectors.page_monitor import PageMonitorAdapter

        return PageMonitorAdapter(name, config["careers_url"], config.get("company"))
    if kind == "github":
        from app.connectors.github import GithubAdapter

        return GithubAdapter(name, config.get("orgs", []), secrets.get("github_token", ""))
    if kind == "jooble":
        from app.connectors.jooble import JoobleAdapter

        if not config.get("terms_confirmed"):  # the owner's confirmation that this use of the API is permitted
            raise ValueError("Jooble is gated: set jooble.terms_confirmed in config/sources.yaml after confirming permitted use")
        if not secrets.get("jooble_api_key"):
            raise ValueError("Jooble is gated: JOOBLE_API_KEY is not set")
        return JoobleAdapter(name, secrets["jooble_api_key"], secrets.get("jooble_base_url") or "https://jooble.org/api", config["keywords"],
                             config.get("location", "India"), config.get("pages", 1), config.get("region", "india"))
    if kind in ("himalayas", "remotive", "jobicy", "workingnomads", "weworkremotely"):
        from app.connectors import remote_boards as rb

        return {"himalayas": rb.HimalayasAdapter, "remotive": rb.RemotiveAdapter, "jobicy": rb.JobicyAdapter, "workingnomads": rb.WorkingNomadsAdapter,
                "weworkremotely": rb.WeWorkRemotelyAdapter}[kind](name)
    if kind == "ats":
        return AtsAdapter(config["provider"], config["token"])
    raise ValueError(f"unknown connector kind {kind!r} for {name}")
