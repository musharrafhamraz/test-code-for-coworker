"""Upwork sign-in and job search through the official MCP server.

The browser is sent to Upwork's own login page. This module never asks for
a password. After Upwork redirects back, the latest marketplace jobs are
read with the authenticated MCP session.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

log = logging.getLogger("jobspy-web.upwork")

MCP_URL = "https://mcp.upwork.com/mcp"
RESOURCE = "https://mcp.upwork.com/mcp"
REGISTER_URL = "https://www.upwork.com/register"
TOKEN_URL = "https://www.upwork.com/api/v3/oauth2/token"
REVOKE_URL = "https://www.upwork.com/api/v3/oauth2/token/revoke"
AUTHORIZE_URL = "https://www.upwork.com/ab/account-security/oauth2/authorize"
PROTOCOL = "2025-06-18"

HERE = Path(__file__).resolve().parent
CLIENT_PATH = HERE / ".upwork_client.json"
TOKEN_PATH = HERE / ".upwork_token.json"

# state -> {verifier, redirect_uri, created}
PENDING: dict[str, dict[str, Any]] = {}
# one MCP session for this process
MCP_SESSION: dict[str, str | None] = {"id": None}


class UpworkError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _http(
    url: str,
    *,
    method: str = "GET",
    data: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 45,
) -> tuple[int, dict[str, str], bytes]:
    sent = {"User-Agent": "JobSpy", "Accept": "application/json"}
    sent.update(headers or {})
    req = urllib.request.Request(url, data=data, method=method, headers=sent)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            return res.status, {k.lower(): v for k, v in res.headers.items()}, res.read()
    except urllib.error.HTTPError as err:
        return err.code, {k.lower(): v for k, v in err.headers.items()}, err.read()


def redirect_uris(port: int) -> list[str]:
    return [
        f"http://127.0.0.1:{port}/api/upwork/callback",
        f"http://localhost:{port}/api/upwork/callback",
    ]


def callback_uri(netloc: str, port: int) -> str:
    host = netloc.split(":")[0].lower()
    if host == "localhost":
        return f"http://localhost:{port}/api/upwork/callback"
    return f"http://127.0.0.1:{port}/api/upwork/callback"


def ensure_client(port: int) -> dict[str, Any]:
    wanted = redirect_uris(port)
    saved = _read_json(CLIENT_PATH)
    if saved and saved.get("client_id") and saved.get("redirect_uris") == wanted:
        return saved
    body = json.dumps(
        {
            "client_name": "JobSpy",
            "redirect_uris": wanted,
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
    ).encode()
    status, _, raw = _http(
        REGISTER_URL,
        method="POST",
        data=body,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    if status not in (200, 201):
        raise UpworkError(
            f"Upwork rejected the app registration ({status}).", status=502
        )
    client = json.loads(raw.decode())
    if not client.get("client_id"):
        raise UpworkError("Upwork did not return a client id.", status=502)
    kept = {
        "client_id": client["client_id"],
        "redirect_uris": wanted,
        "client_secret": client.get("client_secret") or "",
    }
    _write_json(CLIENT_PATH, kept)
    return kept


def begin_login(netloc: str, port: int) -> str:
    client = ensure_client(port)
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)
    redirect = callback_uri(netloc, port)
    now = time.time()
    for key, row in list(PENDING.items()):
        if now - float(row["created"]) > 600:
            PENDING.pop(key, None)
    PENDING[state] = {"verifier": verifier, "redirect_uri": redirect, "created": now}
    query = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(query)


def _token_request(form: dict[str, str]) -> dict[str, Any]:
    client = _read_json(CLIENT_PATH) or {}
    if client.get("client_secret"):
        form["client_secret"] = client["client_secret"]
    body = urllib.parse.urlencode(form).encode()
    status, _, raw = _http(
        TOKEN_URL,
        method="POST",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
    )
    try:
        payload = json.loads(raw.decode() or "{}")
    except ValueError:
        payload = {}
    if status != 200 or "access_token" not in payload:
        detail = payload.get("error_description") or payload.get("error") or raw[:180].decode("utf-8", "replace")
        raise UpworkError(f"Upwork did not issue a token: {detail}", status=401)
    return payload


def _store_token(payload: dict[str, Any], previous: dict[str, Any] | None = None) -> dict[str, Any]:
    expires_in = int(payload.get("expires_in") or 3600)
    saved = {
        "access_token": payload["access_token"],
        "refresh_token": payload.get("refresh_token") or (previous or {}).get("refresh_token"),
        "expires_at": time.time() + expires_in - 60,
        "account_name": (previous or {}).get("account_name") or "",
        "role_label": (previous or {}).get("role_label") or "",
    }
    _write_json(TOKEN_PATH, saved)
    MCP_SESSION["id"] = None
    return saved


def finish_login(code: str, state: str) -> None:
    row = PENDING.pop(state, None)
    if row is None or time.time() - float(row["created"]) > 600:
        raise UpworkError("That sign-in expired. Connect Upwork again.", status=400)
    client = _read_json(CLIENT_PATH) or {}
    payload = _token_request(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": row["redirect_uri"],
            "client_id": client.get("client_id", ""),
            "code_verifier": row["verifier"],
            "resource": RESOURCE,
        }
    )
    saved = _store_token(payload)
    try:
        account = _pick_account(saved["access_token"])
        saved["account_name"] = str(account.get("name") or "")
        saved["role_label"] = str(account.get("role_label") or account.get("role") or "")
        _write_json(TOKEN_PATH, saved)
    except UpworkError as err:
        log.warning("signed in, account lookup failed: %s", err.message)


def connected() -> dict[str, Any]:
    saved = _read_json(TOKEN_PATH)
    if not saved or not saved.get("access_token"):
        return {"connected": False}
    return {
        "connected": True,
        "account_name": saved.get("account_name") or "",
        "role_label": saved.get("role_label") or "",
    }


def disconnect() -> None:
    saved = _read_json(TOKEN_PATH)
    client = _read_json(CLIENT_PATH) or {}
    if saved and saved.get("refresh_token"):
        form = {"token": saved["refresh_token"], "client_id": client.get("client_id", "")}
        body = urllib.parse.urlencode(form).encode()
        _http(
            REVOKE_URL,
            method="POST",
            data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    TOKEN_PATH.unlink(missing_ok=True)
    MCP_SESSION["id"] = None


def _access_token() -> str:
    saved = _read_json(TOKEN_PATH)
    if not saved or not saved.get("access_token"):
        raise UpworkError("Connect Upwork before loading jobs.", status=401)
    if float(saved.get("expires_at") or 0) > time.time():
        return str(saved["access_token"])
    if not saved.get("refresh_token"):
        raise UpworkError("The Upwork session expired. Connect again.", status=401)
    client = _read_json(CLIENT_PATH) or {}
    payload = _token_request(
        {
            "grant_type": "refresh_token",
            "refresh_token": saved["refresh_token"],
            "client_id": client.get("client_id", ""),
            "resource": RESOURCE,
        }
    )
    return str(_store_token(payload, saved)["access_token"])


def _parse_mcp_body(raw: bytes, content_type: str) -> dict[str, Any]:
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return {}
    if "text/event-stream" in content_type:
        messages: list[dict[str, Any]] = []
        for line in text.splitlines():
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                messages.append(json.loads(data))
            except ValueError:
                continue
        for msg in reversed(messages):
            if "id" in msg:
                return msg
        return messages[-1] if messages else {}
    try:
        return json.loads(text)
    except ValueError as err:
        raise UpworkError("Upwork returned a response that was not JSON.", status=502) from err


def _mcp(token: str, message: dict[str, Any], protocol: str = PROTOCOL) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "MCP-Protocol-Version": protocol,
        "User-Agent": "JobSpy",
    }
    if MCP_SESSION["id"]:
        headers["Mcp-Session-Id"] = str(MCP_SESSION["id"])
    status, hdrs, raw = _http(MCP_URL, method="POST", data=json.dumps(message).encode(), headers=headers)
    if session := hdrs.get("mcp-session-id"):
        MCP_SESSION["id"] = session
    if status == 401:
        raise UpworkError("Upwork rejected the session. Connect again.", status=401)
    if status >= 400:
        detail = raw[:240].decode("utf-8", "replace")
        raise UpworkError(f"Upwork MCP returned {status}: {detail}", status=502)
    msg = _parse_mcp_body(raw, hdrs.get("content-type", ""))
    if "error" in msg:
        err = msg["error"]
        text = err.get("message") if isinstance(err, dict) else str(err)
        raise UpworkError(text or "Upwork MCP call failed.", status=502)
    return msg.get("result") if isinstance(msg.get("result"), dict) else msg


def _connect(token: str) -> None:
    last_error: UpworkError | None = None
    for protocol in (PROTOCOL, "2025-03-26"):
        MCP_SESSION["id"] = None
        try:
            _mcp(
                token,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": protocol,
                        "capabilities": {},
                        "clientInfo": {"name": "JobSpy", "version": "1.4.1"},
                    },
                },
                protocol,
            )
            _mcp(token, {"jsonrpc": "2.0", "method": "notifications/initialized"}, protocol)
            return
        except UpworkError as err:
            last_error = err
            if "protocol" not in err.message.lower():
                raise
    if last_error:
        raise last_error


def _tool(token: str, name: str, arguments: dict[str, Any]) -> Any:
    result = _mcp(
        token,
        {
            "jsonrpc": "2.0",
            "id": secrets.randbelow(100000) + 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    if result.get("isError"):
        raise UpworkError(_content_text(result) or f"{name} failed.", status=502)
    return _content_data(result)


def _content_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
    return "\n".join(parts).strip()


def _content_data(result: dict[str, Any]) -> Any:
    text = _content_text(result)
    if not text:
        return result
    try:
        return json.loads(text)
    except ValueError:
        return {"text": text}


def _tool_names(token: str) -> set[str]:
    names: set[str] = set()
    cursor: str | None = None
    for _ in range(6):
        params: dict[str, Any] = {}
        if cursor:
            params["cursor"] = cursor
        listed = _mcp(
            token,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params},
        )
        for tool in listed.get("tools") or []:
            if isinstance(tool, dict) and tool.get("name"):
                names.add(str(tool["name"]))
        cursor = listed.get("nextCursor") or None
        if not cursor:
            return names
    return names


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("accounts", "items", "results", "data"):
            found = _as_list(value.get(key))
            if found:
                return found
    return []


def _pick_account(token: str) -> dict[str, Any]:
    _connect(token)
    payload = _tool(token, "upwork__list_accounts", {})
    accounts = _as_list(payload)
    if not accounts and isinstance(payload, dict):
        accounts = [payload] if payload.get("org_uid") or payload.get("name") else []
    if not accounts:
        raise UpworkError("Upwork did not return an account for this login.", status=502)
    for role in ("TALENT", "FL_AGENCY", "CLIENT"):
        for account in accounts:
            if str(account.get("role") or "").upper() == role and not account.get("suspended"):
                return account
    return accounts[0]


def _call_find_jobs(token: str, account: dict[str, Any], query: str, limit: int) -> Any:
    org = str(account.get("org_uid") or "")
    # Blank search is the freelancer's newest matches. A keyword searches the marketplace.
    if query:
        action, params = "search", {"query": query}
    else:
        action, params = "smart_search", {"mode": "most_recent"}
    return _tool(
        token,
        "upwork__find_jobs",
        {"action": action, "org_uid": org, "params": params},
    )


def _dig(node: Any, *keys: str) -> Any:
    if not isinstance(node, dict):
        return None
    lowered = {str(k).lower(): v for k, v in node.items()}
    for key in keys:
        if key.lower() in lowered and lowered[key.lower()] not in (None, "", [], {}):
            return lowered[key.lower()]
    return None


def _job_lists(node: Any, found: list[list[dict[str, Any]]]) -> None:
    if isinstance(node, list):
        rows = [item for item in node if isinstance(item, dict)]
        if rows and any(_dig(row, "title", "job_title", "opening_title") for row in rows):
            found.append(rows)
        for item in node:
            _job_lists(item, found)
    elif isinstance(node, dict):
        for value in node.values():
            _job_lists(value, found)


def _plain(value: Any) -> str:
    text = value if isinstance(value, str) else ""
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _skills(value: Any) -> list[str]:
    names: list[str] = []
    if isinstance(value, str):
        names.append(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, str):
                names.append(item)
            elif isinstance(item, dict):
                label = _dig(item, "name", "prettyName", "skill", "prefLabel")
                if isinstance(label, str):
                    names.append(label)
    return [name for name in names if name][:12]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = value.replace(",", "").replace("$", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    if isinstance(value, dict):
        return _number(_dig(value, "amount", "value", "min", "minimum", "rawValue"))
    return None


def _normalize(row: dict[str, Any], index: int) -> dict[str, Any]:
    title = _dig(row, "title", "job_title", "opening_title") or "Untitled job"
    ciphertext = str(_dig(row, "ciphertext", "job_ciphertext", "opening_ciphertext") or "")
    url = _dig(row, "url", "job_url", "jobUrl", "link", "canonical_url")
    if not isinstance(url, str) or not url.startswith("http"):
        url = f"https://www.upwork.com/jobs/{ciphertext}" if ciphertext.startswith("~") else ""
    client = _dig(row, "client", "buyer", "company", "companyName")
    company = client if isinstance(client, str) else ""
    if isinstance(client, dict):
        company = str(_dig(client, "name", "companyName", "company_name") or "")
    description = _dig(row, "description", "snippet", "summary", "content")
    if isinstance(description, dict):
        description = _dig(description, "text", "content") or ""
    posted = _dig(row, "publishedOn", "published_on", "date_posted", "createdOn", "createdDateTime")
    if isinstance(posted, (int, float)) and posted > 10_000_000_000:
        posted = time.strftime("%Y-%m-%d", time.gmtime(posted / 1000))
    elif isinstance(posted, (int, float)):
        posted = time.strftime("%Y-%m-%d", time.gmtime(posted))
    elif isinstance(posted, str) and "T" in posted:
        posted = posted.split("T", 1)[0]
    hourly = _dig(row, "hourlyBudget", "hourly_budget", "hourlyRate", "hourly_rate")
    fixed = _dig(row, "amount", "budget", "fixedPrice", "fixed_price")
    pay = hourly if hourly not in (None, "", {}, []) else fixed
    min_amount = _number(_dig(pay, "min", "minimum", "minAmount") if isinstance(pay, dict) else pay)
    max_amount = _number(_dig(pay, "max", "maximum", "maxAmount") if isinstance(pay, dict) else None)
    if min_amount is None:
        min_amount = _number(pay)
    currency = "USD"
    if isinstance(pay, dict) and isinstance(_dig(pay, "currency", "currencyCode"), str):
        currency = str(_dig(pay, "currency", "currencyCode"))
    job_type = "hourly" if hourly not in (None, "", {}, []) else ("fixed" if fixed else None)
    job_id = ciphertext or str(_dig(row, "id", "job_id", "opening_id") or f"upwork-{index}")
    return {
        "id": job_id,
        "site": "upwork",
        "title": str(title),
        "company": company,
        "location": str(_dig(row, "location", "country", "client_location") or ""),
        "job_url": url,
        "description": _plain(description)[:20_000],
        "date_posted": posted if isinstance(posted, str) else None,
        "job_type": job_type,
        "min_amount": min_amount,
        "max_amount": max_amount,
        "currency": currency,
        "interval": "hourly" if job_type == "hourly" else None,
        "skills": _skills(_dig(row, "skills", "skillLabels", "ontologySkills")),
    }


def _jobs_from(payload: Any, limit: int) -> tuple[list[dict[str, Any]], str]:
    lists: list[list[dict[str, Any]]] = []
    _job_lists(payload, lists)
    lists.sort(key=len, reverse=True)
    rows = lists[0][:limit] if lists else []
    note = ""
    if isinstance(payload, dict):
        reason = payload.get("mode_note") or (payload.get("empty_reason") if not rows else "")
        if reason:
            note = str(reason)
        ignored = payload.get("filters_ignored")
        if ignored:
            note = (note + " " if note else "") + f"Some filters were not applied: {ignored}"
    return [_normalize(row, i) for i, row in enumerate(rows)], note.strip()


def latest_jobs(query: str, limit: int, fallback: bool = False) -> dict[str, Any]:
    token = _access_token()
    account = _pick_account(token)
    saved = _read_json(TOKEN_PATH) or {}
    saved["account_name"] = str(account.get("name") or saved.get("account_name") or "")
    saved["role_label"] = str(account.get("role_label") or account.get("role") or "")
    _write_json(TOKEN_PATH, saved)
    if account.get("suspended"):
        raise UpworkError("This Upwork account is restricted, so job search is blocked.", status=403)
    keyword = query.strip()
    payload = _call_find_jobs(token, account, keyword, limit)
    jobs, note = _jobs_from(payload, limit)
    if fallback and keyword and not jobs:
        payload = _call_find_jobs(token, account, "", limit)
        jobs, note = _jobs_from(payload, limit)
        note = "No Upwork jobs matched that keyword, so these are the latest jobs as returned." + (
            f" {note}" if note else ""
        )
    return {
        "connected": True,
        "account_name": saved["account_name"],
        "role_label": saved["role_label"],
        "count": len(jobs),
        "note": note.strip(),
        "jobs": jobs,
    }
