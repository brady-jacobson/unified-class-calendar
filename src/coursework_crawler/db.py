from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Iterator

from .models import (
    CalendarEventRecord,
    CalendarIssue,
    ClassMeeting,
    CrawlResult,
    DeadlineRecord,
    HealthStatus,
    SourceConfig,
)


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sources (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    course_name TEXT NOT NULL,
    platform TEXT NOT NULL,
    adapter TEXT NOT NULL,
    source_course_id TEXT NOT NULL,
    url TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    configured_status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawl_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
    source_id TEXT NOT NULL REFERENCES sources(id),
    checked_at TEXT NOT NULL,
    health TEXT NOT NULL,
    item_count INTEGER NOT NULL,
    message TEXT NOT NULL,
    course_identity TEXT
);

CREATE INDEX IF NOT EXISTS source_checks_latest
ON source_checks(source_id, checked_at DESC);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_item_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    source_course_id TEXT NOT NULL,
    title TEXT NOT NULL,
    details_url TEXT NOT NULL,
    available_from TEXT,
    due_at TEXT,
    available_until TEXT,
    late_due_at TEXT,
    timezone TEXT,
    item_status TEXT,
    raw_date_label TEXT,
    timing_text TEXT,
    description TEXT,
    canonical_key TEXT,
    component_kind TEXT,
    related_links TEXT NOT NULL DEFAULT '[]',
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    consecutive_missing INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, source_item_id)
);

CREATE TABLE IF NOT EXISTS item_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    observed_at TEXT NOT NULL,
    title TEXT NOT NULL,
    details_url TEXT NOT NULL,
    available_from TEXT,
    due_at TEXT,
    available_until TEXT,
    late_due_at TEXT,
    timezone TEXT,
    item_status TEXT,
    raw_date_label TEXT,
    timing_text TEXT,
    description TEXT,
    canonical_key TEXT,
    component_kind TEXT,
    related_links TEXT NOT NULL DEFAULT '[]'
);

CREATE TABLE IF NOT EXISTS deadline_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
    item_id INTEGER NOT NULL REFERENCES items(id),
    changed_at TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);

CREATE TABLE IF NOT EXISTS calendar_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    source_event_id TEXT NOT NULL,
    course_id TEXT NOT NULL,
    source_platform TEXT NOT NULL,
    source_course_id TEXT NOT NULL,
    title TEXT NOT NULL,
    details_url TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    all_day INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    location TEXT,
    canonical_key TEXT,
    raw_date_label TEXT,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    consecutive_missing INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, source_event_id)
);

CREATE INDEX IF NOT EXISTS idx_calendar_events_active_starts
ON calendar_events(active, starts_at);

CREATE TABLE IF NOT EXISTS calendar_event_observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
    event_id INTEGER NOT NULL REFERENCES calendar_events(id),
    observed_at TEXT NOT NULL,
    title TEXT NOT NULL,
    details_url TEXT NOT NULL,
    starts_at TEXT NOT NULL,
    ends_at TEXT NOT NULL,
    all_day INTEGER NOT NULL,
    event_kind TEXT NOT NULL,
    location TEXT,
    canonical_key TEXT,
    raw_date_label TEXT
);

CREATE TABLE IF NOT EXISTS calendar_event_changes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL REFERENCES crawl_runs(id),
    event_id INTEGER NOT NULL REFERENCES calendar_events(id),
    changed_at TEXT NOT NULL,
    field_name TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT
);

CREATE INDEX IF NOT EXISTS calendar_event_changes_recent
ON calendar_event_changes(changed_at DESC);

CREATE TABLE IF NOT EXISTS source_issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL REFERENCES sources(id),
    issue_key TEXT NOT NULL,
    course_id TEXT NOT NULL,
    title TEXT NOT NULL,
    details_url TEXT NOT NULL,
    message TEXT NOT NULL,
    raw_value TEXT NOT NULL,
    severity TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    last_verified_at TEXT NOT NULL,
    consecutive_missing INTEGER NOT NULL DEFAULT 0,
    active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(source_id, issue_key)
);

