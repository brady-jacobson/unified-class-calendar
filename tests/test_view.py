from __future__ import annotations

import tempfile
import unittest
import json
from dataclasses import replace
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
from coursework_crawler.view import render_dashboard


ROOT = Path(__file__).resolve().parents[1]


class DashboardTests(unittest.TestCase):
    def test_package_merge_keeps_components_policies_and_undated_prompt(self) -> None:
        config = load_config(ROOT / "config/sources.example.toml")
        written = next(s for s in config.sources if s.id == "cs3250-gradescope")
        quiz = next(s for s in config.sources if s.id == "cs3250-brightspace-quizzes")
        content = replace(quiz, id="example-content", adapter="brightspace_content_tree")
        tz = ZoneInfo("America/Chicago")
        due = datetime(2026, 9, 9, 9, tzinfo=tz)
        base = DeadlineRecord(
            course_id=written.course_id, source_id=written.id,
            source_platform=written.platform, source_course_id=written.source_course_id,
            source_item_id="900100", title="HW 1", details_url="https://example.invalid/submit",
            due_at=due, late_due_at=datetime(2026, 9, 11, 9, tzinfo=tz),
            component_kind="written submission", canonical_key="due:hw:1",
        )
        with tempfile.TemporaryDirectory() as temporary:
            db = Database(Path(temporary) / "db.sqlite3")
            db.initialize()
            db.sync_sources((*config.sources, content))
            run = db.start_run()
            for source, record in (
                (quiz, replace(base, source_id=quiz.id, source_platform="brightspace",
                    source_item_id="900101", title="HW1 quiz", details_url="https://example.invalid/quiz",
                    late_due_at=None, component_kind="quiz", description="No late work accepted.")),
                (written, base),
                (content, replace(base, source_id=content.id, source_platform="brightspace",
                    source_item_id="900102", title="HW1 FAQ", due_at=None, late_due_at=None,
                    details_url="https://example.invalid/faq", component_kind="FAQ")),
            ):
                db.record_result(run, CrawlResult(source, HealthStatus.SUCCESS, (record,)), 3)
            output = Path(temporary) / "index.html"
            render_dashboard(db.path, output)
            document = output.read_text()
            events = json.loads(document.split("const ALL_EVENTS=", 1)[1].split(";\n", 1)[0])
            self.assertEqual(1, len(events))
            event = events[0]
            self.assertEqual(base.details_url, event["url"])
            self.assertEqual(3, len(event["provenance"]))
            self.assertEqual({"written submission", "quiz", "FAQ"}, {
                p["componentKind"] for p in event["provenance"]
            })
            self.assertTrue(any(p.get("description") == "No late work accepted." for p in event["provenance"]))
            self.assertEqual(base.late_due_at.isoformat(), event["lateDueAt"])

    def test_dashboard_shows_only_explicit_future_due_dates_and_health(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        source = next(source for source in config.sources if source.id == "math2420-webwork")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "db.sqlite3")
            database.initialize()
            database.sync_sources(config.sources)
            database.sync_class_meetings(config.classes)
            run_id = database.start_run()
            records = (
                DeadlineRecord(
                    course_id=source.course_id,
                    source_id=source.id,
                    source_platform=source.platform,
                    source_course_id=source.source_course_id,
                    source_item_id="dated",
                    title="Future explicit due date",
                    details_url="https://example.test/dated",
                    due_at=datetime(2099, 9, 1, 23, 59, tzinfo=ZoneInfo("America/Chicago")),
                ),
                DeadlineRecord(
                    course_id=source.course_id,
                    source_id=source.id,
                    source_platform=source.platform,
                    source_course_id=source.source_course_id,
                    source_item_id="undated",
                    title="Opening only",
                    details_url="https://example.test/undated",
                    available_from=datetime(2099, 8, 31, tzinfo=ZoneInfo("America/Chicago")),
                ),
            )
            database.record_result(
                run_id,
                CrawlResult(source, HealthStatus.SUCCESS, records, message="healthy"),
                3,
            )
            database.finish_run(run_id, "success")
            output = root / "index.html"
            render_dashboard(database.path, output)
            document = output.read_text(encoding="utf-8")
            self.assertIn("Future explicit due date", document)
            self.assertNotIn("Opening only", document)
            self.assertIn("Vanderbilt Calendar", document)
            self.assertIn("classesToggle", document)
            self.assertIn("courseworkToggle", document)
            self.assertIn('id="twoDayBtn"', document)
            self.assertIn("const renderTwoDay", document)
            self.assertIn("mode==='two'?addDays(cursor,-1)", document)
            self.assertIn("mode==='two'?addDays(cursor,1)", document)
            self.assertIn('id="eventPopover"', document)
            self.assertIn('data-event-id=', document)
            self.assertIn("Open assignment details", document)
            self.assertIn("Other sources", document)
            self.assertIn("Source health", document)
            self.assertIn("Schedule ambiguities", document)
            self.assertIn("Recent schedule changes", document)
            self.assertIn("eventOccursOn", document)
            self.assertIn(".calendar-wrap{overflow-x:auto;overflow-y:hidden", document)
            self.assertNotIn(".calendar-wrap{overflow:auto", document)
            self.assertIn("healthy", document)

    def test_dashboard_deduplicates_same_instant_and_shows_provenance_and_issues(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        feed = next(source for source in config.sources if source.id == "brightspace-calendar-feed")
        schedule = next(source for source in config.sources if source.id == "cs3250-course-calendar")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "db.sqlite3")
            database.initialize()
            database.sync_sources(config.sources)
            database.sync_class_meetings(config.classes)
            utc_event = CalendarEventRecord(
                course_id="cs3250",
                source_id=feed.id,
                source_platform=feed.platform,
                source_course_id="100003",
                source_event_id="feed-final",
                title="CS 3250 Final Exam - Section 03",
                details_url=feed.url,
                starts_at=datetime(2026, 12, 17, 20, tzinfo=ZoneInfo("UTC")),
                ends_at=datetime(2026, 12, 17, 23, tzinfo=ZoneInfo("UTC")),
                all_day=False,
                event_kind="exam",
                canonical_key="exam:final:section-3",
            )
            local_event = CalendarEventRecord(
                **{
                    **utc_event.__dict__,
                    "source_id": schedule.id,
                    "source_event_id": "schedule-final",
                    "details_url": schedule.url,
                    "starts_at": datetime(2026, 12, 17, 14, tzinfo=ZoneInfo("America/Chicago")),
                    "ends_at": datetime(2026, 12, 17, 17, tzinfo=ZoneInfo("America/Chicago")),
                }
            )
            run_id = database.start_run()
            database.record_result(
                run_id,
                CrawlResult(feed, HealthStatus.SUCCESS, calendar_events=(utc_event,)),
                3,
            )
            issue = CalendarIssue(
                course_id="cs3250",
                source_id=schedule.id,
                issue_key="example",
                title="Explicit ambiguity",
                details_url=schedule.url,
                message="The source has two incompatible values.",
                raw_value="A / B",
            )
            database.record_result(
                run_id,
                CrawlResult(
                    schedule,
                    HealthStatus.SUCCESS,
                    calendar_events=(local_event,),
                    issues=(issue,),
                ),
                3,
            )
            output = root / "index.html"
            render_dashboard(database.path, output)
            document = output.read_text(encoding="utf-8")
            self.assertEqual(1, document.count('"canonicalKey":"exam:final:section-3"'))
            self.assertIn('"source":"Course schedule + Brightspace calendar"', document)
            self.assertIn("eventSources(e)", document)
            self.assertIn("Primary source:", document)
            self.assertIn("Explicit ambiguity", document)

    def test_dashboard_preserves_unscheduled_work_late_deadlines_and_resource_links(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        source = next(source for source in config.sources if source.id == "math2420-webwork")
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = Database(root / "db.sqlite3")
            database.initialize()
            database.sync_sources(config.sources)
            database.sync_class_meetings(config.classes)
            tz = ZoneInfo("America/Chicago")
            records = (
                DeadlineRecord(
                    course_id=source.course_id,
                    source_id=source.id,
                    source_platform=source.platform,
                    source_course_id=source.source_course_id,
                    source_item_id="dated",
                    title="Homework 9",
                    details_url="https://example.test/homework",
                    due_at=datetime(2099, 9, 9, 23, 59, tzinfo=tz),
                    late_due_at=datetime(2099, 9, 10, 12, 0, tzinfo=tz),
                    component_kind="written submission",
                    related_links=(
                        ("Prompt", "https://example.test/prompt"),
                        ("FAQ", "https://example.test/faq"),
                    ),
                ),
                DeadlineRecord(
                    course_id=source.course_id,
                    source_id=source.id,
                    source_platform=source.platform,
                    source_course_id=source.source_course_id,
                    source_item_id="relative",
                    title="Pre-lab demonstration",
                    details_url="https://example.test/prelab",
                    timing_text="At the start of the assigned lab section.",
                ),
            )
            run_id = database.start_run()
            database.record_result(
                run_id, CrawlResult(source, HealthStatus.SUCCESS, records), 3
            )
            output = root / "index.html"
            render_dashboard(database.path, output)
            document = output.read_text(encoding="utf-8")
            self.assertIn("Required without exact time", document)
            self.assertIn("Pre-lab demonstration", document)
            self.assertIn("At the start of the assigned lab section.", document)
            self.assertIn('"lateDueAt":"2099-09-10T12:00:00-05:00"', document)
            self.assertIn('"source":"Prompt"', document)
            self.assertIn('"source":"FAQ"', document)
            self.assertIn("Late deadline:", document)
            self.assertIn("Component:", document)


if __name__ == "__main__":
    unittest.main()
