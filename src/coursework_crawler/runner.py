from __future__ import annotations

import re
from pathlib import Path
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .adapters import AdapterRegistry
from .adapters.gradescope import GradescopeAdapter
from .adapters.brightspace import (
    BrightspaceAssignmentsAdapter,
    BrightspaceAnnouncementsAdapter,
    BrightspaceCalendarAdapter,
    BrightspaceContentAdapter,
    BrightspaceQuizzesAdapter,
)
from .adapters.webwork import WeBWorKAdapter
from .adapters.zybooks import ZyBooksAdapter
from .adapters.content_tree import BrightspaceContentTreeAdapter
from .adapters.tophat import TopHatAdapter, authenticated as tophat_authenticated
from .adapters.schedules import BrightspaceScheduleAdapter
from .auth import attempt_automatic_login
from .browser import PersistentBrowser
from .config import AppConfig
from .db import Database
from .models import CrawlResult, HealthStatus
from .models import SourceConfig


def default_registry(config: AppConfig | None = None) -> AdapterRegistry:
    registry = AdapterRegistry()
    registry.register("webwork", WeBWorKAdapter)
    registry.register("gradescope", GradescopeAdapter)
    registry.register("brightspace_assignments", BrightspaceAssignmentsAdapter)
    registry.register("brightspace_announcements", BrightspaceAnnouncementsAdapter)
    registry.register("brightspace_quizzes", BrightspaceQuizzesAdapter)
    registry.register("brightspace_content", BrightspaceContentAdapter)
    registry.register("brightspace_content_tree", BrightspaceContentTreeAdapter)
    registry.register("brightspace_calendar", BrightspaceCalendarAdapter)
    registry.register(
        "brightspace_schedule",
        lambda: BrightspaceScheduleAdapter(config.classes if config else ()),
    )
    registry.register("zybooks", ZyBooksAdapter)
    registry.register("tophat", TopHatAdapter)
    return registry


def crawl_with_interactive_auth(
    adapter: Any,
    page: Any,
    source: Any,
    *,
    headless: bool,
    input_fn: Callable[[str], str] | None = None,
) -> CrawlResult:
    """Crawl once, pausing only to let a manual user renew an expired session."""
    result = adapter.crawl(page, source)
    if result.health == HealthStatus.LOGIN_REQUIRED and not headless:
        print(f"{source.id}: {result.health.value} — {result.message}")
        reader = input if input_fn is None else input_fn
        reader(
            f"Sign in to {source.platform} in the open Chrome window. "
            "When the course page is visible, return here and press Enter to save the session and stop: "
        )
        print("Authentication session saved. Start a new crawl when ready.")
    return result


def check_source_auth(page: Any, source: SourceConfig) -> CrawlResult:
    """Verify a platform session without parsing or storing coursework."""
    try:
        page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
        if source.platform == "zybooks":
            try:
                page.wait_for_function(
                    "document.title && !document.title.includes('Loading...')",
                    timeout=15_000,
                )
            except Exception:
                pass
        body_text = page.locator("body").inner_text(timeout=15_000)
    except Exception as exc:
        return CrawlResult(
            source,
            HealthStatus.UNAVAILABLE,
            message=f"Authentication preflight could not load {source.platform}: {type(exc).__name__}.",
        )

    current_url = page.url.lower()
    if source.platform == "webwork":
        authenticated = not (
            "Not logged in." in body_text
            or page.locator('input[name="user"], input[autocomplete="username"]').count() > 0
            or "/login" in current_url
        )
    elif source.platform == "gradescope":
        authenticated = "/login" not in current_url and "Log in to Gradescope" not in body_text
    elif source.platform == "brightspace":
        host = (urlsplit(page.url).hostname or "").lower()
        if source.adapter == "brightspace_calendar":
            authenticated = (
                host == "brightspace.vanderbilt.edu"
                and "Calendar" in page.title()
                and "Sign in" not in page.title()
            )
        else:
            course_link = page.locator(f'a[href="/d2l/home/{source.source_course_id}"]')
            try:
                course_link.first.wait_for(state="attached", timeout=10_000)
            except Exception:
                pass
            authenticated = (
                host == "brightspace.vanderbilt.edu"
                and course_link.count() > 0
                and "Sign in" not in page.title()
            )
    elif source.platform == "tophat":
        try:
            page.get_by_role("navigation", name="Course View", exact=True).wait_for(timeout=20_000)
        except Exception:
            pass
        authenticated = tophat_authenticated(page, source)
    elif source.platform == "zybooks":
        host = (urlsplit(page.url).hostname or "").lower()
        authenticated = (
            host == "learn.zybooks.com"
            and page.get_by_role("button", name=re.compile(r"Profile for ")).count() > 0
        )
    else:
        return CrawlResult(
            source,
            HealthStatus.PARSER_FAILED,
            message=f"No authentication preflight is defined for {source.platform}.",
        )

    if not authenticated:
        return CrawlResult(
            source,
            HealthStatus.LOGIN_REQUIRED,
            message=f"{source.platform} login required; no coursework was crawled.",
        )
    return CrawlResult(
        source,
        HealthStatus.SUCCESS,
        message=f"{source.platform} authentication verified.",
    )


