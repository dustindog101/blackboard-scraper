"""Bounded pre-Duo SSO handling. Diagnostics never include page text or URLs."""

import re
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

# wait_for_sso outcomes.
DUO = 'duo'
AUTHENTICATED = 'authenticated'

# A trusted Duo device redirects back without any prompt; give it this long
# before handing over to the passcode workflow when no prompt has rendered.
DUO_GRACE_SECONDS = 8
_DUO_PROMPT_LABELS = ('Other options', 'Text message passcode', 'Enter a passcode', 'Send a passcode', 'Duo Push')


class LoginFlowError(Exception):
    pass


def is_duo(url):
    host = urlsplit(url).hostname or ''
    return host == 'duosecurity.com' or host.endswith('.duosecurity.com')


def cancelled(event):
    return event is not None and event.is_set()


def verify_session(context, base, timeout=2000):
    """Probe current browser cookies without replacing the saved cache."""
    response = context.request.get(
        base + '/learn/api/public/v1/users/me',
        headers={'Accept': 'application/json'}, timeout=timeout, max_redirects=0,
    )
    try:
        if response.status != 200:
            return False
        data = response.json()
        return isinstance(data, dict) and any(data.get(key) for key in ('id', 'studentId', 'userName'))
    finally:
        response.dispose()


def is_verified(context, base, timeout=2000):
    """verify_session that treats transport errors as 'not verified'."""
    try:
        return verify_session(context, base, timeout=timeout)
    except (PlaywrightError, ValueError):
        return False


def _visible(frame, selector):
    elements = frame.locator(selector + ':visible')
    return elements.first if elements.count() else None


def _click(frame, labels, timeout):
    names = re.compile(r'^\s*(?:' + '|'.join(re.escape(label) for label in labels) + r')[\s.,:;!?]*$', re.I)
    for locator in (frame.get_by_role('button', name=names), frame.get_by_role('link', name=names)):
        for index in range(locator.count()):
            item = locator.nth(index)
            if item.is_visible():
                item.click(timeout=timeout, no_wait_after=True)
                return True
    return False


def _duo_needs_user(page):
    """True once Duo renders a control the passcode workflow can act on."""
    names = re.compile(r'^\s*(?:' + '|'.join(re.escape(label) for label in _DUO_PROMPT_LABELS) + r')', re.I)
    for frame in page.frames:
        if not is_duo(frame.url):
            continue
        if _visible(frame, "input[name='passcode'], input[id*='passcode']") is not None:
            return True
        for locator in (frame.get_by_role('button', name=names), frame.get_by_role('link', name=names)):
            if any(locator.nth(index).is_visible() for index in range(locator.count())):
                return True
    return False


def wait_for_blackboard(context, page, base, cancel_event=None, timeout=60_000):
    """After Duo, wait until the browser holds an HTTP-verified Blackboard session."""
    deadline = time.monotonic() + timeout / 1000
    bb_host = urlsplit(base).hostname
    while time.monotonic() < deadline:
        if cancelled(cancel_event):
            raise LoginFlowError('Login cancelled.')
        if page.is_closed():
            raise LoginFlowError('Login browser was closed. Run bb login again.')
        if urlsplit(page.url).hostname == bb_host and urlsplit(page.url).path.startswith('/ultra'):
            remaining = max(1, min(2000, int((deadline - time.monotonic()) * 1000)))
            if is_verified(context, base, timeout=remaining):
                return
        page.wait_for_timeout(250)
    raise LoginFlowError('Timed out waiting for Blackboard after Duo. No verified session saved. Run bb login --manual.')


