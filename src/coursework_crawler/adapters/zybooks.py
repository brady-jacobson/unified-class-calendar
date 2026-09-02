from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ..models import CrawlResult, DeadlineRecord, HealthStatus, SourceConfig
from .base import Adapter


ZYBOOKS_DUE_PATTERN = re.compile(
    r"Due:\s*(?P<date>\d{1,2}/\d{1,2}/\d{4}),\s*"
    r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s*(?P<zone>[A-Z]{3,4})"
)

ZONE_NAMES = {
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "EDT": "America/New_York",
    "EST": "America/New_York",
}


def parse_zybooks_due(text: str) -> tuple[datetime, str] | None:
    normalized = " ".join(text.split())
    match = ZYBOOKS_DUE_PATTERN.search(normalized)
    if not match:
        return None
    timezone_name = ZONE_NAMES.get(match.group("zone"))
    if timezone_name is None:
        raise ValueError(f"Unsupported zyBooks timezone: {match.group('zone')}")
    value = datetime.strptime(
        f"{match.group('date')} {match.group('time')}",
        "%m/%d/%Y %I:%M %p",
    ).replace(tzinfo=ZoneInfo(timezone_name))
    return value, timezone_name


def record_from_row(source: SourceConfig, row: dict[str, str]) -> DeadlineRecord:
    parsed = parse_zybooks_due(row["date_text"])
    due_at = parsed[0] if parsed else None
    timezone_name = parsed[1] if parsed else None
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=row["item_id"],
        title=row["title"].strip(),
        # Clicking the live assignment card opens its details in-place and leaves
        # this course URL unchanged; zyBooks exposes no per-assignment URL.
        details_url=row.get("details_url") or source.url,
        due_at=due_at,
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=" ".join(row["date_text"].split()) or None,
    )


class ZyBooksAdapter(Adapter):
    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(
                "document.title && !document.title.includes('Loading...')",
                timeout=25_000,
            )
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.UNAVAILABLE,
                message=f"Could not load zyBooks course content: {type(exc).__name__}",
            )

        host = (urlsplit(page.url).hostname or "").lower()
        profile_button = page.get_by_role("button", name=re.compile(r"Profile for ")).count()
        if host != "learn.zybooks.com" or not profile_button:
            return CrawlResult(
                source,
                HealthStatus.LOGIN_REQUIRED,
                message="zyBooks login required; prior records were retained.",
            )

        course_link = page.locator(f'a[href="/zybook/{source.source_course_id}"]')
        heading = page.get_by_role("heading", level=1)
        identity = heading.first.inner_text().strip() if heading.count() else ""
        if not course_link.count() or not identity:
            return CrawlResult(
                source,
                HealthStatus.COURSE_NOT_FOUND,
                message="The configured zyBooks course identity could not be verified.",
                course_identity=identity or None,
            )

        assignments_tab = page.get_by_role("tab", name="Assignments")
        if assignments_tab.count() != 1:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message="The zyBooks Assignments tab was not found.",
                course_identity=identity,
            )
        try:
            if assignments_tab.get_attribute("aria-selected") != "true":
                assignments_tab.click()
            panel = page.get_by_role("tabpanel", name="Assignments")
            panel.wait_for(state="visible", timeout=10_000)
            cards = page.locator('.assignment-summary[assignment_id]')
            try:
                cards.first.wait_for(state="attached", timeout=10_000)
            except Exception:
                panel_text = panel.inner_text().lower()
                empty_markers = ("no assignments", "haven't been assigned", "not been assigned")
                if not any(marker in panel_text for marker in empty_markers):
                    return CrawlResult(
                        source,
                        HealthStatus.PARSER_FAILED,
                        message="zyBooks loaded the Assignments panel but exposed neither assignment cards nor a verified empty state.",
                        course_identity=identity,
                    )
            rows: list[dict[str, str]] = cards.evaluate_all(
                """
                elements => elements.map(row => ({
                  item_id: row.getAttribute('assignment_id') || '',
                  title: (row.querySelector('.assignment-title')?.textContent || '').trim(),
                  date_text: (row.querySelector('.due-date-text')?.textContent || '').trim(),
                  status: (row.querySelector('.assignment-points-text')?.textContent || '').trim(),
                  details_url: location.href
                })).filter(row => row.item_id && row.title)
                """
            )
            records = tuple(record_from_row(source, row) for row in rows)
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"zyBooks assignment parsing failed: {type(exc).__name__}: {exc}",
                course_identity=identity,
            )

        health = HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO
        return CrawlResult(
            source,
            health,
            records,
            f"Verified {len(records)} assignment rows; {sum(r.due_at is not None for r in records)} have explicit due dates.",
            identity,
        )
