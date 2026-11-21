"""Five more ATS vendors for AtsAdapter: Keka, Darwinbox, TurboHire, Zoho Recruit (India) and SmartRecruiters.

Ported from career-ops providers/{keka,darwinbox,turbohire,zoho-recruit,smartrecruiters}.mjs (MIT, reference only; nothing imported or run). The four
Indian vendors publish no API: each endpoint is the zero-auth call the vendor's own careers page makes for every visitor (decision 102). Each vendor
has a token (what identifies the board), a public board URL, a detect regex for careers pages, and a fetch that returns parsed rows. Every request
goes to a host built from the validated token, never to a URL taken from the page, and a redirect is an error, not a hop (career-ops redirect:'error').
Descriptions are not kept, as with the other ATS routes (permissions_metadata: title, location, team and link only)."""
import asyncio
import html
import json
import re
from datetime import UTC, datetime
from typing import NamedTuple
from urllib.parse import quote, unquote

import httpx

from app.connectors.feed import USER_AGENT
from app.services.safefetch import guarded_client

PAGE_PAUSE = 0.25  # seconds between pages of one board


class BoardError(RuntimeError):
    """The board answered, but not in the shape this vendor documents or shows: gone, moved, or changed. Never read as an empty board."""


class Listing(NamedTuple):
    rows: list[dict]  # {id, title, url, loc, pub, team, workplace?}
    complete: bool = True  # False: the walk stopped early, so a job missing from rows is not known to be closed


async def _send(client: httpx.AsyncClient | None, method: str, url: str, **kw) -> httpx.Response:
    c = client or guarded_client(timeout=20)
    try:
        r = await c.request(method, url, headers={"User-Agent": USER_AGENT, **kw.pop("headers", {})}, follow_redirects=False, **kw)
    finally:
        if client is None:
            await c.aclose()
    if 300 <= r.status_code < 400:
        raise BoardError(f"{url} redirected ({r.status_code} to {r.headers.get('location')}): no such board, or the vendor moved it")
    r.raise_for_status()
    return r


def _clean(s) -> str:
    return re.sub(r"\s+", " ", html.unescape(s)).replace(" ,", ",").strip() if isinstance(s, str) else ""


def _when(v) -> datetime | None:
    try:
        if isinstance(v, (int, float)) and v > 0:
            return datetime.fromtimestamp(v, UTC)
        d = datetime.fromisoformat(v) if isinstance(v, str) and v else None
        return d if d is None or d.tzinfo else d.replace(tzinfo=UTC)
    except ValueError:
        return None


def _seg(raw) -> str | None:
    """A posting id as one URL path segment, or None when it cannot be one (a lone surrogate)."""
    s = str(raw).strip() if raw is not None else ""
    try:
        return quote(s, safe="") if s else None
    except UnicodeEncodeError:
        return None


class Vendor:
    name: str
    detect: re.Pattern
    docs: str
    poll = 21600  # seconds; slow, because the Indian endpoints are unpublished
    reserved: frozenset = frozenset()

    def token(self, m: re.Match) -> str | None:
        t = m.group(1).lower()
        return None if t in self.reserved else t

    def check(self, token: str) -> str:
        raise NotImplementedError

    def board_url(self, token: str) -> str:
        raise NotImplementedError

    async def fetch(self, client: httpx.AsyncClient | None, token: str) -> Listing:
        raise NotImplementedError


