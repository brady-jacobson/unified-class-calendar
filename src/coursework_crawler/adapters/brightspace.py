from __future__ import annotations

import json
import html
import hashlib
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from ..models import CalendarEventRecord, CrawlResult, DeadlineRecord, HealthStatus, SourceConfig
from ..semantics import canonical_event_key
from ..links import clean_resource_url
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
ANNOUNCEMENT_DUE_PATTERN = re.compile(
    rf"\b(?:due(?:\s+date)?|deadline)(?:\s+is|\s+on|:)?\s+"
    rf"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),?\s+)?"
    rf"(?P<value>{DATE_VALUE})(?:\s+[A-Z]{{3,4}})?",
    re.I,
)
ANNOUNCEMENT_WORK_PATTERN = re.compile(
    r"\b(?:homework|hw\s*[-#]?\s*\d+|assignment|quiz|exam|test|project|lab|pre-?lab|report|"
    r"reading|worksheet|problem\s+set|pair[- ]share|survey|form|zy\s*[-#]?\s*\d+)\b",
    re.I,
)
ANNOUNCEMENT_ACTION_PATTERN = re.compile(
    r"\b(?:due|deadline|submit|complete|fill(?:ing)?\s+out|released|assigned|"
    r"extended|postponed|rescheduled|moved|changed|remind(?:er|ing)?)\b",
    re.I,
)
COURSEWORK_ID_PATTERN = re.compile(
    r"\b(?:HW|Homework|Written\s+Homework|ZY|Quiz|Exam|Test|Lab|Project)"
    r"\s*[-#]?\s*\d+(?:\.\d+)?\b",
    re.I,
)
RELATIVE_TIMING_PATTERN = re.compile(
    r"\b(?:before\s+(?:the\s+)?(?:start|beginning|next|subsequent|class|lab)|"
    r"at\s+the\s+(?:start|beginning)|prior\s+to\s+(?:your|the)\s+lab|during\s+the\s+week\s+of|"
    r"after\s+(?:the\s+)?lab|within\s+\d+\s+(?:hours?|days?))\b",
    re.I,
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


def parse_brightspace_api_datetime(value: str | None) -> datetime | None:
    """Parse the ISO timestamps returned by Brightspace's authenticated API."""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


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


def announcement_record_from_entry(
    source: SourceConfig,
    entry: dict[str, Any],
    timezone_name: str,
) -> DeadlineRecord | None:
    """Create a conservative obligation from an actionable announcement."""
    title = " ".join(str(entry.get("title", "")).split())
    body = " ".join(str(entry.get("body", "")).split())
    combined = f"{title}. {body}".strip()
    if not (
        ANNOUNCEMENT_WORK_PATTERN.search(combined)
        and ANNOUNCEMENT_ACTION_PATTERN.search(combined)
    ):
        return None

    matches = list(ANNOUNCEMENT_DUE_PATTERN.finditer(combined))
    due_match = matches[0] if len({m.group("value") for m in matches}) == 1 else None
    due_at = parse_brightspace_datetime(
        due_match.group("value") if due_match else None,
        timezone_name,
    )
    timing_sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", combined)
        if sentence.strip()
        and re.search(
            r"\b(?:due|deadline|within\s+\d+\s+hours?|by\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)|"
            r"before\s+(?:class|lab)|at\s+the\s+(?:start|beginning))\b",
            sentence,
            re.I,
        )
    ]
    timing_text = " ".join(timing_sentences)[:800] or None
    href = urljoin(source.url, str(entry.get("href", "")))
    parsed_url = urlsplit(href)
    query = parse_qs(parsed_url.query)
    item_id = (
        str(entry.get("item_id", ""))
        or query.get("newsId", [""])[0]
        or hashlib.sha256(href.encode("utf-8")).hexdigest()[:20]
    )
    links: list[tuple[str, str]] = []
    for raw in entry.get("links", []):
        if not isinstance(raw, (list, tuple)) or len(raw) != 2:
            continue
        label, raw_href = (" ".join(str(raw[0]).split()), str(raw[1]).strip())
        absolute = clean_resource_url(urljoin(href, raw_href))
        if absolute is None:
            continue
        if not label or not absolute.startswith(("https://", "http://")) or absolute == href:
            continue
        pair = (label[:100], absolute)
        if pair not in links:
            links.append(pair)
    canonical_title = coursework_canonical_key(combined) or canonical_event_key(title, "due")
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=item_id,
        title=title,
        details_url=href,
        due_at=due_at,
        timezone=timezone_name if due_at else None,
        status="announcement",
        raw_date_label=due_match.group(0) if due_match else timing_text,
        timing_text=None if due_at else (timing_text or body or title),
        description=body or None,
        canonical_key=canonical_title,
        component_kind="announcement",
        related_links=tuple(links),
    )


