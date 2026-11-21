"""The one guard every dashboard write goes through (decision 100). The API has no login (decision 71), so a write must prove it came from the
dashboard in the owner's browser, not from another web page or a DNS-rebinding trick:
  - the Host header names this machine (127.0.0.1, localhost or ::1), which defeats DNS rebinding;
  - the Origin header is present and is one of CORS_ORIGINS (a browser sets it on every cross-origin write and a web page cannot forge it);
  - a body must be JSON (`Content-Type: application/json`), which a plain HTML form cannot send without a CORS preflight.
Not a login: a local process can still send these headers, which is the same trust as direct database access on this machine."""
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from app.core.config import get_settings

LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host(header: str) -> str:
    return urlsplit(f"//{header}").hostname or ""


async def local_write(request: Request) -> None:
    """FastAPI dependency: `dependencies=[Depends(local_write)]` on every POST, PUT, PATCH and DELETE route."""
    if _host(request.headers.get("host", "")) not in LOCAL_HOSTS:
        raise HTTPException(403, "write refused: not addressed to this machine")
    if request.headers.get("origin") not in get_settings().cors_list:
        raise HTTPException(403, "write refused: Origin is missing or not allowed")
    if request.method in {"POST", "PUT", "PATCH"} and request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        raise HTTPException(415, "write refused: JSON only")