class Keka(Vendor):
    name = "keka"
    detect = re.compile(r"(?<![\w.-])([a-z0-9](?:[a-z0-9-]*[a-z0-9])?)\.keka\.com\b", re.IGNORECASE)
    docs = "none published; the careers widget feed https://<tenant>.keka.com/careers/api/embedjobs (decision 102)"
    reserved = frozenset({"www", "app", "api", "help", "support", "blog", "docs", "status", "mail", "developers"})
    ID = re.compile(r"/ats/documents/([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/careerportal/", re.IGNORECASE)

    def check(self, token):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", token) or token in self.reserved:
            raise ValueError(f"not a Keka tenant: {token!r}")
        return f"https://{token}.keka.com"

    def board_url(self, token):
        return self.check(token) + "/careers"

    async def fetch(self, client, token):
        origin = self.check(token)
        shell = (await _send(client, "GET", f"{origin}/careers")).text
        m = self.ID.search(shell)
        if not m:  # fail closed: a missing identifier means Keka changed the page, not that the board is empty
            raise BoardError(f"keka:{token}: careers page has no embedjobs identifier (page changed?)")
        data = (await _send(client, "GET", f"{origin}/careers/api/embedjobs/default/active/{m.group(1).lower()}")).json()
        return Listing(self.parse(data, origin))

    @staticmethod
    def parse(data, origin: str) -> list[dict]:
        if not isinstance(data, list):
            raise BoardError(f"keka: expected a jobs array, got {type(data).__name__}")
        rows = []
        for j in data:
            seg = _seg(j.get("id")) if isinstance(j, dict) else None
            title = _clean(j.get("title")) if seg else ""
            if not title:
                continue
            places = []
            for loc in j.get("jobLocations") or []:
                p = ", ".join(x for x in (_clean(loc.get("city")), _clean(loc.get("countryName"))) if x) or _clean(loc.get("name"))
                if p and p not in places:
                    places.append(p)
            rows.append({"id": str(j["id"]), "title": title, "url": f"{origin}/careers/jobdetails/{seg}", "loc": "; ".join(places) or None,
                         "pub": _when(j.get("publishedOn")), "team": None})
        return rows


class Darwinbox(Vendor):
    name = "darwinbox"
    # the token is "<tenant host>/<company id>"
    detect = re.compile(r"([a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.darwinbox\.(?:in|com))/ms/candidatev2/([a-z0-9_-]+)/careers", re.IGNORECASE)
    docs = "none published; the careers page call POST /ms/candidateapi/job/alljobs (decision 102)"
    PAGE, MAX_PAGES = 50, 20

    def token(self, m):
        return f"{m.group(1).lower()}/{m.group(2)}"

    def check(self, token):
        host, _, cid = token.partition("/")
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.darwinbox\.(?:in|com)", host) or not re.fullmatch(r"[A-Za-z0-9_-]+", cid):
            raise ValueError(f"not a Darwinbox board: {token!r}")
        return f"https://{host}"

    def board_url(self, token):
        return f"{self.check(token)}/ms/candidatev2/{token.partition('/')[2]}/careers/allJobs"

    async def fetch(self, client, token):
        origin, cid = self.check(token), token.partition("/")[2]
        rows, seen, complete = [], set(), False
        for page in range(1, self.MAX_PAGES + 1):
            if page > 1:
                await asyncio.sleep(PAGE_PAUSE)
            try:
                r = await _send(client, "POST", f"{origin}/ms/candidateapi/job/alljobs?companyId={cid}", headers={"Accept": "application/json"},
                                json={"companyId": cid, "page": page, "sort_option": "new", "limit": self.PAGE})
                data = r.json()
                if data.get("status") != "success" or not isinstance(data.get("data"), list):
                    raise BoardError(f"darwinbox:{token}: expected status success and a data array")
            except (httpx.HTTPError, BoardError, ValueError):
                if page == 1:  # the first page failing means broken, not empty
                    raise
                break  # keep what was read, and say the listing is partial
            for row in self.parse(data["data"], origin, cid):
                if row["url"] not in seen:
                    seen.add(row["url"])
                    rows.append(row)
            if len(data["data"]) < self.PAGE:  # only a short page proves the board ended
                complete = True
                break
        return Listing(rows, complete)

    @staticmethod
    def parse(data: list, origin: str, cid: str) -> list[dict]:
        rows = []
        for j in data:
            seg = _seg(j.get("id")) if isinstance(j, dict) else None
            title = _clean(j.get("title")) if seg else ""
            if not title:
                continue
            tips = j.get("tool_tip_locations") if isinstance(j.get("tool_tip_locations"), list) and j.get("tool_tip_locations") else [j.get("locations")]
            places = list(dict.fromkeys(p for p in map(_clean, tips) if p))
            rows.append({"id": str(j["id"]), "title": title, "url": f"{origin}/ms/candidatev2/{cid}/careers/jobDetails/{seg}",
                         "loc": "; ".join(places) or None, "pub": _when(j.get("posted_on")) or _when(j.get("created_on")), "team": None})
        return rows


