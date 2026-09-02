from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path

from .models import ClassMeeting, SourceConfig


@dataclass(frozen=True)
class AppConfig:
    timezone: str
    term: str
    missing_runs_before_inactive: int
    sources: tuple[SourceConfig, ...]
    classes: tuple[ClassMeeting, ...]


def load_config(path: Path) -> AppConfig:
    with path.open("rb") as handle:
        raw = tomllib.load(handle)

    sources: list[SourceConfig] = []
    for course in raw.get("courses", []):
        course_id = str(course["id"])
        course_name = str(course["name"])
        for source in course.get("sources", []):
            known = {
                "id", "adapter", "platform", "source_course_id", "url",
                "enabled", "status", "priority", "note",
            }
            sources.append(
                SourceConfig(
                    id=str(source["id"]),
                    course_id=course_id,
                    course_name=course_name,
                    adapter=str(source["adapter"]),
                    platform=str(source["platform"]),
                    source_course_id=str(source["source_course_id"]),
                    url=str(source["url"]),
                    enabled=bool(source.get("enabled", True)),
                    status=str(source.get("status", "active")),
                    priority=str(source.get("priority", "normal")),
                    note=str(source.get("note", "")),
                    options={key: value for key, value in source.items() if key not in known},
                )
            )

    ids = [source.id for source in sources]
    if len(ids) != len(set(ids)):
        raise ValueError("Every source id must be unique")

    day_numbers = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    classes = tuple(
        ClassMeeting(
            id=str(meeting["id"]),
            course_id=str(meeting["course_id"]),
            course_code=str(meeting["course_code"]),
            title=str(meeting["title"]),
            days=tuple(day_numbers[str(day).upper()] for day in meeting["days"]),
            start_time=str(meeting["start_time"]),
            end_time=str(meeting["end_time"]),
            location=str(meeting.get("location", "")),
            starts_on=str(meeting["starts_on"]),
            ends_on=str(meeting["ends_on"]),
        )
        for meeting in raw.get("classes", [])
    )
    class_ids = [meeting.id for meeting in classes]
    if len(class_ids) != len(set(class_ids)):
        raise ValueError("Every class meeting id must be unique")

    return AppConfig(
        timezone=str(raw.get("timezone", "America/Chicago")),
        term=str(raw.get("term", "")),
        missing_runs_before_inactive=int(raw.get("missing_runs_before_inactive", 3)),
        sources=tuple(sources),
        classes=classes,
    )
