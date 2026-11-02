"""Upwork job source, backed by Upwork's official MCP server.

Unlike the other boards this does not scrape HTML: it logs in to *your* Upwork
account once (OAuth, see ``python -m jobspy.upwork login``) and calls the
server's job-search tool as you. It therefore returns what your freelancer
account can see, and is meant for personal monitoring, not for republishing.

Upwork does not document its tool names/arguments on the public page, so the
search tool is discovered from ``tools/list`` and its arguments are built from
the tool's own JSON schema. Override with the environment variables
``UPWORK_MCP_SEARCH_TOOL`` (exact tool name) and ``UPWORK_MCP_SEARCH_ARGS``
(JSON merged into the call arguments), and inspect with
``python -m jobspy.upwork tools``.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from jobspy.model import (
    CompensationInterval,
    JobPost,
    JobType,
    Location,
    Scraper,
    ScraperInput,
)
from jobspy.upwork.auth import MCP_URL, NOT_LOGGED_IN, FileTokenStorage, NotLoggedIn

if TYPE_CHECKING:
    from mcp import Client
    from mcp_types import CallToolResult, Tool

__all__ = ["Upwork"]

# --------------------------------------------------------------------------
# connection
# --------------------------------------------------------------------------


@asynccontextmanager
async def connect(interactive: bool = False) -> AsyncIterator[Client]:
    """Open an authenticated MCP client against the Upwork server."""
    from mcp import Client
    from mcp.client.streamable_http import streamable_http_client
    from mcp.shared._httpx_utils import create_mcp_http_client

    from jobspy.upwork.auth import build_provider

    http = create_mcp_http_client(auth=build_provider(interactive))
    async with http:
        async with Client(streamable_http_client(MCP_URL, http_client=http)) as client:
            yield client


async def list_all_tools(client: Client) -> list[Tool]:
    tools: list[Tool] = []
    cursor: str | None = None
    for _ in range(20):
        page = await client.list_tools(cursor=cursor)
        tools.extend(page.tools)
        cursor = page.next_cursor
        if not cursor:
            break
    return tools


# --------------------------------------------------------------------------
# picking the search tool and building its arguments
# --------------------------------------------------------------------------

# words that mark a tool as belonging to another workflow (hiring, contracts…)
_NOT_JOB_SEARCH = (
    "talent", "freelancer", "contract", "proposal", "offer", "message",
    "milestone", "invitation", "invite", "profile", "earning", "timesheet",
    "saved", "favorite", "favourite", "attachment", "list_members", "dashboard",
)  # fmt: skip


def pick_search_tool(tools: list[Tool]) -> Tool:
    """Return the freelancer-side 'search jobs' tool, or raise with the tool list."""
    wanted = os.environ.get("UPWORK_MCP_SEARCH_TOOL")
    if wanted:
        for tool in tools:
            if tool.name == wanted:
                return tool
        raise RuntimeError(
            f"UPWORK_MCP_SEARCH_TOOL={wanted!r} not found; "
            f"server offers: {', '.join(t.name for t in tools)}"
        )

    def score(tool: Tool) -> int:
        name = tool.name.lower()
        text = f"{name} {(tool.description or '').lower()}"
        points = 0
        points += 4 if "job" in name else 0
        points += (
            3 if any(w in name for w in ("search", "find", "list", "browse")) else 0
        )
        points += 1 if "job" in text and "search" in text else 0
        points -= 6 if any(w in name for w in _NOT_JOB_SEARCH) else 0
        return points

    ranked = sorted(tools, key=score, reverse=True)
    if not ranked or score(ranked[0]) < 6:
        raise RuntimeError(
            "could not identify the job-search tool. Run `python -m jobspy.upwork "
            "tools`, then set UPWORK_MCP_SEARCH_TOOL to the right name. Server "
            f"offers: {', '.join(t.name for t in tools)}"
        )
    return ranked[0]


_QUERY_KEYS = (
    "query",
    "q",
    "keywords",
    "keyword",
    "search",
    "search_term",
    "term",
    "text",
)
_LIMIT_KEYS = (
    "limit",
    "page_size",
    "pagesize",
    "per_page",
    "first",
    "count",
    "max_results",
    "size",
)
_SORT_KEYS = ("sort", "sort_by", "sortby", "order_by", "orderby", "sort_order")
_FRESH = re.compile(r"recen|newest|latest|date|fresh|time", re.I)


def _match_key(props: dict[str, Any], candidates: tuple[str, ...]) -> str | None:
    lowered = {key.lower().replace("-", "_"): key for key in props}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def build_arguments(tool: Tool, scraper_input: ScraperInput) -> dict[str, Any]:
    """Map our search intent onto whatever the tool's JSON schema calls things."""
    schema = tool.input_schema or {}
    props: dict[str, Any] = schema.get("properties") or {}
    args: dict[str, Any] = {}

    if query := scraper_input.search_term:
        if key := _match_key(props, _QUERY_KEYS):
            args[key] = [query] if props[key].get("type") == "array" else query
    if key := _match_key(props, _LIMIT_KEYS):
        args[key] = scraper_input.results_wanted
    if key := _match_key(props, _SORT_KEYS):
        options = props[key].get("enum") or []
        fresh = next(
            (o for o in options if isinstance(o, str) and _FRESH.search(o)), None
        )
        if fresh:
            args[key] = fresh  # newest first: this is what early alerts need

    if extra := os.environ.get("UPWORK_MCP_SEARCH_ARGS"):
        args.update(json.loads(extra))
    return args


