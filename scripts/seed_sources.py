"""Register approved sources from config/sources.yaml. New routes start DISABLED.
Usage: cd backend && uv run python ../scripts/seed_sources.py [--enable]
--enable turns on every route except SEC when SEC_CONTACT_EMAIL is unset."""
import asyncio
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))  # so `app` imports without PYTHONPATH

from app.core.config import get_settings
from app.db.session import get_engine, get_sessionmaker
from app.services.registry import register_source, set_enabled

DOCS = {"feed": "robots.txt checked; see docs/phase2-proposals.md",
        "hn": "https://hn.algolia.com/api", "sec": "https://www.sec.gov/search-filings/edgar-application-programming-interfaces",
        "greenhouse": "https://developers.greenhouse.io/job-board.html",
        "lever": "https://github.com/lever/postings-api", "ashby": "https://developers.ashbyhq.com"}


def routes(cfg: dict):
    """Yield (name, connector_type, fields) for every approved source."""
    for f in cfg["feeds"]:
        yield f["name"], "feed", dict(category="funding", access_method="RSS/Atom", docs_url=DOCS["feed"],
            poll_interval_seconds=f["poll"], config={"kind": "feed", "url": f["url"], "region": f["region"]})
    h = cfg["hn"]
    yield h["name"], "api", dict(category="funding", access_method="HN Algolia search API", docs_url=DOCS["hn"],
        poll_interval_seconds=h["poll"], config={"kind": "hn", "queries": h["queries"]})
    s = cfg["sec_form_d"]
    yield s["name"], "feed", dict(category="funding", access_method="EDGAR current-filings Atom", docs_url=DOCS["sec"],
        poll_interval_seconds=s["poll"], config={"kind": "sec_form_d"})
    m = cfg["imap"]
    yield m["name"], "email_alert_parser", dict(category="inbox", access_method="IMAP (app password)",
        docs_url="https://pypi.org/project/imap-tools/", poll_interval_seconds=m["poll"], retention_days=30,
        config={"kind": "imap"})
    for t in cfg.get("telegram_channels", []):
        yield f"telegram:{t['channel']}", "public_preview_poller", dict(category="community", access_method="t.me/s preview",
            docs_url="https://core.telegram.org/api/web-preview", poll_interval_seconds=t["poll"], config={"kind": "telegram", "channel": t["channel"]})
    b = cfg["bluesky"]
    yield b["name"], "stream", dict(category="community", access_method="Jetstream WebSocket", docs_url="https://github.com/bluesky-social/jetstream",
        config={"kind": "bluesky", "accounts": b["accounts"]})
    for m in cfg.get("page_monitors", []):
        yield m["name"], "page_monitor", dict(category="hiring", access_method="sitemap + schema.org JobPosting", docs_url="robots.txt checked",
            poll_interval_seconds=m["poll"], config={"kind": "page_monitor", "careers_url": m["careers_url"], "company": m.get("company")})
    for provider, tokens in cfg["ats"].items():
        for t in tokens:
            yield f"{provider}:{t}", "api", dict(category="hiring", access_method=f"{provider} job board API",
                docs_url=DOCS[provider], poll_interval_seconds=cfg["ats_poll"],
                config={"kind": "ats", "provider": provider, "token": t})


async def main(enable: bool) -> None:
    cfg = yaml.safe_load((ROOT / "config/sources.yaml").read_text())
    st = get_settings()
    has_email, has_imap = bool(st.sec_contact_email), bool(st.imap_user and st.imap_password)
    sm, n, on = get_sessionmaker(), 0, 0
    async with sm() as s:
        for name, ctype, fields in routes(cfg):
            await register_source(s, name, ctype, **fields)
            n += 1
            kind = fields["config"]["kind"]
            if enable and (has_email or kind != "sec_form_d") and (has_imap or kind != "imap"):
                await set_enabled(s, name, ctype, True)
                on += 1
        await s.commit()
    print(f"registered {n} routes, enabled {on}" + "".join([" (SEC skipped: SEC_CONTACT_EMAIL unset)" if not has_email else "", " (IMAP skipped: IMAP_USER/IMAP_PASSWORD unset)" if not has_imap else ""]))
    await get_engine().dispose()


asyncio.run(main("--enable" in sys.argv))
