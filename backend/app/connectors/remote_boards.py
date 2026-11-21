"""Remote-job boards: Himalayas, Remotive, Jobicy, We Work Remotely and Working Nomads. Public, keyless feeds; fields checked against live responses
on 2026-11-18 (Himalayas, Remotive) and 2026-11-21 (the other three).

Only jobs open to India (see services.geo) become events, so a board with ~100k jobs adds a few dozen rows. The location is stored as
"Remote (<restriction>)", e.g. "Remote (worldwide)" or "Remote (APAC)", so alerts and /api/explore apply the same rule to every source.

Remotive's terms: poll at most 4 times a day, show Remotive as the source and link to the job on remotive.com (the source name and url do).
Himalayas: search endpoint, page=N, 20 jobs a page. The endpoint shapes follow career-ops providers/himalayas.mjs and remotive.mjs (MIT).
Jobicy (decision 106): credit Jobicy and link to the source (the source name and url do), one automated pass an hour at most, 1 s between requests.
We Work Remotely and Working Nomads (decision 106): no terms could be read; used on robots.txt alone, as the owner accepted. Shapes follow career-ops
providers/jobicy.mjs, weworkremotely.mjs and workingnomads.mjs (MIT, reference only)."""
import asyncio
import calendar
import html
import re
from datetime import UTC, datetime

import httpx

from app.connectors.api import _get_json
from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.connectors.feed import FeedAdapter
from app.services.geo import open_to_india

REMOTIVE_URL = "https://remotive.com/api/remote-jobs"
HIMALAYAS_SEARCH = "https://himalayas.app/jobs/api/search"  # documented: page=N (20 a page, no cursor), country, worldwide, exclude_worldwide, sort
# Two server-side filters instead of the browse feed (decision 99): the feed's pubDate is one refresh time shared by whole batches of
# 100k+ jobs, so "newest 100" saw about a fifth of a batch. Search gives real update times, ordered, already limited to what India can take.
HIMALAYAS_QUERIES = {"in": {"country": "IN", "exclude_worldwide": "true"}, "ww": {"worldwide": "true"}}  # restricted lists naming India; no restriction
HIMALAYAS_PAGES = 5  # ponytail: newest 100 per query per poll (live 2026-11-18: about 125 and 40 a day); more inside one poll interval would be missed


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
        jobs, nxt = [], {}
        for key, query in HIMALAYAS_QUERIES.items():
            since = newest = (cursor or {}).get(f"since_{key}", 0)
            for page in range(1, HIMALAYAS_PAGES + 1):
                d = await _get_json(self._client, HIMALAYAS_SEARCH, params={**query, "sort": "recent", "page": page})
                rows = d.get("jobs") or []
                fresh = [j for j in rows if _pub(j) >= since]  # >=: jobs updated in the same second as the cursor are kept; event ids dedupe the repeat
                jobs += fresh
                newest = max([newest, *map(_pub, fresh)])
                if len(rows) < 20 or _pub(rows[-1]) < since:  # last page, or its tail is already seen (a few pinned old jobs can top the worldwide list, never end a page)
                    break
            nxt[f"since_{key}"] = newest
        return FetchResult(items=jobs, cursor=nxt)

    async def normalize(self, j) -> NormalizedEvent | None:
        p, countries = _pub(j), j.get("locationRestrictions") or []  # [] = open worldwide; otherwise the only countries it can be worked from
        shown = ", ".join(countries) if len(countries) <= 3 else f"{'India and ' if 'India' in countries else ''}{len(countries) - ('India' in countries)} other countries"
        return _event(self.name, "himalayas", id=j.get("guid") or j.get("applicationLink") or "", title=j.get("title") or "",
                      url=j.get("applicationLink") or j.get("guid") or "", company=j.get("companyName"), restriction=shown,
                      open_=not countries or "India" in countries, published=datetime.fromtimestamp(p, UTC) if p else None)

    async def health_check(self) -> bool:
        await _get_json(self._client, HIMALAYAS_SEARCH, params={"worldwide": "true", "page": 1})
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, permissions="public JSON API (himalayas.app/jobs/api)", quota="keyless limits not published; up to 10 requests per poll, one poll every 6 h")


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


JOBICY_URL = "https://jobicy.com/api/v2/remote-jobs"
JOBICY_GEOS = ("anywhere", "apac")  # geoSlug values from ?get=locations; the list has no India slug. A job listed for "India, USA" only is missed (ponytail).
JOBICY_PAGES = 5  # 100 a page; the 7-day window holds about 1,400 jobs in all, far fewer for these two slugs
JOBICY_PAUSE = 1.1  # seconds between requests: Jobicy's fair use asks for at least 1 s from one IP


def _iso(v) -> datetime | None:
    try:
        d = datetime.fromisoformat(v) if isinstance(v, str) and v else None
        return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


