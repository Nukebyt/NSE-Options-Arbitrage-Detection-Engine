"""One-shot interactive OAuth2 login for testing DEC-7 (does a full-App
token unlock WS depth streaming the Analytics token couldn't reach?).

Starts a tiny local HTTP server on the exact host:port from
UPSTOX_REDIRECT_URI, prints the authorization URL for you to open in a
browser, waits for Upstox's redirect to land on that local server (which
captures the `code` query param automatically -- no manual copy-paste out
of a broken browser tab), exchanges it for an access token, and writes
UPSTOX_OAUTH_ACCESS_TOKEN into .env directly.

Run from an interactive terminal (needs your browser), not from a
non-interactive script.
"""
from __future__ import annotations

import os
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from dotenv import load_dotenv

from auth import exchange_oauth2_code, get_oauth2_authorization_url

_captured_code: str | None = None
_server_error: str | None = None


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        global _captured_code, _server_error
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if "code" in params:
            _captured_code = params["code"][0]
            body = b"<html><body>Login captured, you can close this tab.</body></html>"
        else:
            _server_error = params.get("error_description", params.get("error", ["unknown error"]))[0]
            body = f"<html><body>No code received: {_server_error}</body></html>".encode()

        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass  # silence the default per-request stderr logging


def _write_token_to_env(token: str) -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    lines = env_path.read_text().splitlines()
    found = False
    for i, line in enumerate(lines):
        if line.startswith("UPSTOX_OAUTH_ACCESS_TOKEN="):
            lines[i] = f"UPSTOX_OAUTH_ACCESS_TOKEN={token}"
            found = True
            break
    if not found:
        lines.append(f"UPSTOX_OAUTH_ACCESS_TOKEN={token}")
    env_path.write_text("\n".join(lines) + "\n")


def main() -> None:
    load_dotenv()
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI")
    if not redirect_uri:
        print("UPSTOX_REDIRECT_URI not set in .env -- fill in CLIENT_ID/CLIENT_SECRET/REDIRECT_URI first.")
        sys.exit(1)

    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 80

    server = HTTPServer((host, port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()

    auth_url = get_oauth2_authorization_url()
    print(f"\n1. Open this URL in your browser and log in:\n\n   {auth_url}\n")
    print(f"2. Waiting for the redirect back to {redirect_uri} ...")

    thread.join(timeout=180)
    server.server_close()

    if _captured_code is None:
        print(f"\nNo code received within the wait window. Error: {_server_error}")
        sys.exit(1)

    print("\nCode captured, exchanging for an access token...")
    token = exchange_oauth2_code(_captured_code)
    _write_token_to_env(token)
    print("Done. UPSTOX_OAUTH_ACCESS_TOKEN written to .env (not printed here).")


if __name__ == "__main__":
    main()
