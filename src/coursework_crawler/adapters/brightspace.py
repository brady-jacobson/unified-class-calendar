from __future__ import annotations

import json
import html
import re
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..models import CalendarEventRecord, CrawlResult, DeadlineRecord, HealthStatus, SourceConfig
from ..semantics import canonical_event_key
from .base import Adapter


DATE_VALUE = r"[A-Za-z]+ \d{1,2}, \d{4} \d{1,2}:\d{2} [AP]M"
DUE_PATTERN = re.compile(rf"Due on (?P<value>{DATE_VALUE})")
AVAILABLE_PATTERN = re.compile(
    rf"Available on (?P<start>{DATE_VALUE})(?: until (?P<end>{DATE_VALUE}))?"
)
CONTENT_DUE_PATTERN = re.compile(rf"Due(?::| on) (?P<value>{DATE_VALUE})")
CONTENT_AVAILABLE_RANGE_PATTERN = re.compile(
    rf"Available from (?P<start>{DATE_VALUE}) to (?P<end>{DATE_VALUE})"
)
CONTENT_AVAILABLE_START_PATTERN = re.compile(
    rf"(?:Availability started|Available from) (?P<start>{DATE_VALUE})"
)
CONTENT_AVAILABLE_END_PATTERN = re.compile(
    rf"(?:Availability ends|Available until) (?P<end>{DATE_VALUE})"
)
FEED_URL_PATTERN = re.compile(
    r"https://brightspace\.vanderbilt\.edu/d2l/le/calendar/feed/user/feed\.ics\?token=[^<\s]+"
)
URL_PATTERN = re.compile(r"https?://[^\s|<>]+")
CALENDAR_SUFFIXES = (
    (" - Availability Ends", "availability_end"),
    (" - Available", "available"),
    (" - Due", "due"),
)


def parse_brightspace_datetime(value: str | None, timezone_name: str) -> datetime | None:
    if not value:
        return None
    normalized = " ".join(value.split())
    for pattern in ("%b %d, %Y %I:%M %p", "%B %d, %Y %I:%M %p"):
        try:
            return datetime.strptime(normalized, pattern).replace(tzinfo=ZoneInfo(timezone_name))
        except ValueError:
            continue
    raise ValueError(f"Unsupported Brightspace date: {value!r}")


def parse_date_labels(text: str, timezone_name: str) -> dict[str, datetime | None]:
    normalized = " ".join(text.split())
    due_match = DUE_PATTERN.search(normalized)
    availability_match = AVAILABLE_PATTERN.search(normalized)
    return {
        "due_at": parse_brightspace_datetime(
            due_match.group("value") if due_match else None,
            timezone_name,
        ),
        "available_from": parse_brightspace_datetime(
            availability_match.group("start") if availability_match else None,
            timezone_name,
        ),
        "available_until": parse_brightspace_datetime(
            availability_match.group("end") if availability_match else None,
            timezone_name,
        ),
    }


def parse_content_date_labels(text: str, timezone_name: str) -> dict[str, datetime | None]:
    """Parse content metadata without ever treating availability as a due date."""
    normalized = " ".join(text.split())
    due_match = CONTENT_DUE_PATTERN.search(normalized)
    range_match = CONTENT_AVAILABLE_RANGE_PATTERN.search(normalized)
    start_match = range_match or CONTENT_AVAILABLE_START_PATTERN.search(normalized)
    end_match = range_match or CONTENT_AVAILABLE_END_PATTERN.search(normalized)
    return {
        "due_at": parse_brightspace_datetime(
            due_match.group("value") if due_match else None,
            timezone_name,
        ),
        "available_from": parse_brightspace_datetime(
            start_match.group("start") if start_match else None,
            timezone_name,
        ),
        "available_until": parse_brightspace_datetime(
            end_match.group("end") if end_match else None,
            timezone_name,
        ),
    }


def _unescape_ical(value: str) -> str:
    return html.unescape(
        value.replace(r"\n", "\n")
        .replace(r"\N", "\n")
        .replace(r"\,", ",")
        .replace(r"\;", ";")
        .replace(r"\\", "\\")
    ).strip()


def parse_ical_events(payload: str) -> tuple[dict[str, list[tuple[str, str]]], ...]:
    """Parse the small RFC 5545 subset emitted by Brightspace."""
    unfolded: list[str] = []
    for raw_line in payload.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw_line.startswith((" ", "\t")) and unfolded:
            unfolded[-1] += raw_line[1:]
        else:
            unfolded.append(raw_line)

    events: list[dict[str, list[tuple[str, str]]]] = []
    current: dict[str, list[tuple[str, str]]] | None = None
    for line in unfolded:
        if line == "BEGIN:VEVENT":
            current = {}
        elif line == "END:VEVENT" and current is not None:
            events.append(current)
            current = None
        elif current is not None and ":" in line:
            raw_key, value = line.split(":", 1)
            key, _, parameters = raw_key.partition(";")
            current.setdefault(key.upper(), []).append((parameters, value))
    return tuple(events)