class TurboHire(Vendor):
    name = "turbohire"
    detect = re.compile(r"(?<![\w.-])([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)\.turbohire\.co\b", re.IGNORECASE)
    docs = "none published; the careers page calls https://thapi.azurewebsites.net/api/careerpagev2/filteredjobs (decision 102)"
    reserved = frozenset({"app", "api", "www"})
    API = "https://thapi.azurewebsites.net"
    GUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)

    def check(self, token):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", token) or token in self.reserved:
            raise ValueError(f"not a TurboHire tenant: {token!r}")
        return f"https://{token}.turbohire.co"

    def board_url(self, token):
        return self.check(token) + "/"

    async def fetch(self, client, token):
        origin = self.check(token)
        # The token call answers 403 unless it carries the careers page's own Origin and Referer, which a visitor's browser sends with it.
        tok = (await _send(client, "GET", f"{self.API}/api/token/noauth", headers={"Origin": origin, "Referer": origin + "/"})).json().get("access_token")
        if not isinstance(tok, str) or not tok.strip():
            raise BoardError(f"turbohire:{token}: no anonymous access_token")
        auth = {"Authorization": f"Bearer {tok.strip()}"}
        org = (await _send(client, "GET", f"{self.API}/api/publicorganizations?accountName={token}", headers=auth)).json().get("OrgID")
        if not isinstance(org, str) or not self.GUID.fullmatch(org) or set(org) <= {"0", "-"}:
            raise BoardError(f"turbohire:{token}: no organisation id")
        # pageType 0 is the public careers page, the value the page itself sends; other values show boards a visitor does not see.
        data = (await _send(client, "POST", f"{self.API}/api/careerpagev2/filteredjobs?orgId={org}&pageType=0", headers=auth, json={})).json()
        return Listing(self.parse(data, origin))

    @staticmethod
    def parse(data, origin: str) -> list[dict]:
        if not isinstance(data, dict) or not isinstance(data.get("Result"), list):
            raise BoardError("turbohire: expected a Result array")
        rows, seen = [], set()
        for j in data["Result"]:
            if not isinstance(j, dict) or not j.get("JobIdObfuscated"):
                continue
            seg = _seg(unquote(str(j["JobIdObfuscated"])))  # shipped percent-encoded: decode once, encode once
            title = _clean(j.get("JobTitle"))
            if not seg or not title or seg in seen:
                continue
            seen.add(seg)
            sites = j.get("Location")
            try:
                sites = json.loads(sites) if isinstance(sites, str) and sites.strip().startswith(("[", "{")) else sites
            except ValueError:
                sites = None
            places = list(dict.fromkeys(p for p in (_clean(s.get("Address")) for s in sites if isinstance(s, dict)) if p)) if isinstance(sites, list) \
                else [_clean(sites)] if isinstance(sites, str) else []
            rows.append({"id": unquote(str(j["JobIdObfuscated"])), "title": title, "url": f"{origin}/job/publicjobs/{seg}",
                         "loc": "; ".join(places) or None, "pub": _when(j.get("PublishedDate")), "team": None})
        return rows


