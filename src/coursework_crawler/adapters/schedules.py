from __future__ import annotations

import io
import json
import re
from datetime import date, datetime, time, timedelta
from html import unescape
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from ..models import (
    CalendarEventRecord,
    CalendarIssue,
    ClassMeeting,
    CrawlResult,
    HealthStatus,
    SourceConfig,
)
from ..semantics import canonical_event_key, normalized_event_title
from .base import Adapter


BRIGHTSPACE = "https://brightspace.vanderbilt.edu"
DATE_PREFIX = re.compile(
    r"^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s+"
    r"([A-Za-z]+\s+\d{1,2},\s+\d{4})\s+(.*)$"
)
MONTH_DAY = re.compile(
    r"\b(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
    r"(\d{1,2})(?:st|nd|rd|th)?\b",
    re.I,
)


def _plain_text(fragment: str) -> str:
    value = re.sub(r"<script\b[^>]*>.*?</script>", " ", fragment, flags=re.I | re.S)
    value = re.sub(r"<style\b[^>]*>.*?</style>", " ", value, flags=re.I | re.S)
    value = re.sub(r"<[^>]+>", " ", value)
    return " ".join(unescape(value).replace("\xa0", " ").split())


def _issue(
    source: SourceConfig,
    key: str,
    title: str,
    message: str,
    raw_value: str,
) -> CalendarIssue:
    return CalendarIssue(
        course_id=source.course_id,
        source_id=source.id,
        issue_key=key,
        title=title,
        details_url=source.url,
        message=message,
        raw_value=raw_value,
    )


def _event(
    source: SourceConfig,
    meeting: ClassMeeting | None,
    event_id: str,
    title: str,
    event_date: date,
    kind: str,
    raw_date_label: str,
    *,
    all_day: bool = False,
    end_date: date | None = None,
    start_clock: time | None = None,
    end_clock: time | None = None,
    canonical_key: str | None = None,
) -> CalendarEventRecord:
    timezone = ZoneInfo(str(source.options.get("timezone", "America/Chicago")))
    if all_day:
        starts_at = datetime.combine(event_date, time.min, timezone)
        ends_at = datetime.combine(end_date or event_date + timedelta(days=1), time.min, timezone)
    else:
        if start_clock is None:
            if meeting is None:
                raise ValueError(f"No class meeting time configured for {source.id}")
            start_clock = time.fromisoformat(meeting.start_time)
            end_clock = time.fromisoformat(meeting.end_time)
        starts_at = datetime.combine(event_date, start_clock, timezone)
        ends_at = datetime.combine(event_date, end_clock or start_clock, timezone)
    return CalendarEventRecord(
        course_id=source.course_id,
        source_id=source.id,
        source_platform=source.platform,
        source_course_id=source.source_course_id,
        source_event_id=event_id,
        title=title,
        details_url=source.url,
        starts_at=starts_at,
        ends_at=ends_at,
        all_day=all_day,
        event_kind=kind,
        canonical_key=canonical_key or canonical_event_key(title, kind),
        raw_date_label=raw_date_label,
    )


def _row_kind(title: str) -> str:
    if re.search(r"\b(no class|cancel(?:led|ed|lation)?)\b", title, re.I):
        return "cancellation"
    if re.search(r"\b(exam|test)\b", title, re.I):
        return "exam"
    if re.search(r"\bquiz\b", title, re.I):
        return "quiz"
    if re.search(r"\breview\b", title, re.I):
        return "review"
    return "lecture"


def parse_math2420_pdf(
    payload: bytes,
    source: SourceConfig,
    meeting: ClassMeeting,
) -> tuple[tuple[CalendarEventRecord, ...], tuple[CalendarIssue, ...]]:
    text = "\n".join(page.extract_text() or "" for page in PdfReader(io.BytesIO(payload)).pages)
    return parse_math2420_text(text, source, meeting)


