"""JobSpy web server: serves the frontend UI and a JSON job-search API.

Run from the repo root:
    .venv\\Scripts\\python.exe frontend\\server.py
Then open http://127.0.0.1:8555

Endpoints:
    GET  /           frontend page (index.html)
    GET  /api/info   server info: boards, defaults, schema version
    POST /api/search run a scrape and return jobs + board errors/warnings
    GET  /api/searches/{sid}/jobs/{jid} full record of one job from a search
    GET  /api/health process liveness
    GET  /api/upwork/login    send the browser to Upwork to sign in
    GET  /api/upwork/callback Upwork returns here after sign-in
    GET  /api/upwork/status   whether this browser has connected Upwork
    GET  /api/upwork/jobs     latest Upwork jobs for the connected account
    POST /api/upwork/logout   disconnect Upwork
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
from pathlib import Path

import uvicorn
from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse
from starlette.routing import Route

import upwork_mcp
from jobspy import scrape_jobs
from jobspy.records import DEFAULT_SITES, FIELDS, PREVIEW, REGIONAL, records

log = logging.getLogger("jobspy-web")

ROOT = Path(__file__).resolve().parent.parent
INDEX = Path(__file__).resolve().parent / "index.html"

HOST = os.environ.get("JOBSPY_HOST", "127.0.0.1")
PORT = int(os.environ.get("JOBSPY_PORT", "8555"))

# seconds before the API answers with a timeout while a scrape keeps running
SCRAPE_TIMEOUT = 180
DESCRIPTION_CAP = 20_000  # chars per job, keeps stored rows and responses small

# full rows of recent searches, so the sidebar can request a job's details
SEARCHES: dict[str, dict[str, dict[str, object]]] = {}
MAX_SEARCHES = 20

# ---- relevance filtering (boards match description words too, which pulls
# unrelated titles like "Graphic Designer" into a keyword search) ----------
TITLE_MODES = ("none", "loose", "strict")
STOP_TOKENS = {
    "the", "and", "of", "with", "for", "a", "an", "to", "in", "on", "or", "as",
    "is", "at", "by", "from", "ii", "iii", "iv", "1", "2", "3", "one", "two",
}
# keyword -> titles that may carry the same intent (e.g. "developer" postings
# for an "engineer" search). State it carefully: "development" jobs must not
# match "engineer". Both keys and values are canonicalized on load.
TITLE_ALTS_RAW: dict[str, set[str]] = {
    "engineer": {"developer", "programmer", "architect", "swe", "sde"},
    "developer": {"engineer", "programmer", "swe", "sde"},
    "programmer": {"engineer", "developer"},
    "analyst": {"analytics", "analysis"},
    "designer": {"design", "ux"},
    "nurse": {"nursing", "rn"},
    "admin": {"administrative", "administrator"},
    "intern": {"internship", "trainee"},
    "teacher": {"tutor", "educator"},
    "writer": {"author", "blog"},
    "consultant": {"advisor"},
}
RELEVANCE_THRESHOLDS = {"loose": 0.5, "strict": 0.999}


def _canon(word: str) -> str:
    word = re.sub(r"[^a-z0-9+#]", "", word.lower())
    if len(word) > 4 and word.endswith("ies"):
        word = word[:-3] + "y"
    elif len(word) > 4 and word.endswith("es"):
        word = word[:-2]
    elif len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        word = word[:-1]
    return word


def _tokens(text: str | None) -> list[str]:
    return [
        tok
        for tok in (_canon(part) for part in re.split(r"[\s/,;:()\[\]-]+", text or ""))
        if len(tok) > 1 and tok not in STOP_TOKENS
    ]


TITLE_ALTS = {
    _canon(k): {_canon(v) for v in vals} for k, vals in TITLE_ALTS_RAW.items()
}


def _pair_match(term: str, tt: str) -> bool:
    """Exact match, shared word stem (engineer/engineering), or synonyms."""
    if term == tt:
        return True
    if len(term) >= 6 and tt.startswith(term):
        return True
    if len(tt) >= 6 and term.startswith(tt):
        return True
    return tt in TITLE_ALTS.get(term, ()) or term in TITLE_ALTS.get(tt, ())


def title_relevance(search_term: str | None, title: str | None) -> float:
    """Share of the search keywords the title covers (synonym-aware)."""
    terms = _tokens(search_term)
    if not terms:
        return 1.0
    title_tokens = set(_tokens(title))
    matched = sum(
        1 for term in terms if any(_pair_match(term, tt) for tt in title_tokens)
    )
    return matched / len(terms)

ALL_SITES = [*DEFAULT_SITES, "bayt", "naukri", "bdjobs", "hellowork"]
JOB_TYPES = (
    "fulltime parttime contract temporary internship perdiem other volunteer"
).split()


class SearchRequest(BaseModel):
    search_term: str | None = Field(default=None, max_length=300)
    location: str | None = Field(default=None, max_length=200)
    site_name: list[str] = Field(default_factory=lambda: list(DEFAULT_SITES))
    results_wanted: int = Field(default=10, ge=1, le=200)
    hours_old: int | None = Field(default=None, ge=0, le=4_320)
    is_remote: bool = False
    job_type: str | None = None
    country_indeed: str = Field(default="usa", max_length=60)
    description: bool = False
    proxy: str | None = Field(default=None, max_length=300)
    title_match: str = Field(default="loose")


def bad_request(message: str, status: int = 400) -> JSONResponse:
    return JSON({"error": message}, status_code=status)


class JSON(JSONResponse):
    """JSONResponse that also handles pandas dates and numpy scalars."""

    def render(self, content: object) -> bytes:
        return json.dumps(
            content,
            default=str,  # str(datetime.date) == ISO 8601
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")


async def api_health(request: Request) -> JSONResponse:  # noqa: ARG001
    return JSON({"ok": True})


async def api_info(request: Request) -> JSONResponse:  # noqa: ARG001
    return JSON(
        {
            "schema": 3,
            "boards": ALL_SITES,
            "regional_boards": REGIONAL,
            "job_types": JOB_TYPES,
            "title_modes": TITLE_MODES,
            "defaults": {
                "site_name": DEFAULT_SITES,
                "results_wanted": 10,
                "country_indeed": "usa",
                "title_match": "loose",
            },
            "limits": {"results_wanted": 200, "hours_old": 4320},
        }
    )


async def api_search(request: Request) -> JSONResponse:
    raw = await request.body()
    if not raw:
        return bad_request("send JSON like {\"search_term\": \"nurse\"}")
    try:
        req = SearchRequest.model_validate(json.loads(raw))
    except (ValueError, ValidationError) as err:
        return bad_request(str(err))

    unknown = set(req.site_name) - set(ALL_SITES)
    if unknown:
        return bad_request(
            f"unknown board(s): {sorted(unknown)}. use {ALL_SITES}"
        )
    if req.job_type and req.job_type not in JOB_TYPES:
        return bad_request(f"job_type must be one of {JOB_TYPES}")
    if req.title_match not in TITLE_MODES:
        return bad_request(f"title_match must be one of {TITLE_MODES}")
    if not req.search_term and not req.location and not req.is_remote:
        return bad_request("give a search term (or a location, or remote)")

    kwargs: dict[str, object] = {
        "site_name": req.site_name,
        "search_term": req.search_term,
        "location": req.location,
        "results_wanted": req.results_wanted,
        "is_remote": req.is_remote,
        "country_indeed": req.country_indeed,
        "verbose": 0,
    }
    if req.hours_old is not None:
        kwargs["hours_old"] = req.hours_old
    if req.job_type:
        kwargs["job_type"] = req.job_type
    if req.proxy and req.proxy.strip():
        kwargs["proxies"] = [req.proxy.strip()]

    started = time.monotonic()
    try:
        jobs = await asyncio.wait_for(
            asyncio.to_thread(scrape_jobs, **kwargs), SCRAPE_TIMEOUT
        )
    except asyncio.TimeoutError:
        log.warning("search timed out after %ss", SCRAPE_TIMEOUT)
        return JSON({"error": f"search did not finish in {SCRAPE_TIMEOUT}s; try fewer boards"},
                    status_code=504)
    except ValueError as err:  # jobspy raises ValueError for bad params
        return bad_request(str(err))
    except Exception as err:  # noqa: BLE001 - surfaced to the browser
        log.exception("search crashed")
        return bad_request(f"search failed: {err}", status=500)

    preview_names = [*PREVIEW, "id"]
    search_id = secrets.token_urlsafe(8)
    full_rows = records(jobs, FIELDS)

    # drop postings whose title doesn't relate to the searched keywords —
    # boards (Indeed notably) also match words inside the description
    filtered: dict[str, object] | None = None
    if req.title_match != "none" and req.search_term:
        threshold = RELEVANCE_THRESHOLDS[req.title_match]
        kept: list[dict[str, object]] = []
        hidden_by_site: dict[str, int] = {}
        examples: list[str] = []
        for row in full_rows:
            if title_relevance(req.search_term, str(row.get("title") or "")) >= threshold:
                kept.append(row)
                continue
            site = str(row.get("site") or "?")
            hidden_by_site[site] = hidden_by_site.get(site, 0) + 1
            if len(examples) < 6 and row.get("title"):
                examples.append(str(row["title"]))
        filtered = {
            "mode": req.title_match,
            "hidden": len(full_rows) - len(kept),
            "by_site": hidden_by_site,
            "examples": examples,
        }
        full_rows = kept

    by_id: dict[str, dict[str, object]] = {}
    for n, row in enumerate(full_rows):
        job_id = str(row.get("id") or f"row{n}")
        row["id"] = job_id
        desc = row.get("description")
        if isinstance(desc, str) and len(desc) > DESCRIPTION_CAP:
            row["description"] = desc[:DESCRIPTION_CAP]
        by_id[job_id] = row
    SEARCHES[search_id] = by_id
    while len(SEARCHES) > MAX_SEARCHES:
        SEARCHES.pop(next(iter(SEARCHES)))
    previews = [{k: row[k] for k in preview_names if k in row} for row in full_rows]

    site_counts: dict[str, int] = {}
    for row in full_rows:
        site = str(row.get("site") or "unknown")
        site_counts[site] = site_counts.get(site, 0) + 1
    return JSON(
        {
            "schema": 3,
            "search_id": search_id,
            "count": len(full_rows),
            "counts": site_counts,
            "filtered": filtered,
            "errors": jobs.attrs.get("errors") or None,
            "warnings": jobs.attrs.get("warnings") or None,
            "elapsed_s": round(time.monotonic() - started, 1),
            "jobs": previews,
        }
    )


async def api_job(request: Request) -> JSONResponse:
    """Full record of one job kept from a recent search (for the detail sidebar)."""
    sid = request.path_params["sid"]
    jid = str(request.path_params["jid"])
    row = SEARCHES.get(sid, {}).get(jid)
    if row is None:
        return JSON({"error": "job or search not in memory; run the search again"},
                    status_code=404)
    return JSON(row)


async def index_page(request: Request) -> FileResponse:  # noqa: ARG001
    return FileResponse(INDEX, media_type="text/html")


def _upwork_back(message: str) -> RedirectResponse:
    return RedirectResponse("/?upwork_error=" + urllib.parse.quote(message), status_code=302)


async def upwork_status(request: Request) -> JSONResponse:  # noqa: ARG001
    return JSON(upwork_mcp.connected())


async def upwork_login(request: Request) -> RedirectResponse:
    try:
        url = await asyncio.to_thread(upwork_mcp.begin_login, request.url.netloc, PORT)
    except upwork_mcp.UpworkError as err:
        return _upwork_back(err.message)
    return RedirectResponse(url, status_code=302)


async def upwork_callback(request: Request) -> RedirectResponse:
    denied = request.query_params.get("error_description") or request.query_params.get("error")
    if denied:
        return _upwork_back(str(denied))
    code = request.query_params.get("code") or ""
    state = request.query_params.get("state") or ""
    if not code or not state:
        return _upwork_back("Upwork did not return a sign-in code.")
    try:
        await asyncio.to_thread(upwork_mcp.finish_login, code, state)
    except upwork_mcp.UpworkError as err:
        return _upwork_back(err.message)
    return RedirectResponse("/?upwork=connected", status_code=302)


async def upwork_jobs(request: Request) -> JSONResponse:
    query = (request.query_params.get("q") or "").strip()
    try:
        limit = int(request.query_params.get("limit") or "15")
    except ValueError:
        return bad_request("limit must be a number")
    limit = max(1, min(limit, 25))
    fallback = request.query_params.get("fallback") == "1"
    try:
        data = await asyncio.to_thread(upwork_mcp.latest_jobs, query, limit, fallback)
    except upwork_mcp.UpworkError as err:
        return JSON({"error": err.message}, status_code=err.status)
    return JSON(data)


async def upwork_logout(request: Request) -> JSONResponse:  # noqa: ARG001
    await asyncio.to_thread(upwork_mcp.disconnect)
    return JSON({"connected": False})


routes = [
    Route("/", index_page),
    Route("/api/health", api_health),
    Route("/api/info", api_info),
    Route("/api/search", api_search, methods=["POST"]),
    Route("/api/searches/{sid}/jobs/{jid}", api_job),
    Route("/api/upwork/status", upwork_status),
    Route("/api/upwork/login", upwork_login),
    Route("/api/upwork/callback", upwork_callback),
    Route("/api/upwork/jobs", upwork_jobs),
    Route("/api/upwork/logout", upwork_logout, methods=["POST"]),
]

app = Starlette(routes=routes)


def main() -> None:
    logging.basicConfig(
        format="%(levelname)s: %(name)s: %(message)s", level=logging.INFO
    )
    print(f"JobSpy UI -> http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
