from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class HealthStatus(StrEnum):
    SUCCESS = "success"
    VERIFIED_ZERO = "verified_zero"
    LOGIN_REQUIRED = "login_required"
    COURSE_NOT_FOUND = "course_not_found"
    PARSER_FAILED = "parser_failed"
    UNAVAILABLE = "unavailable"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SourceConfig:
    id: str
    course_id: str
    course_name: str
    adapter: str
    platform: str
    source_course_id: str
    url: str
    enabled: bool = True
    status: str = "active"
    priority: str = "normal"
    note: str = ""
    options: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassMeeting:
    id: str
    course_id: str
    course_code: str
    title: str
    days: tuple[int, ...]
    start_time: str
    end_time: str
    location: str
    starts_on: str
    ends_on: str


@dataclass(frozen=True)
class DeadlineRecord:
    course_id: str
    source_id: str
    source_platform: str
    source_course_id: str
    source_item_id: str
    title: str
    details_url: str
    available_from: datetime | None = None
    due_at: datetime | None = None
    available_until: datetime | None = None
    late_due_at: datetime | None = None
    timezone: str | None = None
    status: str | None = None
    raw_date_label: str | None = None
    timing_text: str | None = None
    description: str | None = None
    canonical_key: str | None = None
    component_kind: str | None = None
    related_links: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class CalendarEventRecord:
    course_id: str
    source_id: str
    source_platform: str
    source_course_id: str
    source_event_id: str
    title: str
    details_url: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    event_kind: str
    location: str | None = None
    canonical_key: str | None = None
    raw_date_label: str | None = None


@dataclass(frozen=True)
class CalendarIssue:
    course_id: str
    source_id: str
    issue_key: str
    title: str
    details_url: str
    message: str
    raw_value: str
    severity: str = "warning"


@dataclass(frozen=True)
class CrawlResult:
    source: SourceConfig
    health: HealthStatus
    records: tuple[DeadlineRecord, ...] = ()
    message: str = ""
    course_identity: str | None = None
    calendar_events: tuple[CalendarEventRecord, ...] = ()
    issues: tuple[CalendarIssue, ...] = ()
