from __future__ import annotations

import re
import subprocess
from typing import Any

from .credentials import Credentials, KeychainCredentialStore
from .models import SourceConfig


def _notify(title: str, message: str) -> None:
    script = (
        "on run argv\n"
        "display notification (item 2 of argv) with title (item 1 of argv)\n"
        "end run"
    )
    subprocess.run(
        ["/usr/bin/osascript", "-e", script, title, message],
        capture_output=True,
        text=True,
    )


def _click_and_settle(page: Any, locator: Any, timeout: int = 30_000) -> None:
    locator.click()
    # wait_for_load_state can return immediately for the page that existed
    # before the click. A short settle gives form navigation time to start
    # before the preflight deliberately revisits the protected course URL.
    page.wait_for_timeout(min(timeout, 2_000))


def _login_webwork(page: Any, credentials: Credentials) -> bool:
    username = page.locator('input[name="user"]')
    password = page.locator('input[name="passwd"]')
    if not username.count() or not password.count():
        return False
    username.fill(credentials.username)
    password.fill(credentials.password)
    _click_and_settle(page, page.locator('#login_form input[type="submit"]'))
    return True


def _login_gradescope(page: Any, credentials: Credentials) -> bool:
    username = page.locator('#session_email')
    password = page.locator('#session_password')
    if not username.count() or not password.count():
        return False
    username.fill(credentials.username)
    password.fill(credentials.password)
    remember = page.locator('#session_remember_me')
    if remember.count() and not remember.is_checked():
        # Gradescope visually overlays the native checkbox with its label.
        remember.check(force=True)
    _click_and_settle(page, page.locator('input[type="submit"][name="commit"]'))
    return True


def _login_gradescope_sso(page: Any, credentials: Credentials) -> bool:
    school_credentials = page.get_by_role("link", name="School Credentials")
    if school_credentials.count():
        _click_and_settle(page, school_credentials)
    vanderbilt = page.get_by_role("link", name="Vanderbilt University")
    if not vanderbilt.count():
        return False
    _click_and_settle(page, vanderbilt)
    return _login_onevu(page, credentials)


def _gradescope_still_logged_out(page: Any) -> bool:
    return "/login" in page.url.lower() or page.locator("#session_email").count() > 0


def _login_zybooks(page: Any, credentials: Credentials) -> bool:
    username = page.locator('input[autocomplete="email"]')
    password = page.locator('input[autocomplete="current-password"]')
    if not username.count() or not password.count():
        return False
    username.fill(credentials.username)
    password.fill(credentials.password)
    _click_and_settle(page, page.get_by_role("button", name="Sign in"))
    return True


def _onevu_push(page: Any) -> bool:
    push = page.get_by_role(
        "link",
        name=re.compile(r"Select to get a push notification", re.I),
    )
    if not push.count():
        return False
    _notify("Approve Vanderbilt sign-in", "Approve the Okta Verify push to refresh coursework sessions.")
    _click_and_settle(page, push.first)
    try:
        page.wait_for_url(re.compile(r"brightspace\.vanderbilt\.edu"), timeout=90_000)
    except Exception:
        return True
    return True


def _login_onevu(page: Any, credentials: Credentials | None) -> bool:
    if _onevu_push(page):
        return True

    identifier = page.locator('input[name="identifier"], input[autocomplete="username"]')
    if identifier.count():
        if credentials is None:
            return False
        identifier.first.fill(credentials.username)
        remember = page.locator('input[name="rememberMe"]')
        if remember.count() and not remember.is_checked():
            remember.check(force=True)
        _click_and_settle(page, page.locator('input[type="submit"], button[type="submit"]').last)

    password = page.locator(
        'input[name="credentials.passcode"], input[autocomplete="current-password"], input[type="password"]'
    )
    if password.count():
        if credentials is None:
            return False
        password.first.fill(credentials.password)
        _click_and_settle(page, page.locator('input[type="submit"], button[type="submit"]').last)

    return _onevu_push(page) or True


def attempt_automatic_login(
    page: Any,
    source: SourceConfig,
    store: KeychainCredentialStore | None = None,
) -> bool:
    """Attempt one credential submission; never retry a failed password in one run."""
    store = store or KeychainCredentialStore()
    try:
        credentials = store.for_platform(source.platform)
        if source.platform == "webwork":
            return bool(credentials and _login_webwork(page, credentials))
        if source.platform == "gradescope":
            if credentials and _login_gradescope(page, credentials):
                if not _gradescope_still_logged_out(page):
                    return True
            vanderbilt = store.get("vanderbilt")
            return bool(vanderbilt and _login_gradescope_sso(page, vanderbilt))
        if source.platform == "zybooks":
            return bool(credentials and _login_zybooks(page, credentials))
        if source.platform == "brightspace":
            return _login_onevu(page, credentials)
    except Exception:
        return False
    return False