def coursework_canonical_key(text: str) -> str | None:
    normalized = " ".join(text.split())
    identity = COURSEWORK_ID_PATTERN.search(normalized)
    if identity:
        return canonical_event_key(identity.group(0), "due")
    if re.search(r"\bsoftware\s+setup\b", normalized, re.I):
        return "todo:software-setup"
    if re.search(r"\bpair[- ]share\b", normalized, re.I):
        return "todo:pair-share"
    return None


def component_kind_from_text(text: str) -> str | None:
    normalized = " ".join(text.split()).lower()
    if re.search(r"\bpre-?lab\b", normalized) and not re.search(
        r"\bno\s+pre-?lab\b|pre-?lab\s+(?:is\s+)?not\s+required",
        normalized,
    ):
        return "pre-lab"
    for pattern, label in (
        (r"\breport\b", "report"),
        (r"\bfaq\b|frequently asked", "FAQ"),
        (r"\bpolicy\b|late days?", "policy"),
        (r"\bquiz\b", "quiz"),
        (r"\bprompt\b|problem set|assignment pdf", "prompt"),
        (r"\bsetup\b|install\b", "preparation"),
        (r"\bsubmission\b|submit\b", "submission"),
    ):
        if re.search(pattern, normalized):
            return label
    return None


def extract_relative_timing(text: str) -> str | None:
    """Return explicit relational wording; never turn it into a timestamp."""
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+|[\r\n]+", " ".join(text.split()))
        if sentence.strip() and RELATIVE_TIMING_PATTERN.search(sentence)
    ]
    return " ".join(dict.fromkeys(sentences))[:1200] or None


def enrich_brightspace_record(
    record: DeadlineRecord,
    body: str,
    links: list[list[str]] | tuple[tuple[str, str], ...],
) -> DeadlineRecord:
    cleaned_links: list[tuple[str, str]] = []
    for raw in links:
        if len(raw) != 2:
            continue
        label, href = " ".join(str(raw[0]).split()), str(raw[1]).strip()
        absolute = clean_resource_url(urljoin(record.details_url, href))
        if absolute is None:
            continue
        if (
            not label
            or not absolute.startswith(("https://", "http://"))
            or absolute == record.details_url
            or re.search(r"/(?:logout|account/settings)(?:[/?]|$)", absolute, re.I)
        ):
            continue
        pair = (label[:100], absolute)
        if pair not in cleaned_links:
            cleaned_links.append(pair)
    combined = f"{record.title} {body}"
    return replace(
        record,
        timing_text=record.timing_text or (None if record.due_at else extract_relative_timing(body)),
        description=" ".join(body.split()) or record.description,
        canonical_key=record.canonical_key or coursework_canonical_key(combined),
        component_kind=record.component_kind or component_kind_from_text(combined),
        related_links=tuple(cleaned_links),
    )