def _ical_property(
    event: dict[str, list[tuple[str, str]]],
    name: str,
) -> tuple[str, str]:
    values = event.get(name, [])
    return values[0] if values else ("", "")


def parse_ical_datetime(parameters: str, value: str, timezone_name: str) -> tuple[datetime, bool]:
    all_day = "VALUE=DATE" in parameters.upper() or (len(value) == 8 and "T" not in value)
    if all_day:
        return datetime.strptime(value[:8], "%Y%m%d").replace(tzinfo=ZoneInfo(timezone_name)), True
    timezone_match = re.search(r"TZID=([^;:]+)", parameters, re.I)
    timezone = ZoneInfo(timezone_match.group(1)) if timezone_match else ZoneInfo(timezone_name)
    if value.endswith("Z"):
        return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC), False
    pattern = "%Y%m%dT%H%M%S" if len(value) >= 15 else "%Y%m%dT%H%M"
    return datetime.strptime(value[:15] if pattern.endswith("%S") else value[:13], pattern).replace(tzinfo=timezone), False


def _calendar_kind_and_title(summary: str) -> tuple[str, str]:
    for suffix, kind in CALENDAR_SUFFIXES:
        if summary.endswith(suffix):
            return kind, summary.removesuffix(suffix).strip()
    if re.search(r"\b(exam|midterm|test)\b", summary, re.I):
        return "exam", summary
    if re.search(r"\bquiz\b", summary, re.I):
        return "quiz", summary
    return "course_event", summary


def calendar_record_from_ical_event(
    source: SourceConfig,
    event: dict[str, list[tuple[str, str]]],
    timezone_name: str,
) -> CalendarEventRecord | None:
    mappings = source.options.get("course_mappings", [])
    _, raw_description = _ical_property(event, "DESCRIPTION")
    _, raw_location = _ical_property(event, "LOCATION")
    description = _unescape_ical(raw_description)
    location = _unescape_ical(raw_location)
    searchable = f"{location}\n{description}"
    org_ids = set(re.findall(r"[?&]ou=(\d+)", searchable))
    mapping = next(
        (
            candidate for candidate in mappings
            if str(candidate.get("org_unit_id", "")) in org_ids
            or str(candidate.get("label", "")).lower() in location.lower()
        ),
        None,
    )
    if not mapping:
        return None

    uid = _unescape_ical(_ical_property(event, "UID")[1])
    summary = _unescape_ical(_ical_property(event, "SUMMARY")[1])
    start_parameters, start_value = _ical_property(event, "DTSTART")
    end_parameters, end_value = _ical_property(event, "DTEND")
    if not uid or not summary or not start_value:
        return None
    starts_at, all_day = parse_ical_datetime(start_parameters, start_value, timezone_name)
    if end_value:
        ends_at, _ = parse_ical_datetime(end_parameters, end_value, timezone_name)
    else:
        ends_at = starts_at + (timedelta(days=1) if all_day else timedelta(minutes=30))

    start_bound = date.fromisoformat(str(source.options["active_term_start"]))
    end_bound = date.fromisoformat(str(source.options["active_term_end"]))
    if not start_bound <= starts_at.astimezone(ZoneInfo(timezone_name)).date() <= end_bound:
        return None
    if event.get("RRULE"):
        # Class meetings are sourced from the authoritative class schedule.
        return None

    kind, title = _calendar_kind_and_title(summary)
    configured_section = str(mapping.get("section", "")).lstrip("0")
    event_section = re.search(r"\bSection\s+0?([1-9])\b", title, re.I)
    if configured_section and event_section and event_section.group(1) != configured_section:
        return None
    urls = [_unescape_ical(url.rstrip('.,)"')) for url in URL_PATTERN.findall(description)]
    details_url = urls[0] if urls else source.url
    return CalendarEventRecord(
        course_id=str(mapping["course_id"]),
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=str(mapping["org_unit_id"]),
        source_event_id=uid,
        title=title,
        details_url=details_url,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        event_kind=kind,
        location=location or None,
        canonical_key=canonical_event_key(title, kind),
        raw_date_label=_unescape_ical(start_value),
    )


def _page_timezone(page: Any) -> str | None:
    raw = page.locator("[data-timezone]").first.get_attribute("data-timezone")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return data.get("identifier")


