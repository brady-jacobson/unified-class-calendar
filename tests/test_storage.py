from __future__ import annotations

import sqlite3
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from coursework_crawler.config import load_config
from coursework_crawler.db import Database
from coursework_crawler.models import (
    CalendarEventRecord,
    CalendarIssue,
    CrawlResult,
    DeadlineRecord,
    HealthStatus,
)


ROOT = Path(__file__).resolve().parents[1]


class StorageTests(unittest.TestCase):
    def test_partial_scan_adds_observations_without_removing_unseen_records(self) -> None:
        config = load_config(ROOT / "config/sources.example.toml")
        source = next(s for s in config.sources if s.id == "math2420-webwork")
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.sqlite3")
            database.initialize()
            database.sync_sources(config.sources)
            def record(item_id: str) -> DeadlineRecord:
                return DeadlineRecord(source.course_id, source.id, source.platform,
                    source.source_course_id, item_id, item_id, "https://example.invalid/item")
            run = database.start_run()
            database.record_result(run, CrawlResult(source, HealthStatus.SUCCESS, (record("old"),)), 1)
            database.record_result(run, CrawlResult(source, HealthStatus.PARTIAL, (record("new"),)), 1)
            database.record_result(run, CrawlResult(source, HealthStatus.SUCCESS, (record("new"),),
                                                    complete_enumeration=False), 1)
            with database.connect() as connection:
                rows = connection.execute("SELECT source_item_id, active, consecutive_missing FROM items ORDER BY source_item_id").fetchall()
                self.assertEqual([("new", 1, 0), ("old", 1, 0)], [tuple(row) for row in rows])

    def test_source_configuration_loads_all_known_and_pending_sources(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        self.assertIn("math2420-webwork", {source.id for source in config.sources})
        self.assertIn("cs2281-gradescope", {source.id for source in config.sources})
        self.assertIn("cs2281l-brightspace-assignments", {source.id for source in config.sources})
        self.assertEqual("example-term", config.term)
        self.assertEqual(6, len(config.classes))
        cs2281 = next(meeting for meeting in config.classes if meeting.course_id == "cs2281")
        self.assertEqual("", cs2281.location)

    def test_calendar_events_are_stored_separately_from_deadlines(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        source = next(source for source in config.sources if source.id == "brightspace-calendar-feed")
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "test.sqlite3"
            database = Database(database_path)
            database.initialize()
            database.sync_sources(config.sources)
            event = CalendarEventRecord(
                course_id="cs3250",
                source_id=source.id,
                source_platform=source.platform,
                source_course_id="100003",
                source_event_id="event-1",
                title="Module 3",
                details_url="https://example.test/event-1",
                starts_at=datetime(2026, 9, 14, 10, tzinfo=ZoneInfo("America/Chicago")),
                ends_at=datetime(2026, 9, 14, 10, 30, tzinfo=ZoneInfo("America/Chicago")),
                all_day=False,
                event_kind="available",
            )
            run_id = database.start_run()
            database.record_result(
                run_id,
                CrawlResult(source, HealthStatus.SUCCESS, calendar_events=(event,)),
                3,
            )
            with database.connect() as connection:
                self.assertEqual(1, connection.execute("SELECT COUNT(*) FROM calendar_events").fetchone()[0])
                self.assertEqual(0, connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def test_due_date_change_is_preserved(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        source = next(source for source in config.sources if source.id == "math2420-webwork")
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "test.sqlite3"
            database = Database(database_path)
            database.initialize()
            database.sync_sources(config.sources)
            tz = ZoneInfo("America/Chicago")
            first = DeadlineRecord(
                course_id=source.course_id,
                source_id=source.id,
                source_platform=source.platform,
                source_course_id=source.source_course_id,
                source_item_id="set-1",
                title="Homework 1",
                details_url="https://example.test/set-1",
                due_at=datetime(2026, 9, 1, 23, 59, tzinfo=tz),
                raw_date_label="Due September 1, 2026 at 11:59 PM CDT",
            )
            run_one = database.start_run()
            database.record_result(run_one, CrawlResult(source, HealthStatus.SUCCESS, (first,)), 3)
            database.finish_run(run_one, "success")

            changed = DeadlineRecord(
                **{**first.__dict__, "due_at": datetime(2026, 9, 2, 23, 59, tzinfo=tz)}
            )
            run_two = database.start_run()
            database.record_result(run_two, CrawlResult(source, HealthStatus.SUCCESS, (changed,)), 3)
            database.finish_run(run_two, "success")

            connection = sqlite3.connect(database_path)
            try:
                change = connection.execute(
                    "SELECT field_name, old_value, new_value FROM deadline_changes"
                ).fetchone()
            finally:
                connection.close()
            self.assertEqual("due_at", change[0])
            self.assertIn("2026-09-01", change[1])
            self.assertIn("2026-09-02", change[2])

    def test_calendar_event_changes_and_ambiguities_are_preserved(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        source = next(source for source in config.sources if source.id == "math2420-course-schedule")
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.sqlite3")
            database.initialize()
            database.sync_sources(config.sources)
            tz = ZoneInfo("America/Chicago")
            event = CalendarEventRecord(
                course_id=source.course_id,
                source_id=source.id,
                source_platform=source.platform,
                source_course_id=source.source_course_id,
                source_event_id="test-1",
                title="Test 1",
                details_url=source.url,
                starts_at=datetime(2026, 9, 23, 9, 5, tzinfo=tz),
                ends_at=datetime(2026, 9, 23, 9, 55, tzinfo=tz),
                all_day=False,
                event_kind="exam",
                canonical_key="exam:1",
            )
            first_run = database.start_run()
            database.record_result(
                first_run, CrawlResult(source, HealthStatus.SUCCESS, calendar_events=(event,)), 3
            )
            changed = CalendarEventRecord(
                **{**event.__dict__, "starts_at": datetime(2026, 9, 25, 9, 5, tzinfo=tz)}
            )
            issue = CalendarIssue(
                course_id=source.course_id,
                source_id=source.id,
                issue_key="conflict",
                title="Conflicting date",
                details_url=source.url,
                message="Two explicit dates disagree.",
                raw_value="September 23 / September 25",
            )
            second_run = database.start_run()
            database.record_result(
                second_run,
                CrawlResult(
                    source,
                    HealthStatus.SUCCESS,
                    calendar_events=(changed,),
                    issues=(issue,),
                ),
                3,
            )
            with database.connect() as connection:
                fields = {
                    row["field_name"]
                    for row in connection.execute("SELECT field_name FROM calendar_event_changes")
                }
                issue_count = connection.execute(
                    "SELECT COUNT(*) FROM source_issues WHERE active=1"
                ).fetchone()[0]
                observations = connection.execute(
                    "SELECT COUNT(*) FROM calendar_event_observations"
                ).fetchone()[0]
            self.assertIn("added", fields)
            self.assertIn("starts_at", fields)
            self.assertEqual(1, issue_count)
            self.assertEqual(2, observations)

    def test_failed_source_does_not_mark_items_missing(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        source = next(source for source in config.sources if source.id == "math2420-webwork")
        with tempfile.TemporaryDirectory() as temporary:
            database_path = Path(temporary) / "test.sqlite3"
            database = Database(database_path)
            database.initialize()
            database.sync_sources(config.sources)
            record = DeadlineRecord(
                course_id=source.course_id,
                source_id=source.id,
                source_platform=source.platform,
                source_course_id=source.source_course_id,
                source_item_id="set-1",
                title="Homework 1",
                details_url="https://example.test/set-1",
            )
            run_one = database.start_run()
            database.record_result(run_one, CrawlResult(source, HealthStatus.SUCCESS, (record,)), 3)
            run_two = database.start_run()
            database.record_result(run_two, CrawlResult(source, HealthStatus.LOGIN_REQUIRED), 3)

            connection = sqlite3.connect(database_path)
            try:
                missing = connection.execute(
                    "SELECT consecutive_missing FROM items"
                ).fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(0, missing)

    def test_obligation_metadata_and_resource_links_are_preserved(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        source = next(source for source in config.sources if source.id == "math2420-webwork")
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "test.sqlite3")
            database.initialize()
            database.sync_sources(config.sources)
            record = DeadlineRecord(
                course_id=source.course_id,
                source_id=source.id,
                source_platform=source.platform,
                source_course_id=source.source_course_id,
                source_item_id="relative",
                title="Required setup",
                details_url="https://example.test/setup",
                timing_text="Before the next class meeting.",
                description="Install the required tools.",
                canonical_key="todo:required-setup",
                component_kind="preparation",
                related_links=(("Instructions", "https://example.test/instructions"),),
            )
            run_id = database.start_run()
            database.record_result(
                run_id, CrawlResult(source, HealthStatus.SUCCESS, (record,)), 3
            )
            with database.connect() as connection:
                row = connection.execute("SELECT * FROM items").fetchone()
                observation = connection.execute(
                    "SELECT * FROM item_observations"
                ).fetchone()
            self.assertEqual("Before the next class meeting.", row["timing_text"])
            self.assertEqual("todo:required-setup", row["canonical_key"])
            self.assertEqual(
                [["Instructions", "https://example.test/instructions"]],
                json.loads(row["related_links"]),
            )
            self.assertEqual("preparation", observation["component_kind"])


if __name__ == "__main__":
    unittest.main()
