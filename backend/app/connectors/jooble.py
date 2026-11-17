"""Jooble job-aggregator API. Disabled by default and gated twice: the route needs `terms_confirmed` (the owner's confirmation of permitted
use) and an API key. Jooble publishes no API-specific terms; its general terms ban automated access to the SITE, so only the documented
REST API is used, with the key from the owner's own registration.

Official docs: https://jooblehelpcenter.freshdesk.com/en/support/solutions/articles/60001448238-rest-api-documentation
POST {base}/{key} with JSON {keywords, location, page}; one key per country domain; free plan 100 requests/day (429 after that), reset 00:00 UTC.
The key sits in the URL path, so no exception text from this module ever contains the URL."""
import re
from datetime import UTC, datetime, timedelta

import httpx

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.services.quota import RateLimited


def _iso(s: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat(s) if s else None
    except ValueError:
        return None
    return d.replace(tzinfo=UTC) if d and d.tzinfo is None else d  # Jooble documents "ISO timestamp" without a zone: read as UTC


def _seconds_to_midnight_utc() -> float:
    n = datetime.now(UTC)
    return (n.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1) - n).total_seconds()


class JoobleAdapter(SourceAdapter):
    connector_type = "api"
    provider = "jooble"

    def __init__(self, name: str, api_key: str, base_url: str, keywords: list[str], location: str, pages: int = 1,
                 region: str = "india", client: httpx.AsyncClient | None = None):
        self.name, self._key, self.base, self.keywords, self.location = name, api_key, base_url.rstrip("/"), keywords, location
        self.pages, self.region, self._client = pages, region, client

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        c, jobs, retry = self._client or httpx.AsyncClient(timeout=20), {}, None
        try:
            for kw in self.keywords:
                for page in range(1, self.pages + 1):
                    try:
                        if self.limiter:
                            await self.limiter.take()
                        r = await c.post(f"{self.base}/{self._key}", json={"keywords": kw, "location": self.location, "page": page})
                    except RateLimited as rl:
                        retry = rl.wait_s
                        break
                    except httpx.HTTPError as exc:
                        raise RuntimeError(f"Jooble request failed: {type(exc).__name__}") from None  # never echo the URL (it holds the key)
                    if r.status_code == 429:  # daily limit: nothing more today
                        wait = _seconds_to_midnight_utc()
                        if self.limiter:
                            await self.limiter.block(wait)
                        retry = wait
                        break
                    if r.status_code != 200:
                        raise RuntimeError(f"Jooble HTTP {r.status_code}")
                    got = r.json().get("jobs") or []
                    for j in got:
                        jobs[j["id"]] = j
                    if not got:
                        break
                if retry:
                    break
        finally:
            if self._client is None:
                await c.aclose()
        return FetchResult(items=list(jobs.values()), cursor={"last_run": datetime.now(UTC).isoformat()}, retry_after_s=retry)

    async def normalize(self, j) -> NormalizedEvent | None:
        if not j.get("title") or not j.get("link"):
            return None
        return NormalizedEvent(
            source_name=self.name, source_event_id=str(j["id"]), source_url=j["link"], canonical_url=j["link"], event_type="job",
            title=j["title"], content=re.sub(r"<[^>]+>|&nbsp;", " ", j.get("snippet") or "").strip(), published_at=_iso(j.get("updated")),
            company_name=j.get("company") or None, location=j.get("location"), country="India" if self.region == "india" else None,
            raw_metadata={"region": self.region, "listed_on": j.get("source"), "type": j.get("type"), "salary": j.get("salary")},
            permissions_metadata={"store": "title, snippet and Jooble link only"})

    async def health_check(self) -> bool:
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(search=True, auth_type="api key (URL path)", quota="free plan: 100 requests/day per key",
                            permissions="Jooble REST API with the owner's own key; use beyond that NOT confirmed")
