"""GitHub: new public repositories of watched companies' organisations (a launch signal, never an alert).

Official docs: REST rate limits https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api ,
conditional requests https://docs.github.com/en/rest/using-the-rest-api/best-practices-for-using-the-rest-api ,
list organization repositories https://docs.github.com/en/rest/repos/repos#list-organization-repositories .

One GET /orgs/{org}/repos?sort=created per organisation per run. ETags make an unchanged org a 304, which does not count against the
primary limit when authenticated. Every request first takes a token from the quota manager; a 403/429 that names a limit blocks the
provider until the time GitHub gave (retry-after, then x-ratelimit-reset) and the batch ends with what was collected."""
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import httpx

from app.connectors.base import Capabilities, FetchResult, NormalizedEvent, SourceAdapter
from app.services.quota import RateLimited, cfg

API = "https://api.github.com"


def _iso(s: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


class GithubAdapter(SourceAdapter):
    connector_type = "api"

    def __init__(self, name: str, orgs: list[dict], token: str = "", client: httpx.AsyncClient | None = None, per_run: int | None = None):
        self.name, self.orgs, self.token, self._client = name, orgs, token, client
        self.provider = "github" if token else "github_anon"
        self.per_run = per_run or cfg()["expansion"]["github_orgs_per_run"]

    def _headers(self, etag: str | None) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "startup-intel-engine/0.1"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if etag:
            h["If-None-Match"] = etag
        return h

    async def _limited(self, r: httpx.Response) -> None:
        """403/429 with remaining = 0 or a retry-after is a limit, not a failure. Primary: wait for the reset; secondary: retry-after, else a minute."""
        if r.status_code not in (403, 429):
            return
        if r.headers.get("retry-after"):
            wait = float(r.headers["retry-after"])
        elif r.headers.get("x-ratelimit-remaining") == "0":
            wait = max(float(r.headers.get("x-ratelimit-reset", 0)) - datetime.now(UTC).timestamp(), 1)
        elif "rate limit" in r.text.lower():
            wait = 60.0
        else:
            return
        if self.limiter:
            await self.limiter.block(wait)
        raise RateLimited(wait, "GitHub rate limit")

    async def fetch(self, cursor: dict | None = None) -> FetchResult:
        state = {k: dict(v) for k, v in ((cursor or {}).get("orgs") or {}).items()}
        horizon = (datetime.now(UTC) - timedelta(days=cfg()["expansion"]["github_new_repo_days"])).isoformat()
        order = sorted(self.orgs, key=lambda o: state.get(o["org"].lower(), {}).get("checked_at", ""))[: self.per_run]
        c, items, retry = self._client or httpx.AsyncClient(timeout=20), [], None
        try:
            for o in order:
                key = o["org"].lower()
                st = state.setdefault(key, {})
                try:
                    if self.limiter:
                        await self.limiter.take()
                    r = await c.get(f"{API}/orgs/{o['org']}/repos", params={"sort": "created", "direction": "desc", "per_page": 30, "type": "public"},
                                    headers=self._headers(st.get("etag")))
                    await self._limited(r)
                except RateLimited as rl:
                    retry = rl.wait_s  # keep what was collected so far; the rest waits
                    break
                st["checked_at"] = datetime.now(UTC).isoformat()
                if r.status_code == 304 or 400 <= r.status_code < 500:  # unchanged, or this org is gone/renamed/private: skip it, not the batch
                    continue
                r.raise_for_status()
                since = st.get("since", horizon)
                fresh = [x for x in r.json() if not x.get("fork") and not x.get("archived") and (x.get("created_at") or "") > since]
                items += [{"repo": x, "org": o} for x in fresh]
                if fresh:
                    st["since"] = max(x["created_at"] for x in fresh)
                st.setdefault("since", horizon)
                st["etag"] = r.headers.get("etag")
        finally:
            if self._client is None:
                await c.aclose()
        return FetchResult(items=items, cursor={"orgs": state}, retry_after_s=retry)

    async def normalize(self, item) -> NormalizedEvent | None:
        x, o = item["repo"], item["org"]
        home = urlparse(x["homepage"]).netloc.removeprefix("www.") if x.get("homepage", "") and x["homepage"].startswith("http") else None
        return NormalizedEvent(
            source_name=self.name, source_event_id=f"repo:{x['id']}", source_url=x["html_url"], canonical_url=x["html_url"], event_type="repo",
            title=x["full_name"], content=x.get("description"), published_at=_iso(x.get("created_at")), company_domain=home,
            raw_metadata={"region": "global", "org": o["org"], "company_id": o.get("company_id"), "stars": x.get("stargazers_count"),
                          "language": x.get("language")},
            permissions_metadata={"store": "repository name, description and link (public GitHub data)"})

    async def health_check(self) -> bool:
        return True

    async def get_capabilities(self) -> Capabilities:
        return Capabilities(search=False, history=True, incremental_sync=True, public_feed=True, auth_type="token" if self.token else "none",
                            quota="5,000/h with token, 60/h without", sync_methods=["ETag", "created_at horizon"],
                            permissions="public GitHub REST API")
