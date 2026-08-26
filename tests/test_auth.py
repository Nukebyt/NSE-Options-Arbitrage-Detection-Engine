import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "data"))

import auth


def test_raises_clearly_when_token_missing(monkeypatch):
    monkeypatch.delenv("UPSTOX_ACCESS_TOKEN", raising=False)
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)  # don't let a real .env override the test
    try:
        auth.get_auth_headers()
        assert False, "expected RuntimeError"
    except RuntimeError as e:
        assert "UPSTOX_ACCESS_TOKEN" in str(e)


def test_returns_bearer_header_shape(monkeypatch):
    monkeypatch.setenv("UPSTOX_ACCESS_TOKEN", "fake-token-for-testing")
    monkeypatch.setattr(auth, "load_dotenv", lambda: None)
    headers = auth.get_auth_headers()
    assert headers["Authorization"] == "Bearer fake-token-for-testing"
    assert headers["Accept"] == "application/json"