class JobicyAdapter(SourceAdapter):
    """Jobicy's v2 feed: newest first, cursor pages, 7-day window, new jobs appear 3 h after publication. Filtered server-side to the Anywhere and APAC slugs."""
    connector_type = "api"

    def __init__(self, name: str, client: httpx.AsyncClient | None = None):
        self.name, self._client = name, client

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        jobs, nxt, first = [], {}, True
        for geo in JOBICY_GEOS:
            since = newest = (cursor or {}).get(f"since_{geo}", 0)
            token = None
            for _ in range(JOBICY_PAGES):
                if not first:
                    await asyncio.sleep(JOBICY_PAUSE)
                first = False
                d = await _get_json(self._client, JOBICY_URL, params={"count": 100, "geo": geo, **({"cursor": token} if token else {})})
                rows = d.get("jobs") or []
                stamps = [(j, int(t.timestamp()) if (t := _iso(j.get("pubDate"))) else 0) for j in rows]
                jobs += [j for j, ts in stamps if ts >= since]  # >=: same-second jobs are kept; event ids dedupe the repeat
                newest = max([newest, *(ts for _, ts in stamps if ts >= since)])
                token = d.get("nextCursor")
                if not d.get("hasMore") or not token or (stamps and stamps[-1][1] < since):  # last page, or its tail is already seen
                    break
            nxt[f"since_{geo}"] = newest
        return FetchResult(items=jobs, cursor=nxt)

    async def normalize(self, j) -> NormalizedEvent | None:
        where = html.unescape(j.get("jobGeo") or "").replace("  ", " ").strip()
        return _event(self.name, "jobicy", id=str(j.get("id") or j.get("url") or ""), title=html.unescape(j.get("jobTitle") or "").strip(),
                      url=j.get("url") or "", company=html.unescape(j.get("companyName") or "").strip(), restriction=where,
                      open_=open_to_india(where), published=_iso(j.get("pubDate")))

    async def health_check(self) -> bool:
        return True  # a probe is a request, and one pass an hour is the limit

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, permissions="Jobicy API terms: credit Jobicy and link to the source",
                            quota="one automated pass an hour at most, 1 s between requests (poll every 6 h)")


class WorkingNomadsAdapter(SourceAdapter):
    """Working Nomads' exposed_jobs feed: one JSON array of about 40 current jobs, `location` is free text ("Global", "Europe, APAC", "USA only")."""
    connector_type = "api"
    URL = "https://www.workingnomads.com/api/exposed_jobs/"

    def __init__(self, name: str, client: httpx.AsyncClient | None = None):
        self.name, self._client = name, client

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        d = await _get_json(self._client, self.URL)
        if not isinstance(d, list):
            raise ValueError("Working Nomads: expected a JSON array")  # noqa: TRY004 - any failure reads as a failed poll
        return FetchResult(items=d, cursor={"count": len(d)})

    async def normalize(self, j) -> NormalizedEvent | None:
        where = html.unescape(j.get("location") or "").strip()
        return _event(self.name, "workingnomads", id=j.get("url") or "", title=html.unescape(j.get("title") or "").strip(), url=j.get("url") or "",
                      company=html.unescape(j.get("company_name") or "").strip(), restriction=where, open_=open_to_india(where),
                      published=_iso(j.get("pub_date")))

    async def health_check(self) -> bool:
        await self.fetch()
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, permissions="public JSON feed; no terms published (robots.txt allows)", quota="none published (poll every 6 h)")


FLAGS = re.compile("[\U0001F1E6-\U0001F1FF]")  # regional-indicator letters: WWR writes "\U0001F1EE\U0001F1F3 India"


class WeWorkRemotelyAdapter(FeedAdapter):
    """We Work Remotely's RSS: title is "Company: Job title"; <region> is "Anywhere in the World", "USA Only", "Asia Only"; <country> names the exact countries
    when the job is limited to some. The country list, when present, is the stricter signal, so it wins."""

    def __init__(self, name: str, url: str = "https://weworkremotely.com/remote-jobs.rss", client: httpx.AsyncClient | None = None):
        super().__init__(name, url, "global", client)

    async def normalize(self, item) -> NormalizedEvent | None:
        link, title = item.get("link") or "", html.unescape(item.get("title") or "").strip()
        company, _, role = title.partition(": ")
        if not role:
            company, role = None, title
        where = re.sub(r"\s+", " ", FLAGS.sub("", html.unescape(item.get("country") or ""))).strip() or html.unescape(item.get("region") or "").strip()
        pub = item.get("published_parsed")
        return _event(self.name, "weworkremotely", id=item.get("id") or link, title=role, url=link, company=company, restriction=where,
                      open_=open_to_india(where), published=datetime.fromtimestamp(calendar.timegm(pub), UTC) if pub else None)

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(public_feed=True, permissions="public RSS; no terms could be read (robots.txt allows)", quota="none published (poll every 6 h)")