def wait_for_sso(context, page, username, password, base, detect_error, cancel_event=None, timeout=30_000):
    """Follow portal -> form (optional) -> Duo or HTTP-verified Blackboard.

    Returns DUO only when Duo shows a prompt (or stays silent past its grace
    period); a trusted device that Duo passes straight through returns
    AUTHENTICATED like an existing SSO session.

    Submit each credential step once. Navigation may remove a form entirely;
    each poll reclassifies the destination instead of waiting on that form.
    """
    deadline = time.monotonic() + timeout / 1000
    username_sent = password_sent = portal_clicked = False
    last_state = 'redirect'
    last_verification = 0.0
    duo_since = None
    bb_host = urlsplit(base).hostname
    while time.monotonic() < deadline:
        if cancelled(cancel_event):
            raise LoginFlowError('Login cancelled.')
        if page.is_closed():
            raise LoginFlowError('Login browser was closed. Run bb login again.')
        host = urlsplit(page.url).hostname
        remaining = max(1, min(1000, int((deadline - time.monotonic()) * 1000)))
        try:
            if is_duo(page.url):
                duo_since = duo_since or time.monotonic()
                last_state = 'Duo'
                now = time.monotonic()
                # Never time out on Duo: past grace or near the deadline, hand
                # over to the passcode workflow, which has its own checks.
                if now - duo_since >= DUO_GRACE_SECONDS or deadline - now < 1 or _duo_needs_user(page):
                    return DUO
            elif host == bb_host:
                if urlsplit(page.url).path.startswith('/ultra/'):
                    last_state = 'Blackboard session verification'
                    if time.monotonic() - last_verification >= 0.5:
                        last_verification = time.monotonic()
                        if is_verified(context, base, timeout=remaining):
                            if cancelled(cancel_event):
                                raise LoginFlowError('Login cancelled.')
                            return AUTHENTICATED
                elif not portal_clicked:
                    portal_clicked = _click(page, ('Log into Blackboard via myUMBC', 'UMBC Login', 'Sign in'), remaining)
            elif host == 'webauth.umbc.edu':
                has_error, error_type, _ = detect_error(page)
                if has_error:
                    # Use a fixed category; banner text can contain account data.
                    raise LoginFlowError(f'UMBC authentication failed ({error_type}). Check your UMBC account; use bb login --manual for recovery.')
                last_state = 'UMBC redirect or credential form'
                for frame in page.frames:
                    if urlsplit(frame.url).hostname != 'webauth.umbc.edu':
                        continue
                    user = _visible(frame, "input[name='j_username']")
                    if user is None:
                        user = _visible(frame, "input:is([type='text'], [type='email'])")
                    pwd = _visible(frame, "input[type='password']")
                    if pwd is not None and not password_sent:
                        if cancelled(cancel_event):
                            raise LoginFlowError('Login cancelled.')
                        if user is not None:
                            user.fill(username, timeout=remaining)
                        pwd.fill(password, timeout=remaining)
                        if cancelled(cancel_event):
                            raise LoginFlowError('Login cancelled.')
                        # Mark before action: a navigation timeout must not resubmit.
                        password_sent = True
                        last_state = 'UMBC authentication response'
                        if not _click(frame, ('Log In', 'Sign in', 'Login'), remaining):
                            pwd.press('Enter', timeout=remaining, no_wait_after=True)
                        break
                    if user is not None and pwd is None and not username_sent and not password_sent:
                        user.fill(username, timeout=remaining)
                        if cancelled(cancel_event):
                            raise LoginFlowError('Login cancelled.')
                        username_sent = True
                        user.press('Enter', timeout=remaining, no_wait_after=True)
                        break
            elif host:
                raise LoginFlowError('SSO reached an unexpected host. Run bb login --manual to inspect the login page.')
            elif page.url.startswith(('chrome-error:', 'edge-error:')):
                raise LoginFlowError('Browser navigation failed. Check your network and run bb login again.')
        except PlaywrightError as exc:
            # Only transient document/frame replacement or a bounded action
            # timeout is safe to re-observe.
            text = str(exc).lower()
            transient = ('execution context was destroyed', 'frame was detached', 'cannot find context')
            if not isinstance(exc, PlaywrightTimeoutError) and not any(marker in text for marker in transient):
                raise LoginFlowError('Browser failed during SSO. Check your connection and run bb login --manual.') from None
        if password_sent and duo_since is None:
            last_state = 'UMBC authentication response'
        page.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1000)))
    raise LoginFlowError(f'Timed out waiting for {last_state}. No verified session saved. Run bb login --manual to inspect the page.')
