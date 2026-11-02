# Job search

A local web app for finding jobs from one page. Enter a keyword, choose the boards, and the results come back as cards you can open for the full posting.

It covers LinkedIn, Indeed, Glassdoor, ZipRecruiter, Bayt, Naukri, BDJobs, and HelloWork. Upwork is included only after you sign in with your own account.

## Run

Python 3.10 or newer.

```bash
pip install -r frontend/requirements.txt
python frontend/server.py
```

Then open http://127.0.0.1:8555

If you already have a virtualenv:

```bash
.venv/Scripts/python.exe frontend/server.py
```

Host and port can be changed when you start it:

```bash
JOBSPY_HOST=127.0.0.1 JOBSPY_PORT=8555 python frontend/server.py
```

The server stays on your machine. Do not expose it on a public network without adding authentication.

## Search

The form has one keyword. There is no location field and no relevance control. Each board's listings are shown as that board returned them.

- Turn boards on or off. Regional boards are marked M-East, IN, BD, and FR.
- Set how many results to ask for, how recent the posts should be, and the job type.
- **Remote** limits the board search to remote roles.
- **Upwork**, next to Remote, adds Upwork to the same search.
- Advanced options cover the Indeed and Glassdoor country, an optional proxy, and full descriptions.

Click **Search Jobs**. Cards show the title, company, pay, job type, remote flag, age, and a link. Open a card for the rest of the record. Descriptions are rendered as normal text, with headings, lists, and bold, instead of raw markup.

## Upwork

Upwork uses your account. The app does not ask for your password.

1. Click **Connect Upwork**. The browser opens Upwork's own login.
2. After you approve access, the page shows **Connected**, your account name, and your role. The connect button is replaced by **Disconnect**.
3. Tick **Upwork** and search. The keyword is applied first. Matching Upwork jobs are listed with the other boards. If nothing matches that keyword, the latest jobs for the account are shown instead, with a note that they were not filtered.

Sign-in tokens stay in local files that are not part of the repository:

- `frontend/.upwork_client.json`
- `frontend/.upwork_token.json`

## API

The page is a small JSON API in `frontend/server.py`.

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/` | The search page |
| GET | `/api/health` | Liveness check |
| GET | `/api/info` | Boards, job types, and defaults |
| POST | `/api/search` | Search the selected boards |
| GET | `/api/searches/{id}/jobs/{job_id}` | Full record for one result |
| GET | `/api/upwork/login` | Send the browser to Upwork to sign in |
| GET | `/api/upwork/callback` | Upwork returns here after sign-in |
| GET | `/api/upwork/status` | Whether an account is connected |
| GET | `/api/upwork/jobs` | Upwork jobs for the connected account |
| POST | `/api/upwork/logout` | Disconnect Upwork |

`GET /api/upwork/jobs` accepts `q` for the keyword, `limit` for how many to return, and `fallback=1`. With fallback, an empty keyword match returns the latest jobs instead.

More detail on the board-search body is in [frontend/README.md](frontend/README.md).
