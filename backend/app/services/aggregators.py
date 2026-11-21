"""Job aggregators (config/aggregators.txt, ported from career-ops): sites that repost other employers' listings. Discovery and ATS detection never
treat one as a company's own careers page or website (J2, decision 102)."""
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

PATH = Path(__file__).resolve().parents[3] / "config" / "aggregators.txt"


@lru_cache(maxsize=1)
def domains() -> frozenset[str]:
    lines = (ln.split("#", 1)[0].strip().lower() for ln in PATH.read_text().splitlines())
    return frozenset(ln for ln in lines if ln)


def is_aggregator(url_or_host: str | None) -> bool:
    """True for a listed domain or any subdomain of one (jobs.zippia.com), never for a lookalike (notzippia.com)."""
    raw = (url_or_host or "").strip().lower()
    host = (urlsplit(raw).hostname if "//" in raw else raw.split("/")[0]) or ""
    host = host.rstrip(".")
    return any(host == d or host.endswith("." + d) for d in domains())
