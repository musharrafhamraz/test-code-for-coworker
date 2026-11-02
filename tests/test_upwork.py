"""Offline tests for the Upwork source: a fake in-process MCP server plays Upwork."""

from __future__ import annotations

import asyncio
import os
import tempfile
import threading
import time
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path

_tmp = tempfile.mkdtemp()
os.environ["UPWORK_MCP_TOKEN_FILE"] = str(Path(_tmp) / "tok.json")
os.environ["UPWORK_MCP_REDIRECT_PORT"] = "8799"

import pytest  # noqa: E402
from mcp import Client  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

import jobspy.upwork as up  # noqa: E402
from jobspy import scrape_jobs  # noqa: E402
from jobspy.upwork import auth  # noqa: E402
from jobspy.upwork.watch import _open_db, check_once, format_alert  # noqa: E402

CALLS: list[dict] = []
JOBS = [
    {
        "ciphertext": "~01abc",
        "title": "Python scraper dev",
        "description": "Build it",
        "createdOn": "2026-11-02T08:00:00Z",
        "hourlyBudgetMin": 25,
        "hourlyBudgetMax": 40,
        "skills": [{"name": "Python"}, {"name": "Scrapy"}],
        "client": {"country": "Germany", "companyName": "Acme"},
    },
    {
        "ciphertext": "~01def",
        "title": "Fixed price data job",
        "budget": {"amount": 300},
        "createdOn": "2026-10-01T08:00:00Z",
        "skills": ["SQL"],
    },
]


def make_server() -> MCPServer:
    server = MCPServer("fake-upwork")

    @server.tool()
    def search_jobs(query: str, limit: int = 10, sort: str = "relevance") -> dict:
        """Search Upwork jobs by keyword."""
        CALLS.append({"query": query, "limit": limit, "sort": sort})
        return {"jobs": JOBS}

    @server.tool()
    def search_talent(query: str) -> dict:
        """Search freelancers."""
        return {"talent": []}

    @server.tool()
    def list_contracts() -> dict:
        """List active contracts."""
        return {}

    return server


@pytest.fixture(autouse=True)
def fake_connect(monkeypatch):
    @asynccontextmanager
    async def connect(interactive: bool = False):
        async with Client(make_server()) as client:
            yield client

    monkeypatch.setattr(up, "connect", connect)
    Path(os.environ["UPWORK_MCP_TOKEN_FILE"]).write_text(
        '{"tokens": {"access_token": "x"}}'
    )
    CALLS.clear()


def test_tool_discovery_picks_job_search_not_talent():
    async def run():
        async with up.connect() as client:
            return up.pick_search_tool(await up.list_all_tools(client)).name

    assert asyncio.run(run()) == "search_jobs"


def test_tool_override_and_unknown(monkeypatch):
    async def run(name):
        monkeypatch.setenv("UPWORK_MCP_SEARCH_TOOL", name)
        async with up.connect() as client:
            return up.pick_search_tool(await up.list_all_tools(client)).name

    assert asyncio.run(run("search_talent")) == "search_talent"
    with pytest.raises(
        BaseException
    ) as info:  # task groups wrap it in an ExceptionGroup
        asyncio.run(run("nope"))
    assert "not found" in up._leaf_message(info.value)


def test_scrape_jobs_end_to_end_and_mapping():
    df = scrape_jobs(
        site_name="upwork", search_term="python", results_wanted=5, verbose=0
    )
    assert not df.attrs["errors"], df.attrs["errors"]
    assert CALLS == [{"query": "python", "limit": 5, "sort": "relevance"}]
    assert len(df) == 2
    hourly = df[df.id == "upwork-~01abc"].iloc[0]
    assert hourly.site == "upwork"
    assert hourly.job_url == "https://www.upwork.com/jobs/~01abc"
    assert (hourly.min_amount, hourly.max_amount, hourly.interval) == (25, 40, "hourly")
    assert hourly.skills == "Python, Scrapy" and hourly.company == "Acme"
    assert hourly.location == "Germany" and hourly.is_remote
    fixed = df[df.id == "upwork-~01def"].iloc[0]
    assert (fixed.min_amount, fixed.max_amount) == (300, 300) and __import__(
        "pandas"
    ).isna(fixed.interval)


def test_hours_old_filters_stale_posts(monkeypatch):
    monkeypatch.setattr(
        up,
        "date",
        type(
            "D",
            (),
            {"today": staticmethod(lambda: __import__("datetime").date(2026, 11, 2))},
        ),
    )
    df = scrape_jobs(site_name="upwork", search_term="x", hours_old=48, verbose=0)
    assert list(df.title) == ["Python scraper dev"]


def test_not_logged_in_is_reported_not_raised():
    Path(os.environ["UPWORK_MCP_TOKEN_FILE"]).unlink()
    df = scrape_jobs(site_name="upwork", search_term="python", verbose=0)
    assert df.empty and "login" in df.attrs["errors"]["upwork"]


def test_extract_items_shapes():
    assert up.extract_items({"data": {"edges": [{"node": {"title": "a"}}]}}) == [
        {"title": "a"}
    ]
    assert up.extract_items([{"title": "a"}]) == [{"title": "a"}]
    assert up.extract_items({"foo": "bar"}) == []


def test_watch_alerts_only_new_jobs_and_seeds_first_run():
    db = _open_db(Path(_tmp) / "seen.db")
    sent: list[str] = []
    assert check_once(["python"], db, send=sent.append) == 0  # first pass seeds
    assert sent == []
    JOBS.append(
        {
            "ciphertext": "~01new",
            "title": "Brand new job",
            "createdOn": "2026-11-02T09:00:00Z",
        }
    )
    try:
        assert check_once(["python"], db, send=sent.append) == 1
        assert check_once(["python"], db, send=sent.append) == 0  # no repeat
    finally:
        JOBS.pop()
    assert "Brand new job" in sent[0] and "upwork.com/jobs/~01new" in sent[0]


def test_format_alert_includes_pay_and_link():
    text = format_alert(
        {
            "title": "T",
            "job_url": "https://u/1",
            "min_amount": 25.0,
            "max_amount": 40.0,
            "interval": "hourly",
            "currency": "USD",
        }
    )
    assert "25-40" in text.replace(" ", "") or "25.0" in text or "25" in text
    assert "https://u/1" in text


def test_token_storage_roundtrip_and_permissions():
    from mcp.shared.auth import OAuthToken

    store = auth.FileTokenStorage(Path(_tmp) / "rt.json")
    assert not store.logged_in()
    asyncio.run(
        store.set_tokens(OAuthToken(access_token="a", refresh_token="r", expires_in=60))
    )
    got = asyncio.run(store.get_tokens())
    assert got.access_token == "a" and got.refresh_token == "r" and store.logged_in()
    if os.name == "posix":
        assert (Path(_tmp) / "rt.json").stat().st_mode & 0o777 == 0o600


def test_oauth_callback_server_receives_code():
    out: dict = {}
    t = threading.Thread(target=lambda: out.update(auth._wait_for_callback(10)))
    t.start()
    time.sleep(0.5)
    urllib.request.urlopen(
        f"{auth.REDIRECT_URI}?code=abc&state=xyz&iss=https%3A%2F%2Fi"
    ).read()
    t.join(5)
    assert out == {"code": "abc", "state": "xyz", "iss": "https://i"}


def test_provider_builds_and_headless_refuses_login():
    provider = auth.build_provider(interactive=False)
    assert str(provider.context.client_metadata.redirect_uris[0]) == auth.REDIRECT_URI
    with pytest.raises(auth.NotLoggedIn):
        asyncio.run(provider.context.redirect_handler("https://x"))