def parse_math2420_text(
    text: str,
    source: SourceConfig,
    meeting: ClassMeeting,
) -> tuple[tuple[CalendarEventRecord, ...], tuple[CalendarIssue, ...]]:
    events: list[CalendarEventRecord] = []
    issues: list[CalendarIssue] = []
    occurrences: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = " ".join(raw_line.replace("\xa0", " ").split())
        match = DATE_PREFIX.match(line)
        if not match:
            continue
        weekday, date_text, remainder = match.groups()
        parsed_date = datetime.strptime(date_text, "%B %d, %Y").date()
        if parsed_date.strftime("%A") != weekday:
            issues.append(_issue(
                source, f"weekday:{parsed_date.isoformat()}", "Schedule weekday conflict",
                "The written weekday does not match the explicit calendar date; the row was not imported.", line,
            ))
            continue
        exercise = re.search(r"\s\d+\.\d+:\s", remainder)
        title = (remainder[:exercise.start()] if exercise else remainder).strip()
        if not title:
            continue
        kind = _row_kind(title)
        identity = normalized_event_title(title)
        occurrences[identity] = occurrences.get(identity, 0) + 1
        events.append(_event(
            source, meeting, f"row:{identity}:{occurrences[identity]}", title,
            parsed_date, kind, f"{weekday}, {date_text}",
        ))

    thanksgiving = re.search(r"Thanksgiving Break \(November 21 - 29, (\d{4})\)", text)
    if thanksgiving and thanksgiving.group(1) != str(source.options.get("term_year", 2026)):
        issues.append(_issue(
            source, "thanksgiving-year", "Thanksgiving Break year conflict",
            "The schedule labels Thanksgiving Break with a different year from the Fall 2026 course; no break dates were inferred.",
            thanksgiving.group(0),
        ))
    return tuple(events), tuple(issues)


def parse_math3320_schedule(
    text: str,
    source: SourceConfig,
    meeting: ClassMeeting,
) -> tuple[tuple[CalendarEventRecord, ...], tuple[CalendarIssue, ...]]:
    year = int(source.options.get("term_year", 2026))
    issues: list[CalendarIssue] = []
    rows: list[tuple[date, str, str]] = []
    for raw_line in text.splitlines():
        line = " ".join(raw_line.split())
        match = re.match(r"^([A-Za-z]+\s+\d{1,2}):\s*(.+)$", line)
        if not match:
            continue
        raw_date, title = match.groups()
        try:
            parsed_date = datetime.strptime(f"{raw_date} {year}", "%B %d %Y").date()
        except ValueError:
            issues.append(_issue(
                source, f"invalid-date:{normalized_event_title(raw_date)}", "Invalid schedule date",
                "The course schedule contains a date that does not exist; no schedule rows were imported.", line,
            ))
            continue
        if parsed_date.weekday() not in meeting.days:
            issues.append(_issue(
                source, f"off-pattern:{parsed_date.isoformat()}", "Schedule date conflicts with MWF meetings",
                "The dated row falls outside the authoritative MWF class pattern; no schedule rows were imported.", line,
            ))
        rows.append((parsed_date, title, raw_date))
    if issues:
        return (), tuple(issues)
    events = tuple(
        _event(source, meeting, f"topic:{normalized_event_title(title)}", title, when,
               _row_kind(title), raw_date)
        for when, title, raw_date in rows
    )
    return events, ()


def parse_cs2281_lectures(
    html: str,
    source: SourceConfig,
    meeting: ClassMeeting,
) -> tuple[CalendarEventRecord, ...]:
    pattern = re.compile(
        r">([A-Z]+\s+\d{1,2})</span>.*?>\s*LECTURE\s+(\d+)\s*</span>.*?"
        r"<p><span[^>]*font-size:\s*1\.15em[^>]*>(.*?)</span></p>",
        re.I | re.S,
    )
    year = int(source.options.get("term_year", 2026))
    events: list[CalendarEventRecord] = []
    for raw_date, lecture_number, raw_title in pattern.findall(html):
        parsed_date = datetime.strptime(f"{raw_date.title()} {year}", "%B %d %Y").date()
        title = f"Lecture {lecture_number}: {_plain_text(raw_title)}"
        events.append(_event(
            source, meeting, f"lecture:{lecture_number}", title, parsed_date,
            "lecture", raw_date.title(), canonical_key=f"lecture:{lecture_number}",
        ))
    return tuple(events)


