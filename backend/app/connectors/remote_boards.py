"""Remote-job boards: Himalayas and Remotive. Public, keyless JSON APIs; fields checked against live responses on 2026-11-18.

Only jobs open to India (see services.geo) become events, so a board with ~100k jobs adds a few dozen rows. The location is stored as
"Remote (<restriction>)", e.g. "Remote (worldwide)" or "Remote (APAC)", so alerts and /api/explore apply the same rule to every source.

Remotive's terms: poll at most 4 times a day, show Remotive as the source and link to the job on remotive.com (the source name and url do).
Himalayas: cursor paging, 20 jobs a page. The endpoint shapes follow career-ops providers/himalayas.mjs and remotive.mjs (MIT)."""
from datetime import UTC, datetime

import httpx

from app.connectors.api import _get_json
from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.services.geo import open_to_india

HIMALAYAS_URL = "https://himalayas.app/jobs/api"
REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
HIMALAYAS_PAGES = 5  # ponytail: newest 100 per poll; more than 100 new jobs (eligible or not) inside one poll interval would be missed


def _event(name: str, board: str, *, id: str, title: str, url: str, company: str | None, restriction: str, open_: bool,
           published: datetime | None) -> NormalizedEvent | None:
    location = f"Remote ({restriction or 'worldwide'})"
    if not (id and title and url and open_):  # open_: the board's own restriction names India or none (structured allowlist, not services.geo's denylist)
        return None
    return NormalizedEvent(
        source_name=name, source_event_id=id, source_url=url, canonical_url=url, event_type="job", title=title, company_name=company or None,
        location=location, published_at=published, raw_metadata={"remote": True, "board": board},
        permissions_metadata={"store": "title, company, location and link only"})


def _pub(j: dict) -> int:
    try:
        return int(j.get("pubDate") or 0)  # epoch seconds
    except (TypeError, ValueError):
        return 0


class HimalayasAdapter(SourceAdapter):
    connector_type = "api"

    def __init__(self, name: str, client: httpx.AsyncClient | None = None):
        self.name, self._client = name, client

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        since, jobs, nxt = (cursor or {}).get("since", 0), [], None
        for _ in range(HIMALAYAS_PAGES):
            d = await _get_json(self._client, HIMALAYAS_URL, params={"limit": 20, **({"cursor": nxt} if nxt else {})})
            page = d.get("jobs") or []
            fresh = [j for j in page if _pub(j) > since]
            jobs += fresh
            nxt = d.get("nextCursor")
            if not nxt or len(fresh) < len(page):  # the page reached jobs an earlier poll already saw
                break
        return FetchResult(items=jobs, cursor={"since": max((_pub(j) for j in jobs), default=since)})

    async def normalize(self, j) -> NormalizedEvent | None:
        p, countries = _pub(j), j.get("locationRestrictions") or []  # [] = open worldwide; otherwise the only countries it can be worked from
        shown = ", ".join(countries) if len(countries) <= 3 else f"{'India and ' if 'India' in countries else ''}{len(countries) - ('India' in countries)} other countries"
        return _event(self.name, "himalayas", id=j.get("guid") or j.get("applicationLink") or "", title=j.get("title") or "",
                      url=j.get("applicationLink") or j.get("guid") or "", company=j.get("companyName"), restriction=shown,
                      open_=not countries or "India" in countries, published=datetime.fromtimestamp(p, UTC) if p else None)

    async def health_check(self) -> bool:
        await _get_json(self._client, HIMALAYAS_URL, params={"limit": 1})
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, permissions="public JSON API (himalayas.app/jobs/api)", quota="none published; up to 5 requests per poll")


class RemotiveAdapter(SourceAdapter):
    connector_type = "api"

    def __init__(self, name: str, client: httpx.AsyncClient | None = None):
        self.name, self._client = name, client

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        d = await _get_json(self._client, REMOTIVE_URL)
        return FetchResult(items=d["jobs"], cursor={"count": len(d["jobs"])})

    async def normalize(self, j) -> NormalizedEvent | None:
        try:  # "2026-11-14T20:33:53": no zone, UTC per Remotive's feed
            published = datetime.fromisoformat(j["publication_date"]).replace(tzinfo=UTC) if j.get("publication_date") else None
        except ValueError:
            published = None
        where = (j.get("candidate_required_location") or "").strip()
        return _event(self.name, "remotive", id=str(j.get("id") or j.get("url") or ""), title=j.get("title") or "", url=j.get("url") or "",
                      company=j.get("company_name"), restriction=where, open_=open_to_india(where), published=published)

    async def health_check(self) -> bool:
        return True  # a probe is a request, and Remotive allows 4 a day

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, permissions="Remotive API terms: link back and name Remotive as the source",
                            quota="max 4 requests a day (poll every 6 h)")