CREATE TABLE IF NOT EXISTS class_meetings (
    id TEXT PRIMARY KEY,
    course_id TEXT NOT NULL,
    course_code TEXT NOT NULL,
    title TEXT NOT NULL,
    days TEXT NOT NULL,
    start_time TEXT NOT NULL,
    end_time TEXT NOT NULL,
    location TEXT NOT NULL,
    starts_on TEXT NOT NULL,
    ends_on TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connect() as connection:
            connection.executescript(SCHEMA)
            self._migrate(connection)
            connection.execute("PRAGMA optimize")

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        item_columns = {
            "available_until": "TEXT",
            "timing_text": "TEXT",
            "description": "TEXT",
            "canonical_key": "TEXT",
            "component_kind": "TEXT",
            "related_links": "TEXT NOT NULL DEFAULT '[]'",
        }
        for table in ("items", "item_observations"):
            columns = {
                row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for column, definition in item_columns.items():
                if column not in columns:
                    connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                    )
        calendar_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(calendar_events)")
        }
        for column in ("canonical_key", "raw_date_label"):
            if column not in calendar_columns:
                connection.execute(f"ALTER TABLE calendar_events ADD COLUMN {column} TEXT")

    def sync_sources(self, sources: tuple[SourceConfig, ...]) -> None:
        with self.connect() as connection:
            for source in sources:
                connection.execute(
                    """
                    INSERT INTO sources (
                        id, course_id, course_name, platform, adapter,
                        source_course_id, url, enabled, configured_status
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        course_id=excluded.course_id,
                        course_name=excluded.course_name,
                        platform=excluded.platform,
                        adapter=excluded.adapter,
                        source_course_id=excluded.source_course_id,
                        url=excluded.url,
                        enabled=excluded.enabled,
                        configured_status=excluded.configured_status
                    """,
                    (
                        source.id, source.course_id, source.course_name,
                        source.platform, source.adapter, source.source_course_id,
                        source.url, int(source.enabled), source.status,
                    ),
                )

    def sync_class_meetings(self, meetings: tuple[ClassMeeting, ...]) -> None:
        with self.connect() as connection:
            configured_ids = {meeting.id for meeting in meetings}
            for meeting in meetings:
                connection.execute(
                    """
                    INSERT INTO class_meetings(
                        id, course_id, course_code, title, days, start_time,
                        end_time, location, starts_on, ends_on
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        course_id=excluded.course_id,
                        course_code=excluded.course_code,
                        title=excluded.title,
                        days=excluded.days,
                        start_time=excluded.start_time,
                        end_time=excluded.end_time,
                        location=excluded.location,
                        starts_on=excluded.starts_on,
                        ends_on=excluded.ends_on
                    """,
                    (
                        meeting.id, meeting.course_id, meeting.course_code,
                        meeting.title, ",".join(str(day) for day in meeting.days),
                        meeting.start_time, meeting.end_time, meeting.location,
                        meeting.starts_on, meeting.ends_on,
                    ),
                )
            if configured_ids:
                placeholders = ",".join("?" for _ in configured_ids)
                connection.execute(
                    f"DELETE FROM class_meetings WHERE id NOT IN ({placeholders})",
                    tuple(sorted(configured_ids)),
                )
            else:
                connection.execute("DELETE FROM class_meetings")

    def start_run(self) -> int:
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO crawl_runs(started_at, status) VALUES (?, ?)",
                (_now(), "running"),
            )
            return int(cursor.lastrowid)

    def finish_run(self, run_id: int, status: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE crawl_runs SET finished_at=?, status=? WHERE id=?",
                (_now(), status, run_id),
            )

    def record_result(
        self,
        run_id: int,
        result: CrawlResult,
        missing_runs_before_inactive: int,
    ) -> None:
        observed_at = _now()
        source_missing_threshold = int(
            result.source.options.get("missing_runs_before_inactive", missing_runs_before_inactive)
        )
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO source_checks(
                    run_id, source_id, checked_at, health, item_count,
                    message, course_identity
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, result.source.id, observed_at, result.health.value,
                    len(result.records) + len(result.calendar_events),
                    result.message, result.course_identity,
                ),
            )

            seen_issue_keys: set[str] = set()
            for issue in result.issues:
                seen_issue_keys.add(issue.issue_key)
                self._upsert_issue(connection, issue, observed_at)

            if result.health not in {HealthStatus.SUCCESS, HealthStatus.VERIFIED_ZERO, HealthStatus.PARTIAL}:
                return

            seen_ids: set[str] = set()
            for record in result.records:
                seen_ids.add(record.source_item_id)
                self._upsert_item(connection, run_id, record, observed_at)

            seen_event_ids: set[str] = set()
            for event in result.calendar_events:
                seen_event_ids.add(event.source_event_id)
                self._upsert_calendar_event(connection, run_id, event, observed_at)

            # A partial enumeration may add verified observations, but cannot
            # establish that an unseen item or an earlier issue disappeared.
            if result.health == HealthStatus.PARTIAL:
                return

            rows = connection.execute(
                "SELECT id, source_item_id FROM items WHERE source_id=? AND active=1",
                (result.source.id,),
            ).fetchall()
            for row in rows:
                if row["source_item_id"] in seen_ids:
                    continue
                connection.execute(
                    """
                    UPDATE items
                    SET consecutive_missing=consecutive_missing+1,
                        active=CASE WHEN consecutive_missing+1 >= ? THEN 0 ELSE active END
                    WHERE id=?
                    """,
                    (source_missing_threshold, row["id"]),
                )

            event_rows = connection.execute(
                """
                SELECT id, source_event_id, consecutive_missing
                FROM calendar_events WHERE source_id=? AND active=1
                """,
                (result.source.id,),
            ).fetchall()
            for row in event_rows:
                if row["source_event_id"] in seen_event_ids:
                    continue
                missing_count = int(row["consecutive_missing"] or 0) + 1
                field_name = (
                    "removed" if missing_count >= source_missing_threshold
                    else "missing" if missing_count == 1
                    else None
                )
                if field_name:
                    connection.execute(
                        """
                        INSERT INTO calendar_event_changes(
                            run_id, event_id, changed_at, field_name, old_value, new_value
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, row["id"], observed_at, field_name, "active", str(missing_count)),
                    )
                connection.execute(
                    """
                    UPDATE calendar_events
                    SET consecutive_missing=consecutive_missing+1,
                        active=CASE WHEN consecutive_missing+1 >= ? THEN 0 ELSE active END
                    WHERE id=?
                    """,
                    (source_missing_threshold, row["id"]),
                )

            issue_rows = connection.execute(
                "SELECT id, issue_key FROM source_issues WHERE source_id=? AND active=1",
                (result.source.id,),
            ).fetchall()
            for row in issue_rows:
                if row["issue_key"] in seen_issue_keys:
                    continue
                connection.execute(
                    """
                    UPDATE source_issues
                    SET consecutive_missing=consecutive_missing+1,
                        active=CASE WHEN consecutive_missing+1 >= ? THEN 0 ELSE active END
                    WHERE id=?
                    """,
                    (source_missing_threshold, row["id"]),
                )

    @staticmethod
    def _upsert_issue(
        connection: sqlite3.Connection,
        issue: CalendarIssue,
        observed_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO source_issues(
                source_id, issue_key, course_id, title, details_url, message,
                raw_value, severity, first_seen_at, last_seen_at, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, issue_key) DO UPDATE SET
                course_id=excluded.course_id,
                title=excluded.title,
                details_url=excluded.details_url,
                message=excluded.message,
                raw_value=excluded.raw_value,
                severity=excluded.severity,
                last_seen_at=excluded.last_seen_at,
                last_verified_at=excluded.last_verified_at,
                consecutive_missing=0,
                active=1
            """,
            (
                issue.source_id, issue.issue_key, issue.course_id, issue.title,
                issue.details_url, issue.message, issue.raw_value, issue.severity,
                observed_at, observed_at, observed_at,
            ),
        )

    def _upsert_calendar_event(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        event: CalendarEventRecord,
        observed_at: str,
    ) -> None:
        current = connection.execute(
            "SELECT * FROM calendar_events WHERE source_id=? AND source_event_id=?",
            (event.source_id, event.source_event_id),
        ).fetchone()
        values = {
            "title": event.title,
            "details_url": event.details_url,
            "starts_at": _iso(event.starts_at),
            "ends_at": _iso(event.ends_at),
            "all_day": int(event.all_day),
            "event_kind": event.event_kind,
            "location": event.location,
            "canonical_key": event.canonical_key,
            "raw_date_label": event.raw_date_label,
        }
        cursor = connection.execute(
            """
            INSERT INTO calendar_events(
                source_id, source_event_id, course_id, source_platform,
                source_course_id, title, details_url, starts_at, ends_at,
                all_day, event_kind, location, canonical_key, raw_date_label,
                first_seen_at, last_seen_at, last_verified_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_id, source_event_id) DO UPDATE SET
                course_id=excluded.course_id,
                source_platform=excluded.source_platform,
                source_course_id=excluded.source_course_id,
                title=excluded.title,
                details_url=excluded.details_url,
                starts_at=excluded.starts_at,
                ends_at=excluded.ends_at,
                all_day=excluded.all_day,
                event_kind=excluded.event_kind,
                location=excluded.location,
                canonical_key=excluded.canonical_key,
                raw_date_label=excluded.raw_date_label,
                last_seen_at=excluded.last_seen_at,
                last_verified_at=excluded.last_verified_at,
                consecutive_missing=0,
                active=1
            """,
            (
                event.source_id, event.source_event_id, event.course_id,
                event.source_platform, event.source_course_id, event.title,
                event.details_url, values["starts_at"], values["ends_at"],
                values["all_day"], event.event_kind, event.location,
                event.canonical_key, event.raw_date_label, observed_at,
                observed_at, observed_at,
            ),
        )
        event_id = int(cursor.lastrowid) if current is None else int(current["id"])
        if current is None:
            connection.execute(
                """
                INSERT INTO calendar_event_changes(
                    run_id, event_id, changed_at, field_name, old_value, new_value
                ) VALUES (?, ?, ?, 'added', NULL, ?)
                """,
                (run_id, event_id, observed_at, values["starts_at"]),
            )
        else:
            for field, new_value in values.items():
                old_value = current[field]
                if old_value != new_value:
                    connection.execute(
                        """
                        INSERT INTO calendar_event_changes(
                            run_id, event_id, changed_at, field_name, old_value, new_value
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, event_id, observed_at, field, old_value, new_value),
                    )
            if not current["active"]:
                connection.execute(
                    """
                    INSERT INTO calendar_event_changes(
                        run_id, event_id, changed_at, field_name, old_value, new_value
                    ) VALUES (?, ?, ?, 'restored', 'inactive', 'active')
                    """,
                    (run_id, event_id, observed_at),
                )
        connection.execute(
            """
            INSERT INTO calendar_event_observations(
                run_id, event_id, observed_at, title, details_url, starts_at,
                ends_at, all_day, event_kind, location, canonical_key, raw_date_label
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, event_id, observed_at, event.title, event.details_url,
                values["starts_at"], values["ends_at"], values["all_day"],
                event.event_kind, event.location, event.canonical_key,
                event.raw_date_label,
            ),
        )

    def _upsert_item(
        self,
        connection: sqlite3.Connection,
        run_id: int,
        record: DeadlineRecord,
        observed_at: str,
    ) -> None:
        current = connection.execute(
            "SELECT * FROM items WHERE source_id=? AND source_item_id=?",
            (record.source_id, record.source_item_id),
        ).fetchone()
        values = {
            "title": record.title,
            "details_url": record.details_url,
            "available_from": _iso(record.available_from),
            "due_at": _iso(record.due_at),
            "available_until": _iso(record.available_until),
            "late_due_at": _iso(record.late_due_at),
            "timezone": record.timezone,
            "item_status": record.status,
            "raw_date_label": record.raw_date_label,
            "timing_text": record.timing_text,
            "description": record.description,
            "canonical_key": record.canonical_key,
            "component_kind": record.component_kind,
            "related_links": json.dumps(record.related_links, ensure_ascii=False),
        }

        if current is None:
            cursor = connection.execute(
                """
                INSERT INTO items(
                    source_id, source_item_id, course_id, source_platform,
                    source_course_id, title, details_url, available_from,
                    due_at, available_until, late_due_at, timezone, item_status,
                    raw_date_label, timing_text, description, canonical_key,
                    component_kind, related_links, first_seen_at, last_seen_at,
                    last_verified_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.source_id, record.source_item_id, record.course_id,
                    record.source_platform, record.source_course_id,
                    values["title"], values["details_url"], values["available_from"],
                    values["due_at"], values["available_until"], values["late_due_at"],
                    values["timezone"], values["item_status"], values["raw_date_label"],
                    values["timing_text"], values["description"], values["canonical_key"],
                    values["component_kind"], values["related_links"],
                    observed_at, observed_at, observed_at,
                ),
            )
            item_id = int(cursor.lastrowid)
        else:
            item_id = int(current["id"])
            for field in (
                "available_from", "due_at", "available_until", "late_due_at",
                "timing_text",
            ):
                if current[field] != values[field]:
                    connection.execute(
                        """
                        INSERT INTO deadline_changes(
                            run_id, item_id, changed_at, field_name, old_value, new_value
                        ) VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, item_id, observed_at, field, current[field], values[field]),
                    )
            connection.execute(
                """
                UPDATE items SET
                    title=?, details_url=?, available_from=?, due_at=?, available_until=?,
                    late_due_at=?, timezone=?, item_status=?, raw_date_label=?, timing_text=?,
                    description=?, canonical_key=?, component_kind=?, related_links=?,
                    last_seen_at=?, last_verified_at=?, consecutive_missing=0, active=1
                WHERE id=?
                """,
                (
                    values["title"], values["details_url"], values["available_from"],
                    values["due_at"], values["available_until"], values["late_due_at"],
                    values["timezone"], values["item_status"], values["raw_date_label"],
                    values["timing_text"], values["description"], values["canonical_key"],
                    values["component_kind"], values["related_links"],
                    observed_at, observed_at, item_id,
                ),
            )

        connection.execute(
            """
            INSERT INTO item_observations(
                run_id, item_id, observed_at, title, details_url, available_from,
                due_at, available_until, late_due_at, timezone, item_status, raw_date_label,
                timing_text, description, canonical_key, component_kind, related_links
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id, item_id, observed_at, values["title"], values["details_url"],
                values["available_from"], values["due_at"], values["available_until"],
                values["late_due_at"], values["timezone"], values["item_status"],
                values["raw_date_label"], values["timing_text"], values["description"],
                values["canonical_key"], values["component_kind"], values["related_links"],
            ),
        )
