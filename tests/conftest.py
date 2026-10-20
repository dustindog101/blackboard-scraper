import pytest


@pytest.fixture(autouse=True)
def no_live_http_refresh(monkeypatch, tmp_path):
    """Keep tests away from protected application storage, Messages and the IdP."""
    from core import http_refresh, session, session_state
    for module in (http_refresh, session, session_state):
        monkeypatch.setattr(module, "SESSION_DIR", tmp_path / "session")
    monkeypatch.setattr(session, "check_sms_access", lambda: (False, "offline fixture: Messages disabled"))
    monkeypatch.setattr(session, "refresh_session_http", lambda *a, **k: http_refresh.NEEDS_LOGIN)