def split_lab_obligations(record: DeadlineRecord, body: str) -> tuple[DeadlineRecord, ...]:
    """Split explicit pre-lab/report requirements while retaining relational wording."""
    if record.due_at is not None:
        return (record,)
    normalized = " ".join(body.split())
    lab_identity = COURSEWORK_ID_PATTERN.search(f"{record.title} {normalized}")
    lab_label = lab_identity.group(0) if lab_identity else record.title
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", normalized)
        if sentence.strip()
    ]
    records: list[DeadlineRecord] = []
    prelab = " ".join(
        sentence for sentence in sentences
        if re.search(r"\bpre-?lab\b", sentence, re.I)
        and not re.search(r"\bno\s+pre-?lab\b|not\s+required", sentence, re.I)
        and RELATIVE_TIMING_PATTERN.search(sentence)
    )
    report = " ".join(
        sentence for sentence in sentences
        if re.search(r"\breport\b", sentence, re.I)
        and RELATIVE_TIMING_PATTERN.search(sentence)
    )
    if prelab:
        records.append(
            replace(
                record,
                source_item_id=f"{record.source_item_id}:prelab",
                title=f"{lab_label} pre-lab requirement",
                timing_text=prelab[:1200],
                component_kind="pre-lab",
            )
        )
    if report:
        records.append(
            replace(
                record,
                source_item_id=f"{record.source_item_id}:report",
                title=f"{lab_label} report",
                timing_text=report[:1200],
                component_kind="report",
            )
        )
    return tuple(records) or (record,)


def _page_detail_payload(page: Any) -> tuple[str, list[list[str]]]:
    from .content_tree import html_details

    texts: list[str] = []
    links: list[list[str]] = []
    # Assignment instructions are rendered inside this component; body.innerText
    # excludes its shadow tree and instead captures the submission form chrome.
    blocks = page.locator("d2l-html-block[html]")
    if blocks.count():
        for raw in blocks.evaluate_all("xs => xs.map(x => x.getAttribute('html') || '')"):
            body, block_links = html_details(raw)
            if body:
                texts.append(body)
            links.extend(block_links)
        attachment_links = page.locator('a[href*="download"], a[href*="fileId="]')
        links.extend(attachment_links.evaluate_all(
            "xs => xs.map(x => [(x.textContent || '').trim(), x.href])"
        ))
        if texts:
            return "\n".join(texts), links
    for frame in page.frames:
        try:
            text = frame.locator("body").inner_text(timeout=5_000)
            if text and text not in texts:
                texts.append(text)
            frame_links: list[list[str]] = frame.locator("a[href]").evaluate_all(
                """
                elements => elements.map(link => [
                  (link.textContent || link.getAttribute('aria-label') || '').trim(),
                  link.getAttribute('href') || ''
                ])
                """
            )
            links.extend(frame_links)
        except Exception:
            continue
    return "\n".join(texts), links


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
    title = row["title"].strip()
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=item_id,
        title=title,
        details_url=absolute,
        available_from=parsed["available_from"],
        due_at=parsed["due_at"],
        available_until=parsed["available_until"],
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=" ".join(row["date_text"].split()) or None,
        canonical_key=coursework_canonical_key(title),
        component_kind=component_kind_from_text(title),
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
    title = row["title"].strip()
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=row["item_id"],
        title=title,
        details_url=details_url,
        available_from=parsed["available_from"],
        due_at=parsed["due_at"],
        available_until=parsed["available_until"],
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=" ".join(row["date_text"].split()) or None,
        canonical_key=coursework_canonical_key(title),
        component_kind="quiz",
        timing_text=None if parsed["due_at"] else "No explicit due time published for this quiz.",
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
    title = row["title"].strip()
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=item_id,
        title=title,
        details_url=absolute,
        available_from=parsed["available_from"],
        due_at=parsed["due_at"],
        available_until=parsed["available_until"],
        timezone=timezone_name,
        status=row.get("status") or None,
        raw_date_label=" ".join(row["date_text"].split()) or None,
        canonical_key=coursework_canonical_key(title),
        component_kind=component_kind_from_text(title),
    )