class ZohoRecruit(Vendor):
    name = "zoho_recruit"
    HOST = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.zohorecruit\.(?:com\.au|com\.cn|com|ca|eu|in|jp|sa)"
    detect = re.compile(r"(?<![\w.-])(" + HOST + r")\b", re.IGNORECASE)
    docs = "none published; the careers site https://<tenant>.zohorecruit.<tld>/jobs/Careers (decision 102)"
    TAG = re.compile(r"""<input\b(?:[^>"']|"[^"]*"|'[^']*')*>""", re.IGNORECASE)

    def check(self, token):
        if not re.fullmatch(self.HOST, token) or token.split(".")[0] in {"www", "app", "api", "help", "support", "blog", "docs", "status", "mail"}:
            raise ValueError(f"not a Zoho Recruit tenant: {token!r}")
        return f"https://{token}"

    def board_url(self, token):
        return self.check(token) + "/jobs/Careers"

    async def fetch(self, client, token):
        origin = self.check(token)
        return Listing(self.parse((await _send(client, "GET", origin + "/jobs/Careers")).text, origin))

    @classmethod
    def parse(cls, page: str, origin: str) -> list[dict]:
        value = None
        for tag in cls.TAG.findall(page or ""):
            if re.search(r"""\sid\s*=\s*["']?jobs["'\s/>]""", tag, re.IGNORECASE):
                m = re.search(r"""\svalue\s*=\s*(?:"([^"]*)"|'([^']*)')""", tag, re.IGNORECASE)
                value = (m.group(1) if m and m.group(1) is not None else m.group(2) if m else "") or ""
                break
        if value is None:  # an unknown tenant, or one with only the legacy career site: broken, not empty
            raise BoardError("zoho_recruit: no id=\"jobs\" input on /jobs/Careers")
        try:
            records = json.loads(html.unescape(value))
        except ValueError as e:
            raise BoardError(f"zoho_recruit: the jobs value is not JSON ({e})") from e
        if not isinstance(records, list):
            raise BoardError("zoho_recruit: the jobs value is not an array")
        rows, seen = [], set()
        for j in records:
            if not isinstance(j, dict) or j.get("Is_Locked") is True or j.get("Publish") is False:  # shown as filled
                continue
            seg, title = _seg(j.get("id")), _clean(j.get("Posting_Title")) or _clean(j.get("Job_Opening_Name"))
            if not seg or not title or seg in seen:
                continue
            seen.add(seg)
            parts = [p for p in (_clean(j.get("City")), _clean(j.get("State")), _clean(j.get("Country"))) if p]
            if j.get("Remote_Job") is True:
                parts.append("Remote")
            rows.append({"id": str(j["id"]), "title": title, "url": f"{origin}/jobs/Careers/{seg}", "loc": ", ".join(dict.fromkeys(parts)) or None,
                         "pub": _when(j.get("Date_Opened")), "team": None})
        return rows


class SmartRecruiters(Vendor):
    name = "smartrecruiters"
    detect = re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/([A-Za-z0-9][\w-]*)", re.IGNORECASE)
    docs = "https://developers.smartrecruiters.com/docs/posting-api"
    reserved = frozenset({"oneclick-ui", "company", "api", "static", "assets", "embed"})
    poll = 3600
    PAGE, MAX_PAGES = 100, 50

    def token(self, m):
        t = m.group(1)
        return None if t.lower() in self.reserved else t

    def check(self, token):
        if not re.fullmatch(r"[A-Za-z0-9][\w-]*", token) or token.lower() in self.reserved:
            raise ValueError(f"not a SmartRecruiters company: {token!r}")
        return token

    def board_url(self, token):
        return f"https://careers.smartrecruiters.com/{self.check(token)}"

    async def fetch(self, client, token):
        slug = self.check(token)
        rows, seen, complete = [], set(), False
        for page in range(self.MAX_PAGES):
            if page:
                await asyncio.sleep(PAGE_PAUSE)
            data = (await _send(client, "GET", f"https://api.smartrecruiters.com/v1/companies/{slug}/postings",
                                params={"limit": self.PAGE, "offset": page * self.PAGE, "status": "PUBLIC"})).json()
            if not isinstance(data, dict) or not isinstance(data.get("content"), list):
                raise BoardError("smartrecruiters: expected content[]")
            for row in self.parse(data["content"], slug):
                if row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
            if len(data["content"]) < self.PAGE:
                complete = True
                break
        return Listing(rows, complete)

    @staticmethod
    def parse(content: list, slug: str) -> list[dict]:
        rows = []
        for j in content:
            seg = _seg(j.get("id")) if isinstance(j, dict) else None
            title = _clean(j.get("name")) if seg else ""
            if not title:
                continue
            loc = j.get("location") if isinstance(j.get("location"), dict) else {}
            place = _clean(loc.get("fullLocation")) or ", ".join(p for p in map(_clean, (loc.get("city"), loc.get("region"), loc.get("country"))) if p)
            where = ", ".join(p for p in (place, "Remote" if loc.get("remote") else "") if p)
            slugged = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
            dept = j.get("department") if isinstance(j.get("department"), dict) else {}
            rows.append({"id": str(j["id"]), "title": title, "url": f"https://jobs.smartrecruiters.com/{slug}/{seg}" + (f"-{slugged}" if slugged else ""),
                         "loc": where or None, "pub": _when(j.get("releasedDate")), "team": _clean(dept.get("label")) or None})
        return rows


VENDORS: dict[str, Vendor] = {v.name: v for v in (Keka(), Darwinbox(), TurboHire(), ZohoRecruit(), SmartRecruiters())}