# --------------------------------------------------------------------------
# turning the tool's reply into JobPost rows
# --------------------------------------------------------------------------

_LIST_KEYS = (
    "jobs",
    "results",
    "items",
    "data",
    "matches",
    "listings",
    "edges",
    "nodes",
)


def _payload(result: CallToolResult) -> Any:  # noqa: ANN401
    from mcp_types import TextContent

    if result.structured_content is not None:
        return result.structured_content
    texts = [b.text for b in result.content if isinstance(b, TextContent)]
    for text in texts:
        try:
            return json.loads(text)
        except ValueError:
            continue
    raise RuntimeError(
        "search tool did not return JSON, so it can't be mapped. First 300 chars: "
        + (texts[0][:300] if texts else "<empty>")
    )


def extract_items(
    payload: Any,  # noqa: ANN401
    depth: int = 0,
) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [p.get("node", p) for p in payload if isinstance(p, dict)]
    if isinstance(payload, dict) and depth < 4:
        for key in _LIST_KEYS:
            if key in payload:
                found = extract_items(payload[key], depth + 1)
                if found or isinstance(payload[key], list):
                    return found
        for value in payload.values():
            if isinstance(value, (dict, list)):
                found = extract_items(value, depth + 1)
                if found:
                    return found
    return []


def _first(raw: dict[str, Any], *keys: str) -> Any:  # noqa: ANN401
    lowered = {k.lower().replace("_", ""): v for k, v in raw.items()}
    for key in keys:
        value = lowered.get(key.lower().replace("_", ""))
        if value not in (None, "", [], {}):
            return value
    return None


def _number(value: Any) -> float | None:  # noqa: ANN401
    if isinstance(value, dict):
        value = _first(value, "amount", "value", "displayValue")
    try:
        return (
            float(str(value).replace(",", "").lstrip("$"))
            if value is not None
            else None
        )
    except ValueError:
        return None


def _when(value: Any) -> datetime | None:  # noqa: ANN401
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            seconds = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def _names(value: Any) -> list[str] | None:  # noqa: ANN401
    if not isinstance(value, list):
        return None
    out = [
        (
            v
            if isinstance(v, str)
            else str(_first(v, "name", "label", "prettyName") or "")
        )
        for v in value
    ]
    return [n for n in out if n] or None


