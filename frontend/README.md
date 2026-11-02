# JobSpy Web UI

A small frontend + JSON API for JobSpy. Enter a keyword, pick boards, hit
**Search Jobs**, get the listings.

## Run

From the repo root (activate `.venv` first if you prefer, or use the full path):

```bash
.venv/Scripts/python.exe frontend/server.py
```

Then open **http://127.0.0.1:8555**

Custom host/port via environment (`frontend/server.py` reads them at startup):

```bash
JOBSPY_HOST=0.0.0.0 JOBSPY_PORT=8080 .venv/Scripts/python.exe frontend/server.py
```

## Using it inside another codebase

`frontend/server.py` is a plain ASGI module: import it and serve `app` with any
server — no side effects at import time.

```python
from frontend.server import app  # Starlette app

uvicorn.run(app, host="127.0.0.1", port=8555)  # or gunicorn -k uvicorn.workers.UvicornWorker frontend.server:app
```

The routes are also reusable piecemeal — `api_search`, `api_job`, `api_info`,
`api_health` — or skip them and call the functions underneath directly
(`title_relevance(search_term, title)` if you still want the optional title
scorer, and `scrape_jobs` through a worker thread as in `api_search`). Minimal
third-party requirements live in `frontend/requirements.txt`; the UI itself
has none.

## Frontend features

- Keyword search across any of the 8 boards (toggle pills; regional boards
  are labeled M-East / IN / BD / FR). The form has no location field.
- Results per board, posting age, and job type
- **Remote** and **Upwork** checkboxes. Upwork is included in **Search Jobs**
  only when it is ticked, and only after **Connect Upwork** (browser sign-in
  on upwork.com, then Upwork's hosted MCP server). The keyword is sent first.
  If it matches nothing, the latest jobs for that account are shown as
  returned, with a note. The card shows **Connected**, the account name, and
  the role; **Disconnect** clears the local session.
- Advanced: Indeed/Glassdoor country, proxy (rotated per board), full job
  descriptions. Descriptions in the details panel are rendered as formatted
  text, not raw markdown.
- Per-board result counts, elapsed time, and blocked-board errors shown as banners
- Colored cards per board with salary (formatted hourly/…/yearly), job type,
  remote and age badges. The page does not filter titles for relevance; it
  shows what each board returned.
- Click a job (or Enter on \*it\*) to open a right-side details panel that
  lists **every field** the board returned: full description, **About the
  company**, skills, experience, level, job function, listing type, salary
  source, openings, work-from-home type, emails, company rating and reviews,
  employees, revenue, addresses, posting ID, website links, the direct-
  application link, company logo, plus the **Open posting** button. Esc or ✕
  closes; on narrow screens it slides in as a drawer.
- Descriptions show in the panel when the search fetched them; otherwise the
  panel notes to re-run with "Include job descriptions" (Indeed always has
  them without the option).

UI files: `frontend/index.html` (no build step, no dependencies).

## API

All JSON. The server binds `127.0.0.1` by default — keep it local unless you
add auth.

- `GET /` — the UI page
- `GET /api/health` — `{"ok": true}`
- `GET /api/info` — boards, job types, defaults, limits (the UI auto-loads this)
- `POST /api/search` — body:

```json
{
  "search_term": "software engineer",
  "site_name": ["indeed", "zip_recruiter"],
  "results_wanted": 10,
  "hours_old": 168,
  "is_remote": false,
  "job_type": null,
  "country_indeed": "usa",
  "description": false,
  "proxy": "user:pass@host:port",
  "title_match": "none"
}
```

`location` is still accepted by `POST /api/search` for callers that want it.
The page does not send it. Every field except the body is optional; give at
least a `search_term` (or a `location`, or `is_remote`). The page sends
`title_match: "none"`. Response:

```json
{
  "search_id": "k9Lwm0nPW78",
  "count": 6,
  "counts": {"indeed": 3, "zip_recruiter": 3},
  "filtered": {"mode": "loose", "hidden": 1, "by_site": {"zip_recruiter": 1},
               "examples": ["Summer 2027 Internship - RA/QA - Quality Assurance"]},
  "errors": {"linkedin": "response status code 429 (blocked: ...)"},
  "warnings": {"bdjobs": ["job_type internship isn't supported, ignoring it"]},
  "elapsed_s": 2.3,
  "jobs": [{"site": "indeed", "title": "Java Developer", ...}]
}
```

`title_match` — `loose` (title should cover a good share of the keywords,
with synonyms like engineer↔developer↔programmer), `strict` (the title must
cover every keyword), or `none` (keep everything as the boards returned it).
The page always sends `none`. Other values still drop rows and report them
under `filtered`.

Upwork is separate from that body:

- `GET /api/upwork/login` — redirect the browser to Upwork's OAuth page
- `GET /api/upwork/callback` — Upwork returns here; then the browser goes back to `/`
- `GET /api/upwork/status` — `{connected, account_name, role_label}`
- `GET /api/upwork/jobs?q=&limit=&fallback=1` — keyword search for the connected account. `fallback=1` with an empty match returns the latest jobs instead.
- `POST /api/upwork/logout` — revoke and delete the local token

`jobs` rows carry `id, site, title, company, location, date_posted, is_remote,
job_url` plus `job_type` and `min/max_amount, currency, interval` when the
board gives them; rows a board left empty drop those keys. Clicking a job in
the UI fetches its full record:

- `GET /api/searches/{search_id}/jobs/{id}` — the complete JobPost record
  (description when fetched, skills, experience, company info and rating, direct
  apply URL, logo, and more) kept in server memory for the last 20 searches.

Notes:

- `description: true` sends one extra request per job for boards other than
  Indeed — slower and more likely to trigger blocks.
- A 504 means the scrape didn't finish in 180s (the scrape keeps running in
  the background).
- Boards that fail come back in `errors`/`warnings` while other boards' results
  still return — a popular search without a proxy will typically 429 LinkedIn.