def content_record_from_api_item(
    source: SourceConfig,
    item: dict[str, Any],
    ancestor_titles: tuple[str, ...] = (),
) -> DeadlineRecord:
    """Build a stable topic/module record while inheriting package identity."""
    item_type = "module" if int(item.get("Type", 1)) == 0 else "topic"
    item_id = str(item.get("Id", ""))
    if not item_id:
        raise ValueError("Brightspace content API item did not expose an identifier")
    title = " ".join(str(item.get("Title", "")).split())
    combined_title = " ".join((*ancestor_titles, title))
    details_url = (
        f"https://brightspace.vanderbilt.edu/d2l/le/lessons/"
        f"{source.source_course_id}/{'units' if item_type == 'module' else 'topics'}/{item_id}"
    )
    due_key = "ModuleDueDate" if item_type == "module" else "DueDate"
    start_key = "ModuleStartDate" if item_type == "module" else "StartDate"
    end_key = "ModuleEndDate" if item_type == "module" else "EndDate"
    due_at = parse_brightspace_api_datetime(item.get(due_key))
    available_from = parse_brightspace_api_datetime(item.get(start_key))
    available_until = parse_brightspace_api_datetime(item.get(end_key))
    raw_labels = " | ".join(
        f"{label}: {value}"
        for label, value in (
            ("Due", item.get(due_key)),
            ("Available from", item.get(start_key)),
            ("Available until", item.get(end_key)),
        )
        if value
    )
    return DeadlineRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_item_id=f"{item_type}:{item_id}",
        title=title,
        details_url=details_url,
        available_from=available_from,
        due_at=due_at,
        available_until=available_until,
        timezone="UTC" if any((due_at, available_from, available_until)) else None,
        status="hidden" if item.get("IsHidden") else None,
        raw_date_label=raw_labels or None,
        canonical_key=coursework_canonical_key(title) or coursework_canonical_key(combined_title),
        component_kind=component_kind_from_text(title),
    )


def module_description_records(
    source: SourceConfig,
    module: dict[str, Any],
    ancestor_titles: tuple[str, ...] = (),
) -> tuple[DeadlineRecord, ...]:
    """Preserve stable links embedded directly in a module description."""
    description = module.get("Description") or {}
    raw_html = str(description.get("Html") or description.get("Text") or "")
    if not raw_html:
        return ()
    module_title = " ".join(str(module.get("Title", "")).split())
    records: list[DeadlineRecord] = []
    for index, match in enumerate(
        re.finditer(
            r"<a\b[^>]*href=[\"'](?P<href>[^\"']+)[\"'][^>]*>(?P<label>.*?)</a>",
            raw_html,
            re.I | re.S,
        )
    ):
        label = html.unescape(re.sub(r"<[^>]+>", " ", match.group("label")))
        label = " ".join(label.split())
        if not label:
            continue
        absolute = clean_resource_url(urljoin(source.url, html.unescape(match.group("href"))))
        if absolute is None:
            continue
        if not absolute.startswith(("https://", "http://")):
            continue
        combined = " ".join((*ancestor_titles, module_title, label))
        canonical = coursework_canonical_key(label) or coursework_canonical_key(combined)
        if not canonical:
            continue
        records.append(
            DeadlineRecord(
                course_id=source.course_id,
                source_id=source.id,
                source_platform=source.platform,
                source_course_id=source.source_course_id,
                source_item_id=f"module:{module['Id']}:link:{hashlib.sha256(absolute.encode()).hexdigest()[:20]}",
                title=label,
                details_url=absolute,
                description=" ".join(
                    html.unescape(re.sub(r"<[^>]+>", " ", raw_html)).split()
                ) or None,
                canonical_key=canonical,
                component_kind=component_kind_from_text(label) or "prompt",
            )
        )
    return tuple(records)
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
            if not rows and not self._has_empty_state(page):
                raise ValueError("No assignment rows or verified empty state were found")
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"Brightspace assignment parsing failed: {type(exc).__name__}: {exc}",
                course_identity=identity,
            )
        if source.options.get("inspect_details"):
            enriched_records: list[DeadlineRecord] = []
            for record in records:
                try:
                    page.goto(record.details_url, wait_until="domcontentloaded", timeout=30_000)
                    page.locator("d2l-html-block[html]").first.wait_for(state="attached", timeout=10_000)
                    detail_body, links = _page_detail_payload(page)
                except Exception as exc:
                    if not _is_authenticated(page, source):
                        return CrawlResult(source, HealthStatus.LOGIN_REQUIRED,
                            message="Brightspace login required while checking assignment details; prior records retained.")
                    return CrawlResult(
                        source,
                        HealthStatus.UNAVAILABLE,
                        message=f"Could not load assignment detail for {record.title}: {type(exc).__name__}",
                        course_identity=identity,
                    )
                if not _is_authenticated(page, source):
                    return CrawlResult(
                        source,
                        HealthStatus.LOGIN_REQUIRED,
                        message="Brightspace login required while checking assignment details; prior records were retained.",
                        course_identity=identity,
                    )
                enriched = enrich_brightspace_record(record, detail_body, links)
                if enriched.due_at is None and not enriched.timing_text:
                    enriched = replace(enriched, timing_text="No explicit due time published for this assignment.")
                if source.options.get("split_relational_obligations"):
                    enriched_records.extend(split_lab_obligations(enriched, detail_body))
                else:
                    enriched_records.append(enriched)
            records = tuple(enriched_records)
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

        if source.options.get("inspect_details"):
            enriched_records: list[DeadlineRecord] = []
            for record in records:
                try:
                    page.goto(record.details_url, wait_until="domcontentloaded", timeout=30_000)
                    detail_body, links = _page_detail_payload(page)
                except Exception as exc:
                    return CrawlResult(
                        source,
                        HealthStatus.UNAVAILABLE,
                        message=f"Could not load content detail for {record.title}: {type(exc).__name__}",
                        course_identity=identity,
                    )
                if not _is_authenticated(page, source):
                    return CrawlResult(
                        source,
                        HealthStatus.LOGIN_REQUIRED,
                        message="Brightspace login required while checking content details; prior records were retained.",
                        course_identity=identity,
                    )
                enriched_records.append(enrich_brightspace_record(record, detail_body, links))
            records = tuple(enriched_records)

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


