"""Shared fakes for mocked login tests: SSO step, Duo method list, HTTP verification."""

import contextlib
from unittest.mock import patch

from core import session


@contextlib.contextmanager
def sso_and_verification(page, verified=True, fake_flow=True):
    """HTTP verification returns `verified` (bool or callable).

    With fake_flow the pre-Duo step follows page.url and Duo offers SMS; without it
    the real wait_for_sso / duo_methods run against a real browser page.
    """
    check = verified if callable(verified) else (lambda: verified)
    with contextlib.ExitStack() as stack:
        patches = [
            ("is_verified", {"side_effect": lambda *a, **k: check()}),
            ("record_user_agent", {}),
        ]
        if fake_flow:
            patches += [
                ("wait_for_sso", {"side_effect": lambda *a, **k: "duo" if "duosecurity" in page.url else "authenticated"}),
                ("duo_methods", {"return_value": ["sms"]}),
                ("wait_for_blackboard", {"return_value": None}),
            ]
        for name, kwargs in patches:
            stack.enter_context(patch.object(session, name, **kwargs))
        yield