def _is_authenticated(page: Any, source: SourceConfig) -> bool:
    host = (urlsplit(page.url).hostname or "").lower()
    return (
        host == "brightspace.vanderbilt.edu"
        and page.locator(f'a[href="/d2l/home/{source.source_course_id}"]').count() > 0
        and "Sign in" not in page.title()
    )


def _course_identity(page: Any, source: SourceConfig) -> str:
    locator = page.locator(f'a[href="/d2l/home/{source.source_course_id}"]')
    if not locator.count():
        return ""
    values = [value.strip() for value in locator.all_inner_texts() if value.strip()]
    return max(values, key=len) if values else ""


def _within_active_term(record: DeadlineRecord, source: SourceConfig) -> bool:
    start_raw = source.options.get("active_term_start")
    end_raw = source.options.get("active_term_end")
    if not start_raw or not end_raw:
        return True
    start = date.fromisoformat(str(start_raw))
    end = date.fromisoformat(str(end_raw))
    values = [
        value for value in (
            record.available_from,
            record.due_at,
            record.available_until,
            record.late_due_at,
        ) if value is not None
    ]
    return not values or any(start <= value.date() <= end for value in values)


def assignment_record_from_row(
    source: SourceConfig,
    row: dict[str, str],
    timezone_name: str,
) -> DeadlineRecord:
    parsed = parse_date_labels(row["date_text"], timezone_name)
    absolute = urljoin(source.url, row["href"])
    item_id = parse_qs(urlsplit(absolute).query).get("db", [""])[0]
    if not item_id:
        raise ValueError("Brightspace assignment row did not expose a db identifier")
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=item_id,
        title=row["title"].strip(),
        details_url=absolute,
        available_from=parsed["available_from"],
        due_at=parsed["due_at"],
        available_until=parsed["available_until"],
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=" ".join(row["date_text"].split()) or None,
    )


def quiz_record_from_row(
    source: SourceConfig,
    row: dict[str, str],
    timezone_name: str,
) -> DeadlineRecord:
    parsed = parse_date_labels(row["date_text"], timezone_name)
    query = parse_qs(urlsplit(source.url).query)
    org_unit = query.get("ou", [source.source_course_id])[0]
    details_query = urlencode({"qi": row["item_id"], "ou": org_unit})
    details_url = urlunsplit(
        ("https", "brightspace.vanderbilt.edu", "/d2l/lms/quizzing/user/quiz_summary.d2l", details_query, "")
    )
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=row["item_id"],
        title=row["title"].strip(),
        details_url=details_url,
        available_from=parsed["available_from"],
        due_at=parsed["due_at"],
        available_until=parsed["available_until"],
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=" ".join(row["date_text"].split()) or None,
    )


def content_record_from_row(
    source: SourceConfig,
    row: dict[str, str],
    timezone_name: str,
) -> DeadlineRecord:
    parsed = parse_content_date_labels(row["date_text"], timezone_name)
    absolute = urljoin(source.url, row["href"])
    item_id = row.get("item_id") or urlsplit(absolute).path.rstrip("/").split("/")[-1]
    if not item_id:
        raise ValueError("Brightspace content row did not expose a topic identifier")
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=item_id,
        title=row["title"].strip(),
        details_url=absolute,
        available_from=parsed["available_from"],
        due_at=parsed["due_at"],
        available_until=parsed["available_until"],
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=" ".join(row["date_text"].split()) or None,
    )
class _BrightspaceBase(Adapter):
    empty_text: str
    expected_heading: str

    def _has_empty_state(self, page: Any) -> bool:
        return self.empty_text.lower() in page.locator("html").inner_html().lower()

    def _load(self, page: Any, source: SourceConfig) -> tuple[str, str, str] | CrawlResult:
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.locator(
                    f'a[href="/d2l/home/{source.source_course_id}"]'
                ).first.wait_for(state="attached", timeout=10_000)
            except Exception:
                # Authentication and identity checks below classify a real login
                # redirect or missing course more accurately than this render wait.
                pass
            body_text = page.locator("body").inner_text(timeout=15_000)
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.UNAVAILABLE,
                message=f"Could not load Brightspace: {type(exc).__name__}",
            )
        if not _is_authenticated(page, source):
            return CrawlResult(
                source,
                HealthStatus.LOGIN_REQUIRED,
                message="Brightspace login required or course session unavailable; prior records were retained.",
            )
        identity = _course_identity(page, source)
        if identity and self.expected_heading not in body_text:
            try:
                page.wait_for_function(
                    "heading => document.body.innerText.includes(heading)",
                    arg=self.expected_heading,
                    timeout=10_000,
                )
                body_text = page.locator("body").inner_text(timeout=5_000)
            except Exception:
                pass
        if not identity or self.expected_heading not in body_text:
            return CrawlResult(
                source,
                HealthStatus.COURSE_NOT_FOUND,
                message="The configured Brightspace course/page identity could not be verified.",
                course_identity=identity or None,
            )
        timezone_name = _page_timezone(page)
        if not timezone_name:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message="Brightspace did not expose the timezone used for rendered dates.",
                course_identity=identity,
            )
        return body_text, identity, timezone_name