class BrightspaceAnnouncementsAdapter(_BrightspaceBase):
    empty_text = "There are no announcements"
    expected_heading = "Announcements"

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        loaded = self._load(page, source)
        if isinstance(loaded, CrawlResult):
            return loaded
        body_text, identity, timezone_name = loaded
        try:
            entries: list[dict[str, Any]] = page.locator(
                f'a[href*="/news/{source.source_course_id}/"][href*="/view"]'
            ).evaluate_all(
                r"""
                elements => elements.map(link => {
                  const href = link?.getAttribute('href') || '';
                  const match = href.match(/\/news\/\d+\/(\d+)\/view/i);
                  return {
                    item_id: match?.[1] || '',
                    title: (link.textContent || '').trim(),
                    href,
                  };
                }).filter(entry => entry.title && entry.href)
                """
            )
            entries = list({entry["href"]: entry for entry in entries}.values())
        except Exception as exc:
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message=f"Brightspace announcement-list parsing failed: {type(exc).__name__}: {exc}",
                course_identity=identity,
            )
        if not entries and not self._has_empty_state(page):
            return CrawlResult(
                source,
                HealthStatus.PARSER_FAILED,
                message="No announcement rows or verified empty state were found.",
                course_identity=identity,
            )

        records: list[DeadlineRecord] = []
        for entry in entries:
            details_url = urljoin(source.url, entry["href"])
            try:
                page.goto(details_url, wait_until="domcontentloaded", timeout=30_000)
                page.locator("d2l-html-block[html]").first.wait_for(state="attached", timeout=15_000)
                body, links = _page_detail_payload(page)
            except Exception as exc:
                if not _is_authenticated(page, source):
                    return CrawlResult(source, HealthStatus.LOGIN_REQUIRED,
                        message="Brightspace login required while checking announcements; prior records retained.")
                return CrawlResult(
                    source,
                    HealthStatus.UNAVAILABLE,
                    message=(
                        f"Could not load Brightspace announcement {entry['title']}: "
                        f"{type(exc).__name__}"
                    ),
                    course_identity=identity,
                )
            if not _is_authenticated(page, source):
                return CrawlResult(
                    source,
                    HealthStatus.LOGIN_REQUIRED,
                    message="Brightspace login required while checking announcements; prior records were retained.",
                    course_identity=identity,
                )
            record = announcement_record_from_entry(
                source,
                {**entry, "href": details_url, "body": body, "links": links},
                timezone_name,
            )
            if record is not None and _within_active_term(record, source):
                records.append(record)

        health = HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO
        return CrawlResult(
            source,
            health,
            tuple(records),
            (
                f"Inspected {len(entries)} announcements; retained {len(records)} "
                "actionable coursework announcements."
            ),
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