def authentication_preflight(
    context: Any,
    sources: tuple[SourceConfig, ...],
    *,
    headless: bool,
    input_fn: Callable[[str], str] | None = None,
    checker: Callable[[Any, SourceConfig], CrawlResult] = check_source_auth,
    authenticator: Callable[[Any, SourceConfig], bool] = attempt_automatic_login,
) -> tuple[tuple[CrawlResult, ...], Any | None]:
    """Check every enabled platform before crawling and collect login tabs."""
    representatives: dict[str, SourceConfig] = {}
    for source in sources:
        representatives.setdefault(source.platform, source)

    pages: dict[str, Any] = {}
    for index, (platform, source) in enumerate(representatives.items()):
        if index == 0 and context.pages:
            pages[platform] = context.pages[0]
        else:
            pages[platform] = context.new_page()

    pending = set(representatives)
    attempted_automatic_login: set[str] = set()
    while pending:
        results = {
            platform: checker(pages[platform], representatives[platform])
            for platform in tuple(pending)
        }
        hard_failures = tuple(
            result for result in results.values()
            if result.health not in {HealthStatus.SUCCESS, HealthStatus.LOGIN_REQUIRED}
        )
        if hard_failures:
            return hard_failures, None

        login_platforms = {
            platform for platform, result in results.items()
            if result.health == HealthStatus.LOGIN_REQUIRED
        }
        attempted_now = False
        for platform in sorted(login_platforms - attempted_automatic_login):
            attempted_automatic_login.add(platform)
            attempted_now = authenticator(pages[platform], representatives[platform]) or attempted_now
        if attempted_now:
            results.update({
                platform: checker(pages[platform], representatives[platform])
                for platform in login_platforms
            })
            hard_failures = tuple(
                result for result in results.values()
                if result.health not in {HealthStatus.SUCCESS, HealthStatus.LOGIN_REQUIRED}
            )
            if hard_failures:
                return hard_failures, None
            login_platforms = {
                platform for platform, result in results.items()
                if result.health == HealthStatus.LOGIN_REQUIRED
            }
        authenticated_platforms = pending - login_platforms
        pending = login_platforms

        if not pending:
            keeper = next(iter(pages.values()), None)
            for page in pages.values():
                if page is not keeper:
                    page.close()
            return (), keeper

        for platform in authenticated_platforms:
            pages[platform].close()
            pages.pop(platform, None)

        blockers = tuple(results[platform] for platform in sorted(pending))
        if headless:
            return blockers, None

        labels = ", ".join(sorted(pending))
        reader = input if input_fn is None else input_fn
        reader(
            f"Sign in to every open platform tab ({labels}). "
            "When all course pages are visible, return here and press Enter to recheck authentication: "
        )

    return (), next(iter(pages.values()), None)


def run_crawl(
    config: AppConfig,
    database: Database,
    profile_dir: Path,
    source_ids: set[str] | None = None,
    headless: bool = True,
) -> int:
    database.initialize()
    database.sync_sources(config.sources)
    database.sync_class_meetings(config.classes)
    run_id = database.start_run()
    registry = default_registry(config)
    selected = tuple(
        source for source in config.sources
        if source.enabled and (source_ids is None or source.id in source_ids)
    )
    failures = 0

    try:
        with PersistentBrowser(profile_dir, headless=headless).open() as context:
            preflight_failures, page = authentication_preflight(
                context,
                selected,
                headless=headless,
            )
            for result in preflight_failures:
                database.record_result(
                    run_id,
                    result,
                    config.missing_runs_before_inactive,
                )
                failures += 1
                print(f"{result.source.id}: {result.health.value} — {result.message}")

            for source in (() if preflight_failures else selected):
                assert page is not None
                try:
                    adapter = registry.create(source.adapter)
                except KeyError:
                    result = CrawlResult(
                        source,
                        HealthStatus.PARSER_FAILED,
                        message=f"Adapter {source.adapter!r} is not implemented yet.",
                    )
                else:
                    try:
                        result = crawl_with_interactive_auth(
                            adapter,
                            page,
                            source,
                            headless=headless,
                        )
                    except Exception as exc:
                        result = CrawlResult(
                            source,
                            HealthStatus.PARSER_FAILED,
                            message=f"Unexpected adapter failure: {type(exc).__name__}: {exc}",
                        )
                database.record_result(
                    run_id,
                    result,
                    config.missing_runs_before_inactive,
                )
                if result.health not in {
                    HealthStatus.SUCCESS,
                    HealthStatus.VERIFIED_ZERO,
                    HealthStatus.AMBIGUOUS,
                }:
                    failures += 1
                print(f"{source.id}: {result.health.value} — {result.message}")
                if result.health == HealthStatus.LOGIN_REQUIRED:
                    print("Authentication was required; the crawl stopped and all prior records were preserved.")
                    break
    except Exception as exc:
        failures = len(selected)
        for source in selected:
            result = CrawlResult(
                source,
                HealthStatus.UNAVAILABLE,
                message=f"Browser startup failed: {type(exc).__name__}: {exc}",
            )
            database.record_result(
                run_id,
                result,
                config.missing_runs_before_inactive,
            )
            print(f"{source.id}: {result.health.value} — {result.message}")

    database.finish_run(run_id, "partial" if failures else "success")
    return 1 if failures else 0
