from __future__ import annotations

from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from ..models import CrawlResult, DeadlineRecord, HealthStatus, SourceConfig
from ..semantics import canonical_event_key
from .base import Adapter


def parse_gradescope_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S %z")


def _timezone_name(header_title: str) -> str | None:
    if "Central Time" in header_title:
        return "America/Chicago"
    if "Eastern Time" in header_title:
        return "America/New_York"
    if "Pacific Time" in header_title:
        return "America/Los_Angeles"
    return header_title.strip() or None


def record_from_row(
    source: SourceConfig,
    row: dict[str, str],
    timezone_name: str | None,
) -> DeadlineRecord:
    details_path = row.get("details_url", "")
    details_url = urljoin(source.url, details_path) if details_path else source.url
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=row["item_id"],
        title=row["title"].strip(),
        details_url=details_url,
        available_from=parse_gradescope_datetime(row.get("released_at")),
        due_at=parse_gradescope_datetime(row.get("due_at")),
        late_due_at=parse_gradescope_datetime(row.get("late_due_at")),
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=row.get("raw_date_label") or None,
        canonical_key=canonical_event_key(row["title"], "due"),
        component_kind="submission",
    )


class GradescopeAdapter(Adapter):
    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            body_text = page.locator("body").inner_text(timeout=15_000)
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.UNAVAILABLE,
                message=f"Could not load Gradescope: {type(exc).__name__}",
            )

        if "/login" in page.url.lower() or "Log in to Gradescope" in body_text:
            return CrawlResult(
                source,
                HealthStatus.LOGIN_REQUIRED,
                message="Gradescope login required; prior records were retained.",
            )

        heading = page.locator("main h1").first.inner_text().strip() if page.locator("main h1").count() else ""
        course_marker = f"Course ID: {source.source_course_id}"
        if course_marker not in body_text or not heading:
            return CrawlResult(
                source,
                HealthStatus.COURSE_NOT_FOUND,
                message="The configured Gradescope course identity could not be verified.",
                course_identity=heading or None,
            )

        table = page.locator("#assignments-student-table")
        if table.count() != 1:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message="The Gradescope assignments table was not found.",
                course_identity=heading,
            )

        try:
            timezone_title = table.locator("thead abbr").first.get_attribute("title") or ""
            timezone_name = _timezone_name(timezone_title)
            rows: list[dict[str, str]] = table.locator("tbody tr").evaluate_all(
                """
                elements => elements.map(row => {
                  const primary = row.querySelector('th.table--primaryLink');
                  const action = primary?.querySelector('a, button');
                  const times = Array.from(row.querySelectorAll('time'));
                  const byLabel = prefix => times.find(t => (t.getAttribute('aria-label') || '').startsWith(prefix));
                  const release = byLabel('Released at');
                  const due = byLabel('Due at');
                  const late = byLabel('Late Due Date at');
                  const href = action?.getAttribute('href')
                    || action?.getAttribute('data-images-url')
                    || action?.getAttribute('data-post-url')
                    || '';
                  const assignmentId = action?.getAttribute('data-assignment-id')
                    || (href.match(/assignments\\/(\\d+)/) || [])[1]
                    || '';
                  return {
                    item_id: assignmentId,
                    title: (action?.getAttribute('data-assignment-title') || action?.textContent || '').trim(),
                    details_url: href,
                    status: (row.querySelector('.submissionStatus--text')?.textContent || '').trim(),
                    released_at: release?.getAttribute('datetime') || '',
                    due_at: due?.getAttribute('datetime') || '',
                    late_due_at: late?.getAttribute('datetime') || '',
                    raw_date_label: times.map(t => t.getAttribute('aria-label') || t.textContent?.trim() || '').join(' | ')
                  };
                }).filter(row => row.item_id && row.title)
                """
            )
            records = tuple(record_from_row(source, row, timezone_name) for row in rows)
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"Gradescope page structure or date parsing failed: {type(exc).__name__}: {exc}",
                course_identity=heading,
            )

        health = HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO
        message = (
            f"Verified {len(records)} assignment rows; "
            f"{sum(record.due_at is not None for record in records)} have explicit due dates."
        )
        return CrawlResult(
            source,
            health,
            records,
            message=message,
            course_identity=heading,
        )
