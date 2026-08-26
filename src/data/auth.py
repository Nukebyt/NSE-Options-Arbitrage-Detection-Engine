"""Auth for Upstox's public+read-only market-data endpoints.

Uses the Analytics access token (static, 365-day validity) rather than the
full OAuth2 authorization-code flow -- see FOUNDATIONS.md S39 and BUGS.md
DEC-2 for why. No redirect flow, no refresh logic: just attach the token.
"""
from __future__ import annotations

import os

from dotenv import load_dotenv


def get_auth_headers() -> dict:
    load_dotenv()
    token = os.environ.get("UPSTOX_ACCESS_TOKEN")
    if not token:
        raise RuntimeError(
            "UPSTOX_ACCESS_TOKEN not set in .env -- generate an Analytics access token at "
            "account.upstox.com/developer/apps (Analytics tab) and drop it in .env. "
            "See ROADMAP.md Phase 0."
        )
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_oauth2_authorization_url() -> str:
    """Step 1 of the full OAuth2 flow (FOUNDATIONS.md S39 Path A), being
    tested for DEC-7 to see if it unlocks WS depth streaming the Analytics
    token couldn't reach. Verified against docs.upstox.com during Phase 0
    research (unlike several other endpoints touched this session, this one
    hasn't yet been independently re-verified against a real successful
    login -- treat as best-effort until tested).
    """
    import urllib.parse

    load_dotenv()
    client_id = os.environ.get("UPSTOX_CLIENT_ID")
    redirect_uri = os.environ.get("UPSTOX_REDIRECT_URI")
    if not client_id or not redirect_uri:
        raise RuntimeError("UPSTOX_CLIENT_ID / UPSTOX_REDIRECT_URI not set in .env")

    params = urllib.parse.urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": redirect_uri})
    return f"https://api.upstox.com/v2/login/authorization/dialog?{params}"


def exchange_oauth2_code(code: str) -> str:
    """Step 3: exchange the single-use authorization code (from the
    redirect after login) for an access token. The code is consumed the
    moment this call runs, successfully or not -- can't be retried with the
    same code."""
    import requests

    load_dotenv()
    resp = requests.post(
        "https://api.upstox.com/v2/login/authorization/token",
        data={
            "code": code,
            "client_id": os.environ.get("UPSTOX_CLIENT_ID"),
            "client_secret": os.environ.get("UPSTOX_CLIENT_SECRET"),
            "redirect_uri": os.environ.get("UPSTOX_REDIRECT_URI"),
            "grant_type": "authorization_code",
        },
        headers={"Accept": "application/json"},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_oauth_auth_headers() -> dict:
    """Separate from get_auth_headers() (the Analytics token) on purpose --
    keeps the working Analytics-token path completely undisturbed while
    this OAuth2 path is still being tested (DEC-7)."""
    load_dotenv()
    token = os.environ.get("UPSTOX_OAUTH_ACCESS_TOKEN")
    if not token:
        raise RuntimeError("UPSTOX_OAUTH_ACCESS_TOKEN not set in .env -- run oauth2_login.py first.")
    return {"Authorization": f"Bearer {token}", "Accept": "application/json"}


def get_ws_authorized_url() -> str:
    """Upstox's WS auth is a two-step authorize-then-connect pattern
    (FOUNDATIONS.md S40): a REST call with the Bearer token returns a
    short-lived, pre-authorized wss:// URL with a single-use `code` embedded
    in its query string. Connect directly to that URL -- no separate auth
    headers needed on the WS handshake itself, the code in the URL IS the auth.

    Two things the docs' prose got wrong, caught by testing live rather than
    trusting the page (2026-08-25): the endpoint is under /v3/, not /v2/
    (the /v2/ path returns 410 Gone -- retired, not just undocumented), and
    the response field is `authorizedRedirectUri` (camelCase), not the
    `authorized_redirect_uri` (snake_case) the docs page showed.
    """
    import requests

    resp = requests.get(
        "https://api.upstox.com/v3/feed/market-data-feed/authorize",
        headers=get_auth_headers(),
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["data"]["authorizedRedirectUri"]