def parse_cs3250_calendar(
    html: str,
    source: SourceConfig,
    meeting: ClassMeeting,
) -> tuple[CalendarEventRecord, ...]:
    text = _plain_text(html)
    year = int(source.options.get("term_year", 2026))
    events: dict[str, CalendarEventRecord] = {}

    for heading in re.findall(r"<h2\b[^>]*>(.*?)</h2>", html, flags=re.I | re.S):
        label = _plain_text(heading)
        match = re.search(r"WEEK OF\s+([A-Za-z]+\s+\d{1,2}(?:st|nd|rd|th)?)\s*(.*)", label, re.I)
        if not match:
            continue
        raw_date, topic = match.groups()
        date_match = MONTH_DAY.search(raw_date)
        if not date_match:
            continue
        week_date = datetime.strptime(
            f"{date_match.group(1)} {date_match.group(2)} {year}", "%B %d %Y"
        ).date()
        title = topic.strip(" -") or "Course topics"
        key = f"week:{week_date.isoformat()}"
        events[key] = _event(
            source, meeting, key, title, week_date, "week_topic", raw_date,
            all_day=True, canonical_key=key,
        )

    exact_patterns = (
        (r"(Exam\s+#?(\d+))\s+-\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday),\s+([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?\s+\(in class\)", "exam"),
        (r"(Technical Interview Practice \(TIPs\)\s+#?(\d+))\s+-\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday),\s+([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?", "course_event"),
        (r"(HW\s+#?(\d+)\s+Due)\s+-\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday),\s+([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?\s+by\s+9AM", "due"),
    )
    for pattern, kind in exact_patterns:
        for match in re.finditer(pattern, text, re.I):
            title, number, raw_date = match.groups()
            parsed_date = datetime.strptime(f"{raw_date} {year}", "%B %d %Y").date()
            canonical = canonical_event_key(title, kind)
            if kind == "due":
                event = _event(
                    source, meeting, canonical, title, parsed_date, kind, raw_date,
                    start_clock=time(9), end_clock=time(9, 1), canonical_key=canonical,
                )
            else:
                event = _event(
                    source, meeting, canonical, title, parsed_date, kind, raw_date,
                    canonical_key=canonical,
                )
            events[canonical] = event

    fall_break = re.search(
        r"FALL BREAK\s+-\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday),\s+([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?\s+-\s+No Class",
        text, re.I,
    )
    if fall_break:
        raw_date = fall_break.group(1)
        parsed_date = datetime.strptime(f"{raw_date} {year}", "%B %d %Y").date()
        events["break:fall"] = _event(
            source, meeting, "break:fall", "Fall Break - No Class", parsed_date,
            "cancellation", raw_date, canonical_key="break:fall",
        )

    withdraw = re.search(
        r"Last Day to Withdraw\s+-\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday),\s+"
        r"([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?\s+@\s+11:59pm",
        text, re.I,
    )
    if withdraw:
        raw_date = withdraw.group(1)
        parsed_date = datetime.strptime(f"{raw_date} {year}", "%B %d %Y").date()
        events["term:withdraw"] = _event(
            source, meeting, "term:withdraw", "Last Day to Withdraw", parsed_date,
            "course_event", raw_date, start_clock=time(23, 59), end_clock=time(23, 59),
            canonical_key="term:withdraw",
        )

    last_class = re.search(
        r"LAST DAY OF CLASSES\s+(?:Monday|Tuesday|Wednesday|Thursday|Friday),?\s+"
        r"([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?",
        text, re.I,
    )
    if last_class:
        raw_date = last_class.group(1)
        parsed_date = datetime.strptime(f"{raw_date} {year}", "%B %d %Y").date()
        events["course:last-class"] = _event(
            source, meeting, "course:last-class", "Last CS 3250 Class", parsed_date,
            "course_event", raw_date, canonical_key="course:last-class",
        )

    final = re.search(
        r"Section 03\s*\(MWF 1:25pm section\)\s*-\s*"
        r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*"
        r"([A-Za-z]+\s+\d{1,2})(?:st|nd|rd|th)?,\s*(2:00 PM)\s*-\s*(5:00 PM)",
        text, re.I,
    )
    if final:
        raw_date, start_value, end_value = final.groups()
        parsed_date = datetime.strptime(f"{raw_date} {year}", "%B %d %Y").date()
        events["exam:final:section-3"] = _event(
            source, meeting, "exam:final:section-3", "CS 3250 Final Exam - Section 03",
            parsed_date, "exam", f"{raw_date}, {start_value} - {end_value}",
            start_clock=datetime.strptime(start_value, "%I:%M %p").time(),
            end_clock=datetime.strptime(end_value, "%I:%M %p").time(),
            canonical_key="exam:final:section-3",
        )
    return tuple(events.values())