class BrightspaceAssignmentsAdapter(_BrightspaceBase):
    empty_text = "There are currently no assignments available."
    expected_heading = "Assignments"

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        loaded = self._load(page, source)
        if isinstance(loaded, CrawlResult):
            return loaded
        body_text, identity, timezone_name = loaded
        try:
            rows: list[dict[str, str]] = page.locator('table[summary="List of assignments for this course"] tr').evaluate_all(
                """
                elements => elements.map(row => {
                  const cell = row.querySelector('th[scope="row"]');
                  const link = cell?.querySelector('a[href*="folder_submit_files"]');
                  return {
                    title: (link?.textContent || '').trim(),
                    href: link?.getAttribute('href') || '',
                    date_text: (cell?.textContent || '').trim(),
                    status: (row.querySelector('td')?.textContent || '').trim()
                  };
                }).filter(row => row.title && row.href)
                """
            )
            records = tuple(
                record for record in (
                    assignment_record_from_row(source, row, timezone_name) for row in rows
                ) if _within_active_term(record, source)
            )
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"Brightspace assignment parsing failed: {type(exc).__name__}: {exc}",
                course_identity=identity,
            )
        if not records and not self._has_empty_state(page) and rows:
            return CrawlResult(source, HealthStatus.VERIFIED_ZERO, (), "All visible rows were outside the active term.", identity)
        health = HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO
        return CrawlResult(
            source,
            health,
            records,
            f"Verified {len(records)} active-term assignment rows; {sum(r.due_at is not None for r in records)} have explicit due dates.",
            identity,
        )


class BrightspaceQuizzesAdapter(_BrightspaceBase):
    empty_text = "There are currently no quizzes available."
    expected_heading = "Quiz List"

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        loaded = self._load(page, source)
        if isinstance(loaded, CrawlResult):
            return loaded
        body_text, identity, timezone_name = loaded
        try:
            rows: list[dict[str, str]] = page.locator('a[onclick*="GoToQuiz("]').evaluate_all(
                r"""
                links => links.map(link => {
                  const row = link.closest('tr');
                  const onclick = link.getAttribute('onclick') || '';
                  return {
                    item_id: (onclick.match(/GoToQuiz\((\d+)/) || [])[1] || '',
                    title: (link.textContent || '').trim(),
                    date_text: (row?.querySelector('span.ds_b')?.textContent || '').trim(),
                    status: (row?.lastElementChild?.textContent || '').trim()
                  };
                }).filter(row => row.item_id && row.title)
                """
            )
            records = tuple(
                record for record in (
                    quiz_record_from_row(source, row, timezone_name) for row in rows
                ) if _within_active_term(record, source)
            )
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"Brightspace quiz parsing failed: {type(exc).__name__}: {exc}",
                course_identity=identity,
            )
        if not rows and not self._has_empty_state(page):
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message="No quiz rows or verified empty-state message were found.",
                course_identity=identity,
            )
        health = HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO
        return CrawlResult(
            source,
            health,
            records,
            f"Verified {len(records)} active-term quiz rows; {sum(r.due_at is not None for r in records)} have explicit due dates.",
            identity,
        )


