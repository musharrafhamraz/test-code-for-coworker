# Upwork source (via Upwork's official MCP server)

Adds `upwork` as a board next to LinkedIn/Indeed/etc. It talks to
`https://mcp.upwork.com/mcp` as **your own Upwork account** (OAuth 2.1), so it
returns what your freelancer account can see. Use it for personal monitoring;
do not republish the results on a public site (check Upwork's terms first).

## Setup

```bash
pip install -e ".[upwork]"            # adds the MCP client (mcp>=2.3)
python -m jobspy.upwork login         # opens a browser, authorize once
python -m jobspy.upwork tools         # shows the server's tools + schemas
python -m jobspy.upwork search "python scraping" -n 10
```

Tokens are stored in `~/.jobspy/upwork_mcp.json` (mode 0600; treat it like a
password). Override with `UPWORK_MCP_TOKEN_FILE`.

## Use it from code / the web UI

```python
from jobspy import scrape_jobs

scrape_jobs(site_name=["upwork", "indeed"], search_term="python", results_wanted=20)
```

The web UI shows an **Upwork** pill. The server refuses Upwork searches unless
it is bound to localhost (`JOBSPY_HOST=127.0.0.1`), because the results come
from your personal login. Upwork is never in the default board list.

## Early notifications

```bash
export TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...   # and/or UPWORK_WEBHOOK_URL (Discord/Slack)
python -m jobspy.upwork watch "python scraping" "web scraper" --interval 600
```

New jobs (not yet in `~/.jobspy/upwork_seen.db`) trigger an alert that links
straight to the job on Upwork. The first pass only records existing jobs
(`--notify-existing` to alert on them too). Minimum interval is 60 s; there is
no push/webhook from Upwork, so this polls and is bounded by their rate limits.

## If the search tool isn't recognised

Upwork does not publish tool names or arguments on its public page, so the tool
is discovered from `tools/list` and its arguments built from its JSON schema.
If discovery or mapping is off, run `python -m jobspy.upwork tools`, then set:

- `UPWORK_MCP_SEARCH_TOOL` - exact tool name
- `UPWORK_MCP_SEARCH_ARGS` - JSON merged into the call, e.g. `{"category":"Web Development"}`

Field mapping lives in `to_job_post()` in `__init__.py` (aliases per field).

## Tests

`pytest tests/test_upwork.py` runs offline against a fake in-process MCP server.
