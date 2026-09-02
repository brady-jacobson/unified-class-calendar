from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class PersistentBrowser:
    """A dedicated Chrome profile whose cookies survive scheduled runs."""

    def __init__(self, profile_dir: Path, headless: bool = True):
        self.profile_dir = profile_dir
        self.headless = headless
        self.auth_state_path = profile_dir / "auth-state.json"

    def _restore_auth_state(self, context: object) -> None:
        if not self.auth_state_path.exists():
            return
        try:
            state = json.loads(self.auth_state_path.read_text(encoding="utf-8"))
            cookies = state.get("cookies", [])
            if cookies:
                context.add_cookies(cookies)
        except (OSError, ValueError, TypeError):
            # A corrupt cache must not make every source look healthy. The adapters
            # will detect the resulting login page and report login_required.
            return

    def _save_auth_state(self, context: object) -> None:
        context.storage_state(path=str(self.auth_state_path))
        os.chmod(self.auth_state_path, 0o600)

    @contextmanager
    def open(self) -> Iterator[object]:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise RuntimeError(
                "Playwright is not installed. Run: pip install -e . && playwright install chrome"
            ) from exc

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                channel="chrome",
                headless=self.headless,
                viewport={"width": 1440, "height": 1000},
            )
            self._restore_auth_state(context)
            try:
                yield context
            finally:
                try:
                    self._save_auth_state(context)
                except Exception:
                    # Chrome can be closed by the user or interrupted before the
                    # context manager exits. Adapters will report login_required
                    # on the next run if no usable state was saved.
                    pass
                try:
                    context.close()
                except Exception:
                    pass