class BrightspaceScheduleAdapter(Adapter):
    def __init__(self, meetings: tuple[ClassMeeting, ...] = ()):
        self.meetings = {meeting.course_id: meeting for meeting in meetings}

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        meeting = self.meetings.get(source.course_id)
        if meeting is None:
            return CrawlResult(
                source, HealthStatus.PARSER_FAILED,
                message="No authoritative class meeting is configured for this schedule.",
            )
        schedule_format = str(source.options.get("schedule_format", ""))
        try:
            if schedule_format == "math2420_pdf":
                topic = self._json(page, source, f"content/topics/{source.options['topic_id']}")
                response = page.context.request.get(urljoin(BRIGHTSPACE, topic["Url"]))
                self._raise_for_login(response)
                if not response.ok:
                    raise RuntimeError(f"schedule PDF returned {response.status}")
                events, issues = parse_math2420_pdf(response.body(), source, meeting)
            elif schedule_format == "math3320_text":
                modules = self._json(page, source, "content/root/")
                module = next(item for item in modules if str(item["Id"]) == str(source.options["module_id"]))
                events, issues = parse_math3320_schedule(module["Description"]["Text"], source, meeting)
            elif schedule_format == "cs3250_html":
                topic = self._json(page, source, f"content/topics/{source.options['topic_id']}")
                response = page.context.request.get(urljoin(BRIGHTSPACE, topic["Url"]))
                self._raise_for_login(response)
                if not response.ok:
                    raise RuntimeError(f"course calendar returned {response.status}")
                events = parse_cs3250_calendar(response.text(), source, meeting)
                issues = ()
            elif schedule_format == "cs2281_lectures":
                module = self._json(page, source, f"content/modules/{source.options['module_id']}")
                events = parse_cs2281_lectures(module["Description"]["Html"], source, meeting)
                issues = ()
            elif schedule_format == "cs2281_schedule":
                module = self._json(page, source, f"content/modules/{source.options['module_id']}")
                raw = module["Description"]["Html"]
                if "spring2026" in raw.lower():
                    issue = _issue(
                        source, "wrong-term-resource", "CS 2281 Schedule is not a Fall 2026 schedule",
                        "The live Schedule unit still references a Spring 2026 image, so no dates were imported from it.",
                        _plain_text(raw) or raw,
                    )
                    return CrawlResult(
                        source, HealthStatus.AMBIGUOUS, message=issue.message,
                        course_identity=module.get("Title"), issues=(issue,),
                    )
                raise ValueError("Schedule unit did not expose a recognized dated Fall 2026 resource")
            else:
                raise ValueError(f"Unknown schedule format {schedule_format!r}")
        except StopIteration:
            return CrawlResult(
                source, HealthStatus.COURSE_NOT_FOUND,
                message="The configured live schedule resource was not found; prior events were retained.",
            )
        except PermissionError:
            return CrawlResult(
                source, HealthStatus.LOGIN_REQUIRED,
                message="Brightspace schedule login required; prior events were retained.",
            )
        except Exception as exc:
            return CrawlResult(
                source, HealthStatus.PARSER_FAILED,
                message=f"Schedule retrieval/parsing failed: {type(exc).__name__}: {exc}",
            )

        health = HealthStatus.AMBIGUOUS if issues and not events else (
            HealthStatus.SUCCESS if events else HealthStatus.VERIFIED_ZERO
        )
        return CrawlResult(
            source, health, calendar_events=events, issues=issues,
            message=f"Verified {len(events)} explicit schedule events; {len(issues)} ambiguities require attention.",
            course_identity=source.course_name,
        )

    @staticmethod
    def _json(page: Any, source: SourceConfig, endpoint: str) -> Any:
        url = f"{BRIGHTSPACE}/d2l/api/le/1.75/{source.source_course_id}/{endpoint}"
        response = page.context.request.get(url)
        if response.status in {401, 403}:
            raise PermissionError(url)
        if not response.ok:
            raise RuntimeError(f"Brightspace API returned {response.status}")
        body = response.text()
        if "json" not in response.headers.get("content-type", "").lower():
            if "sign in" in body.lower() or "single sign-on" in body.lower():
                raise PermissionError(url)
        return json.loads(body)

    @staticmethod
    def _raise_for_login(response: Any) -> None:
        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type:
            return
        body = response.body()[:20_000].decode("utf-8", errors="ignore").lower()
        if "sign in" in body or "single sign-on" in body:
            raise PermissionError("Brightspace session expired")