class BrightspaceContentAdapter(Adapter):
    """Collect only topic rows nested in the configured Brightspace unit."""

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(
                "document.title && !document.title.startsWith('Loading')",
                timeout=25_000,
            )
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.UNAVAILABLE,
                message=f"Could not load Brightspace content: {type(exc).__name__}",
            )
        if not _is_authenticated(page, source):
            return CrawlResult(
                source,
                HealthStatus.LOGIN_REQUIRED,
                message="Brightspace login required or course session unavailable; prior records were retained.",
            )
        identity = _course_identity(page, source)
        if not identity:
            return CrawlResult(
                source,
                HealthStatus.COURSE_NOT_FOUND,
                message="The configured Brightspace course identity could not be verified.",
            )
        timezone_name = _page_timezone(page)
        if not timezone_name:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message="Brightspace did not expose the timezone used for rendered dates.",
                course_identity=identity,
            )

        unit_id = urlsplit(source.url).path.rstrip("/").split("/")[-1]
        try:
            frame = page.frame_locator("iframe")
            current = frame.locator(
                f'd2l-list-item-nav[key="{unit_id}"][current]'
            )
            current.wait_for(state="attached", timeout=25_000)
            if current.count() != 1:
                raise ValueError(f"Expected one active unit {unit_id}; found {current.count()}")
            completion = current.locator("d2l-toc-module-completion")
            completion.wait_for(state="attached", timeout=25_000)
            completion.evaluate(
                """
                element => new Promise(resolve => {
                  if (!element.hasAttribute('skeleton')) return resolve();
                  const observer = new MutationObserver(() => {
                    if (!element.hasAttribute('skeleton')) {
                      observer.disconnect();
                      resolve();
                    }
                  });
                  observer.observe(element, {attributes: true});
                })
                """
            )
            navigation_rows: list[dict[str, str]] = frame.locator(
                'd2l-list-item-nav[action-href]'
            ).evaluate_all(
                """
                elements => elements.map(row => {
                  const labels = Array.from(row.querySelectorAll('[aria-label]'))
                    .map(el => el.getAttribute('aria-label') || '')
                    .filter(Boolean);
                  const raw = [row.textContent || '', ...labels].join(' ');
                  return {
                    item_id: row.getAttribute('key') || '',
                    title: row.getAttribute('label') || '',
                    href: row.getAttribute('action-href') || '',
                    date_text: raw,
                    status: /\bCompleted\b/i.test(raw) ? 'Completed' : ''
                  };
                }).filter(row => row.item_id && row.title && row.href)
                """
            )
            rows = []
            inside_configured_unit = False
            for row in navigation_rows:
                if "/units/" in row["href"]:
                    if inside_configured_unit:
                        break
                    inside_configured_unit = row["item_id"] == unit_id
                    continue
                if inside_configured_unit and "/topics/" in row["href"]:
                    rows.append(row)
            records = tuple(
                record for record in (
                    content_record_from_row(source, row, timezone_name) for row in rows
                ) if _within_active_term(record, source)
            )
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"Brightspace content parsing failed: {type(exc).__name__}: {exc}",
                course_identity=identity,
            )

        if rows and not records:
            return CrawlResult(
                source,
                HealthStatus.VERIFIED_ZERO,
                (),
                "All visible content topics were outside the active term.",
                identity,
            )
        health = HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO
        return CrawlResult(
            source,
            health,
            records,
            f"Verified {len(records)} content topics; {sum(r.due_at is not None for r in records)} have explicit due dates.",
            identity,
        )


class BrightspaceCalendarAdapter(Adapter):
    """Import the user's private Brightspace ICS feed without persisting its token."""

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(2_000)
            body_text = page.locator("body").inner_text(timeout=15_000)
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.UNAVAILABLE,
                message=f"Could not load Brightspace calendar: {type(exc).__name__}",
            )
        if (urlsplit(page.url).hostname or "").lower() != "brightspace.vanderbilt.edu" or "Calendar" not in body_text:
            return CrawlResult(
                source,
                HealthStatus.LOGIN_REQUIRED,
                message="Brightspace calendar login required; prior calendar events were retained.",
            )
        timezone_name = _page_timezone(page) or "America/Chicago"
        subscribe_url = urljoin(
            source.url.rstrip("/") + "/",
            "subscribe/subscribeDialogLaunch?subscriptionOptionId=-1",
        )
        try:
            subscription = page.context.request.get(subscribe_url, timeout=30_000)
            if not subscription.ok:
                raise ValueError(f"subscription endpoint returned {subscription.status}")
            feed_match = FEED_URL_PATTERN.search(subscription.text())
            if not feed_match:
                raise ValueError("private feed address was not present")
            feed = page.context.request.get(html.unescape(feed_match.group(0)), timeout=30_000)
            if not feed.ok:
                raise ValueError(f"calendar feed returned {feed.status}")
            events = tuple(
                record for record in (
                    calendar_record_from_ical_event(source, event, timezone_name)
                    for event in parse_ical_events(feed.text())
                ) if record is not None
            )
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"Brightspace calendar import failed: {type(exc).__name__}: {exc}",
            )
        health = HealthStatus.SUCCESS if events else HealthStatus.VERIFIED_ZERO
        due_count = sum(event.event_kind == "due" for event in events)
        return CrawlResult(
            source,
            health,
            (),
            f"Verified {len(events)} active-term calendar events; {due_count} are explicit due events.",
            "Brightspace Calendar",
            events,
        )
