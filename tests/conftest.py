import pytest


@pytest.fixture(autouse=True)
def no_live_http_refresh(monkeypatch):
    """Tests never contact the IdP: the browserless refresh reports 'needs login' unless a test overrides it."""
    from core import http_refresh, session
    monkeypatch.setattr(session, "refresh_session_http", lambda *a, **k: http_refresh.NEEDS_LOGIN)
