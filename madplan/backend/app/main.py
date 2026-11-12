"""MadPlan as a Home Assistant app: the API, the pages, and the background jobs, in one process.

Home Assistant shows the app in its sidebar through ingress. The Supervisor strips its own
path before passing a request on, and tells the app what that path is in ``X-Ingress-Path``.
The pages are built with a placeholder for it, which is filled in as they are served.
"""

from __future__ import annotations

import logging
import mimetypes
import os
import re
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import FileResponse, Response

from app.api import account, admin, auth, home_assistant, household_data, new_recipes, plans
from app.bootstrap import bootstrap
from app.core.config import settings
from app.core.db import session_scope

VERSION = "1.0.0"
# What the frontend is built with in place of the ingress path. See frontend/next.config.mjs.
BASE_PLACEHOLDER = "/__madplan_base__"
STATIC_DIR = Path(os.environ.get("MADPLAN_STATIC_DIR", "/app/static"))
# Files whose text holds the placeholder. Images and fonts are sent as they are.
_TEXT_TYPES = (".html", ".js", ".css", ".json", ".txt", ".webmanifest")
_SAFE_INGRESS = re.compile(r"^/api/hassio_ingress/[A-Za-z0-9_-]+$")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    with session_scope() as db:
        bootstrap(db)
    stop = threading.Event()
    if os.environ.get("MADPLAN_RUN_JOBS", "1") == "1":
        from app.worker import run_in_background

        run_in_background(stop)
    yield
    stop.set()


app = FastAPI(
    title=settings.app_name,
    version=VERSION,
    lifespan=lifespan,
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None,
    openapi_url=None if settings.is_production else "/openapi.json",
)

v1 = APIRouter(prefix="/api/v1")
for module in (auth, account, plans, new_recipes, household_data, admin, home_assistant):
    v1.include_router(module.router)
app.include_router(v1)


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": settings.app_name, "version": VERSION}


def _base(request: Request) -> str:
    """The ingress path, if it looks like one. Anything else would be put into the pages."""
    path = request.headers.get("x-ingress-path", "").rstrip("/")
    return path if _SAFE_INGRESS.match(path) else ""


@lru_cache(maxsize=256)
def _rewritten(path: str, mtime: float, base: str) -> bytes:
    return Path(path).read_bytes().replace(BASE_PLACEHOLDER.encode(), base.encode())


def _find(relative: str) -> Path | None:
    root = STATIC_DIR.resolve()
    for candidate in (relative, f"{relative}/index.html", f"{relative}.html"):
        target = (root / candidate.lstrip("/")).resolve()
        # Nothing outside the built pages, whatever the path asks for.
        if (root in target.parents or target == root) and target.is_file():
            return target
    return None


# HEAD too: Next.js asks for pages that way before a link is followed.
@app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
def pages(path: str, request: Request) -> Response:
    """The built pages, with the ingress path put in where the placeholder is."""
    if path.startswith("api/"):
        return Response(status_code=404)
    target = _find(path or "index.html") or _find("index.html")
    if target is None:
        return Response("MadPlan's pages are not built.", status_code=503)
    if target.suffix not in _TEXT_TYPES:
        return FileResponse(target)
    body = _rewritten(str(target), target.stat().st_mtime, _base(request))
    media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    headers = {"Cache-Control": "no-cache" if target.suffix == ".html" else "public, max-age=31536000, immutable"}
    return Response(body, media_type=media_type, headers=headers)