def to_job_post(raw: dict[str, Any]) -> JobPost | None:
    title = _first(raw, "title", "job_title", "name")
    if not title:
        return None
    job_id = _first(raw, "id", "job_id", "ciphertext", "uid", "cipher")
    url = _first(raw, "url", "job_url", "link", "href")
    if not url:
        ref = str(job_id or "")
        url = (
            f"https://www.upwork.com/jobs/{ref}"
            if ref.startswith("~")
            else f"https://www.upwork.com/nx/search/jobs/?q={title}"
        )

    client = _first(raw, "client", "buyer") or {}
    client = client if isinstance(client, dict) else {}
    hourly_min = _number(
        _first(raw, "hourly_min", "hourly_rate_min", "hourlyBudgetMin")
    )
    hourly_max = _number(
        _first(raw, "hourly_max", "hourly_rate_max", "hourlyBudgetMax")
    )
    fixed = _number(_first(raw, "budget", "fixed_price", "fixed_budget", "amount"))
    hourly = hourly_min is not None or hourly_max is not None
    posted = _when(
        _first(raw, "posted_on", "created_on", "published_on", "date_posted",
               "created_at", "posted_at", "publishTime", "createdDateTime", "published")
    )  # fmt: skip

    return JobPost(
        id=f"upwork-{job_id}" if job_id else None,
        job_url=str(url),
        title=str(title),
        company=_first(client, "company_name", "name", "companyName"),
        location=Location(country=_first(client, "country") or _first(raw, "country")),
        date_posted=posted.date() if posted else None,
        job_type=[JobType.CONTRACT],
        interval=CompensationInterval.HOURLY if hourly else None,
        min_amount=hourly_min if hourly else fixed,
        max_amount=hourly_max if hourly else fixed,
        currency=str(_first(raw, "currency", "currency_code") or "USD"),
        is_remote=True,
        job_level=_first(raw, "experience_level", "contractor_tier", "tier"),
        listing_type="hourly" if hourly else ("fixed-price" if fixed else None),
        description=_first(raw, "description", "summary", "snippet", "content"),
        skills=_names(_first(raw, "skills", "required_skills", "tags")),
    )


# --------------------------------------------------------------------------
# the scraper
# --------------------------------------------------------------------------


def _leaf_message(exc: BaseException) -> str:
    """Unwrap (nested) exception groups raised out of anyio task groups."""
    inner = getattr(exc, "exceptions", None)
    if inner:
        return _leaf_message(inner[0])
    return str(exc) or type(exc).__name__


class Upwork(Scraper):
    def scrape(self, scraper_input: ScraperInput) -> list[JobPost]:
        if not FileTokenStorage().logged_in():
            self.fail(NOT_LOGGED_IN)
            return []
        try:
            return asyncio.run(self._scrape(scraper_input))
        except ImportError as err:
            self.fail(
                f"{err}. Install the MCP client: pip install 'python-jobspy[upwork]'"
            )
        except NotLoggedIn as err:
            self.fail(str(err))
        except Exception as err:  # noqa: BLE001 - reported per board, like the others
            self.fail(_leaf_message(err))
        return []

    async def _scrape(self, scraper_input: ScraperInput) -> list[JobPost]:
        async with connect(interactive=False) as client:
            tool = pick_search_tool(await list_all_tools(client))
            args = build_arguments(tool, scraper_input)
            self.log.info("calling %s with %s", tool.name, args)
            result = await client.call_tool(tool.name, args)
        if result.is_error:
            texts = " ".join(str(getattr(b, "text", "")) for b in result.content)
            raise RuntimeError(f"{tool.name} failed: {texts[:300]} (arguments: {args})")

        jobs = [p for raw in extract_items(_payload(result)) if (p := to_job_post(raw))]
        if hours := scraper_input.hours_old:
            cutoff = date.today() - timedelta(days=math.ceil(hours / 24))
            jobs = [j for j in jobs if not j.date_posted or j.date_posted >= cutoff]
        return jobs[
            scraper_input.offset : scraper_input.offset + scraper_input.results_wanted
        ]
