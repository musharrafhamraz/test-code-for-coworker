"""OAuth 2.1 login + token storage for the Upwork MCP server.

The login is interactive and happens once (``python -m jobspy.upwork login``).
Afterwards scrapes run headless from the stored tokens, which the MCP SDK
refreshes automatically. Tokens live in a 0600 JSON file; treat it like a
password: anyone holding it can act as your Upwork account.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import (
        AuthorizationCodeResult,
        OAuthClientInformationFull,
        OAuthToken,
    )

MCP_URL = os.environ.get("UPWORK_MCP_URL", "https://mcp.upwork.com/mcp")
TOKEN_PATH = Path(
    os.environ.get("UPWORK_MCP_TOKEN_FILE", Path.home() / ".jobspy" / "upwork_mcp.json")
)
REDIRECT_PORT = int(os.environ.get("UPWORK_MCP_REDIRECT_PORT", "8765"))
REDIRECT_URI = f"http://127.0.0.1:{REDIRECT_PORT}/callback"
LOGIN_TIMEOUT = 300  # seconds to finish the browser login

NOT_LOGGED_IN = (
    "Not logged in to Upwork. Run once: python -m jobspy.upwork login "
    "(opens a browser to authorize your Upwork account)"
)


class NotLoggedIn(RuntimeError):
    pass


class FileTokenStorage:
    """TokenStorage implementation backed by a single 0600 JSON file."""

    def __init__(self, path: Path = TOKEN_PATH) -> None:
        self.path = path

    def _read(self) -> dict[str, object]:
        try:
            data: dict[str, object] = json.loads(self.path.read_text(encoding="utf-8"))
            return data
        except (OSError, ValueError):
            return {}

    def _write(self, data: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self.path)

    async def get_tokens(self) -> OAuthToken | None:
        from mcp.shared.auth import OAuthToken

        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json", exclude_none=True)
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        from mcp.shared.auth import OAuthClientInformationFull

        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json", exclude_none=True)
        self._write(data)

    def logged_in(self) -> bool:
        return bool(self._read().get("tokens"))


def _wait_for_callback(timeout: float) -> dict[str, str]:
    """Run a one-shot localhost server until the browser hits /callback."""
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            if url.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            query = {k: v[0] for k, v in parse_qs(url.query).items()}
            result.update(query)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            ok = "code" in query
            self.wfile.write(
                (
                    "<h3>Upwork connected. You can close this tab.</h3>"
                    if ok
                    else "<h3>Authorization failed. Return to the terminal.</h3>"
                ).encode()
            )
            done.set()

        def log_message(self, *args: object) -> None:  # silence request logging
            pass

    server = HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    server.timeout = 1
    deadline = time.monotonic() + timeout
    try:
        while not done.is_set() and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not done.is_set():
        raise TimeoutError(f"no browser login within {int(timeout)}s")
    return result


def build_provider(interactive: bool = False) -> OAuthClientProvider:
    """OAuth provider. Headless (default) refuses to open a browser."""
    from mcp.client.auth import AuthorizationCodeResult, OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    metadata = OAuthClientMetadata(
        client_name="JobSpy",
        redirect_uris=[REDIRECT_URI],  # type: ignore[list-item]
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        token_endpoint_auth_method="none",
    )

    async def redirect_handler(url: str) -> None:
        if not interactive:
            raise NotLoggedIn(NOT_LOGGED_IN)
        print(
            f"Opening your browser to authorize Upwork.\nIf nothing opens, visit:\n{url}\n"
        )
        webbrowser.open(url)

    async def callback_handler() -> AuthorizationCodeResult:
        if not interactive:
            raise NotLoggedIn(NOT_LOGGED_IN)
        params = await asyncio.to_thread(_wait_for_callback, LOGIN_TIMEOUT)
        if "error" in params:
            raise RuntimeError(f"Upwork authorization failed: {params['error']}")
        return AuthorizationCodeResult(
            code=params["code"], state=params.get("state"), iss=params.get("iss")
        )

    return OAuthClientProvider(
        server_url=MCP_URL,
        client_metadata=metadata,
        storage=FileTokenStorage(),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
