"""Poll Upwork for new jobs and alert you the moment one appears.

Seen job ids are kept in SQLite so restarts don't re-alert. The first pass
only records what already exists (no alert flood); pass ``notify_existing=True``
to alert on those too. Each alert links straight to the job on Upwork.

Channels (configure any combination via environment variables):
  TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID   Telegram message
  UPWORK_WEBHOOK_URL                      JSON POST ({"content","text"}: works
                                          with Discord and Slack incoming webhooks)
With none set, alerts print to the console.
"""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import requests

from jobspy import scrape_jobs

DB_PATH = Path(
    os.environ.get("UPWORK_WATCH_DB", Path.home() / ".jobspy" / "upwork_seen.db")
)
MIN_INTERVAL = 60  # seconds; polling faster only burns rate limit


def format_alert(job: dict[str, Any]) -> str:
    pay = ""
    if job.get("min_amount") or job.get("max_amount"):
        low, high = job.get("min_amount"), job.get("max_amount")
        unit = "/hr" if job.get("interval") == "hourly" else " fixed"
        rng = f"{low:g}" if low == high else f"{low or '?'}-{high or '?'}"
        pay = f"\n{job.get('currency') or ''} {rng}{unit}".replace("  ", " ")
    return f"New Upwork job: {job['title']}{pay}\n{job['job_url']}"


def send_telegram(text: str) -> bool:
    token, chat = (
        os.environ.get("TELEGRAM_BOT_TOKEN"),
        os.environ.get("TELEGRAM_CHAT_ID"),
    )
    if not (token and chat):
        return False
    resp = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "disable_web_page_preview": False},
        timeout=15,
    )
    resp.raise_for_status()
    return True


def send_webhook(text: str) -> bool:
    url = os.environ.get("UPWORK_WEBHOOK_URL")
    if not url:
        return False
    requests.post(
        url, json={"content": text, "text": text}, timeout=15
    ).raise_for_status()
    return True


def notify(text: str) -> None:
    sent = False
    for channel in (send_telegram, send_webhook):
        try:
            sent = channel(text) or sent
        except requests.RequestException as err:
            print(f"[notify] {channel.__name__} failed: {err}")
    if not sent:
        print(text, "\n")


def _open_db(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.execute(
        "CREATE TABLE IF NOT EXISTS seen (key TEXT PRIMARY KEY, title TEXT, url TEXT,"
        " first_seen TEXT DEFAULT CURRENT_TIMESTAMP)"
    )
    return db


def check_once(
    terms: Iterable[str],
    db: sqlite3.Connection,
    limit: int = 20,
    notify_existing: bool = False,
    send: Callable[[str], None] = notify,
) -> int:
    """One polling pass. Returns the number of new jobs alerted."""
    first_run = db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0
    alerted = 0
    for term in terms:
        jobs = scrape_jobs(
            site_name=["upwork"], search_term=term, results_wanted=limit, verbose=0
        )
        if err := jobs.attrs.get("errors", {}).get("upwork"):
            print(f"[watch] upwork error for {term!r}: {err}")
            continue
        for raw in jobs.to_dict("records"):
            row = {str(k): v for k, v in raw.items()}
            key = row.get("id") or row["job_url"]
            if db.execute("SELECT 1 FROM seen WHERE key=?", (key,)).fetchone():
                continue
            db.execute(
                "INSERT INTO seen(key, title, url) VALUES (?,?,?)",
                (key, row["title"], row["job_url"]),
            )
            if first_run and not notify_existing:
                continue
            send(format_alert(row))
            alerted += 1
        db.commit()
    return alerted


def watch(
    terms: list[str],
    interval: int = 600,
    limit: int = 20,
    notify_existing: bool = False,
    db_path: Path = DB_PATH,
) -> None:
    interval = max(interval, MIN_INTERVAL)
    db = _open_db(db_path)
    print(f"watching Upwork for {terms} every {interval}s (Ctrl+C to stop)")
    while True:
        try:
            n = check_once(terms, db, limit, notify_existing)
            print(f"[watch] {time.strftime('%H:%M:%S')} new jobs: {n}")
        except Exception as err:  # noqa: BLE001 - keep the watcher alive
            print(f"[watch] pass failed: {err}")
        notify_existing = False
        time.sleep(interval)
