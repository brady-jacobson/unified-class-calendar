from __future__ import annotations

import re
from datetime import datetime
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..models import CrawlResult, DeadlineRecord, HealthStatus, SourceConfig
from .base import Adapter


DATE_PATTERN = re.compile(
    r"(?P<label>Due|Will open on) "
    r"(?P<month>[A-Za-z]+) (?P<day>\d{1,2}), (?P<year>\d{4}) at "
    r"(?P<clock>\d{1,2}:\d{2}:\d{2} [AP]M) (?P<zone>[A-Z]{3,4})"
)

ZONE_NAMES = {
    "CDT": "America/Chicago",
    "CST": "America/Chicago",
    "EDT": "America/New_York",
    "EST": "America/New_York",
}


def parse_webwork_date(text: str) -> tuple[str, datetime, str] | None:
    match = DATE_PATTERN.search(" ".join(text.split()))
    if not match:
        return None
    zone_label = match.group("zone")
    zone_name = ZONE_NAMES.get(zone_label)
    if zone_name is None:
        raise ValueError(f"Unsupported WeBWorK timezone abbreviation: {zone_label}")
    naive = datetime.strptime(
        " ".join(
            [
                match.group("month"), match.group("day"), match.group("year"),
                match.group("clock"),
            ]
        ),
        "%B %d %Y %I:%M:%S %p",
    )
    return match.group("label"), naive.replace(tzinfo=ZoneInfo(zone_name)), zone_name


def canonicalize_webwork_url(base_url: str, href: str) -> str:
    absolute = urljoin(base_url, href)
    parts = urlsplit(absolute)
    query = [(key, value) for key, value in parse_qsl(parts.query) if key != "effectiveUser"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def item_id_from_url(url: str) -> str:
    path = urlsplit(url).path.rstrip("/")
    return path.rsplit("/", 1)[-1]


def record_from_row(source: SourceConfig, row: dict[str, str]) -> DeadlineRecord:
    raw_label = " ".join(row["date_text"].split())
    parsed = parse_webwork_date(raw_label)
    due_at = None
    available_from = None
    timezone = None
    if parsed:
        label, value, timezone = parsed
        if label == "Due":
            due_at = value
        elif label == "Will open on":
            available_from = value

    details_url = canonicalize_webwork_url(source.url, row["href"])
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=item_id_from_url(details_url),
        title=row["title"].strip(),
        details_url=details_url,
        available_from=available_from,
        due_at=due_at,
        timezone=timezone,
        status=row.get("status") or None,
        raw_date_label=raw_label or None,
    )


class WeBWorKAdapter(Adapter):
    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            body_text = page.locator("body").inner_text(timeout=15_000)
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.UNAVAILABLE,
                message=f"Could not load WeBWorK: {type(exc).__name__}",
            )

        if (
            "Not logged in." in body_text
            or page.locator('input[name="user"], input[autocomplete="username"]').count() > 0
            or "/login" in page.url.lower()
        ):
            return CrawlResult(
                source,
                HealthStatus.LOGIN_REQUIRED,
                message="WeBWorK login required; prior records were retained.",
            )

        heading = page.locator("h1").first.inner_text().strip() if page.locator("h1").count() else ""
        if source.source_course_id not in page.url or not heading:
            return CrawlResult(
                source,
                HealthStatus.COURSE_NOT_FOUND,
                message="The configured WeBWorK course identity could not be verified.",
                course_identity=heading or None,
            )

        try:
            rows: list[dict[str, str]] = page.locator("li[data-set-status]").evaluate_all(
                """
                elements => elements.map(row => {
                  const link = row.querySelector('a.fw-bold');
                  const date = row.querySelector('.font-sm');
                  return {
                    title: link?.textContent?.trim() || '',
                    href: link?.getAttribute('href') || '',
                    date_text: date?.textContent?.trim() || '',
                    status: row.getAttribute('data-set-status') || ''
                  };
                }).filter(row => row.title && row.href)
                """
            )
            records = tuple(record_from_row(source, row) for row in rows)
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"WeBWorK page structure or date parsing failed: {type(exc).__name__}: {exc}",
                course_identity=heading,
            )

        health = HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO
        return CrawlResult(
            source,
            health,
            records,
            message=f"Verified {len(records)} assignment rows; {sum(r.due_at is not None for r in records)} have explicit due dates.",
            course_identity=heading,
        )
