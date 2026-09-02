from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.adapters.brightspace import (
    assignment_record_from_row,
    calendar_record_from_ical_event,
    content_record_from_row,
    parse_ical_events,
    parse_content_date_labels,
    parse_date_labels,
    quiz_record_from_row,
)
from coursework_crawler.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class BrightspaceParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        self.assignments = next(s for s in config.sources if s.id == "cs3265-brightspace-assignments")
        self.quizzes = next(s for s in config.sources if s.id == "cs3250-brightspace-quizzes")
        self.survey = next(s for s in config.sources if s.id == "cs2281-brightspace-survey")
        self.calendar = next(s for s in config.sources if s.id == "brightspace-calendar-feed")

    def test_availability_end_is_not_due_date(self) -> None:
        values = parse_date_labels(
            "Available on Aug 31, 2026 10:00 AM until Sep 9, 2026 9:00 AM",
            "America/Chicago",
        )
        self.assertIsNone(values["due_at"])
        self.assertEqual("2026-08-31T10:00:00-05:00", values["available_from"].isoformat())
        self.assertEqual("2026-09-09T09:00:00-05:00", values["available_until"].isoformat())

    def test_assignment_uses_live_db_identifier_and_explicit_due(self) -> None:
        record = assignment_record_from_row(
            self.assignments,
            {
                "title": "In class Activity 01",
                "href": "/d2l/lms/dropbox/user/folder_submit_files.d2l?db=900005&ou=100005",
                "date_text": "Due on Aug 27, 2026 12:30 PM Available on Aug 27, 2026 10:59 AM",
                "status": "1 Submission, 1 File",
            },
            "America/Chicago",
        )
        self.assertEqual("900005", record.source_item_id)
        self.assertEqual("2026-08-27T12:30:00-05:00", record.due_at.isoformat())
        self.assertIsNone(record.available_until)

    def test_quiz_keeps_due_and_availability_end_separate(self) -> None:
        record = quiz_record_from_row(
            self.quizzes,
            {
                "item_id": "900002",
                "title": "HW5 MoMs Selection Quiz B (F26)",
                "date_text": "Due on Dec 2, 2026 9:00 AM Available on Nov 13, 2026 10:00 AM until Dec 2, 2026 9:00 AM",
                "status": "0 / 1",
            },
            "America/Chicago",
        )
        self.assertEqual("2026-12-02T09:00:00-06:00", record.due_at.isoformat())
        self.assertEqual("2026-12-02T09:00:00-06:00", record.available_until.isoformat())
        self.assertIsNone(record.late_due_at)
        self.assertEqual(
            "https://brightspace.vanderbilt.edu/d2l/lms/quizzing/user/quiz_summary.d2l?qi=900002&ou=100003",
            record.details_url,
        )

    def test_content_availability_start_is_not_due_date(self) -> None:
        values = parse_content_date_labels(
            "Availability started August 26, 2026 10:00 AM",
            "America/Chicago",
        )
        self.assertIsNone(values["due_at"])
        self.assertEqual("2026-08-26T10:00:00-05:00", values["available_from"].isoformat())

    def test_content_preserves_live_due_year_and_separate_availability(self) -> None:
        record = content_record_from_row(
            self.survey,
            {
                "item_id": "900004",
                "title": "First Week Student Survey",
                "href": "/d2l/le/lessons/100004/topics/900004",
                "date_text": (
                    "Due: August 30, 2025 11:59 PM "
                    "Available from August 26, 2026 10:00 AM to September 5, 2026 11:59 PM."
                ),
                "status": "Completed",
            },
            "America/Chicago",
        )
        self.assertEqual("2025-08-30T23:59:00-05:00", record.due_at.isoformat())
        self.assertEqual("2026-08-26T10:00:00-05:00", record.available_from.isoformat())
        self.assertEqual("2026-09-05T23:59:00-05:00", record.available_until.isoformat())
        self.assertEqual("900004", record.source_item_id)

    def test_calendar_feed_keeps_due_and_availability_semantics_distinct(self) -> None:
        payload = """BEGIN:VCALENDAR\r
BEGIN:VEVENT\r
UID:event-due\r
DTSTART:20260930T140000Z\r
DTEND:20260930T140000Z\r
SUMMARY:HW 2 - Due\r
LOCATION:COURSE C\r
DESCRIPTION:View event - https://lms.example.invalid/calendar/event/1\r
END:VEVENT\r
BEGIN:VEVENT\r
UID:event-open\r
DTSTART:20260914T150000Z\r
DTEND:20260914T150000Z\r
SUMMARY:Module 3 - Available\r
LOCATION:COURSE C\r
DESCRIPTION:View event - https://lms.example.invalid/calendar/event/2\r
END:VEVENT\r
END:VCALENDAR\r
"""
        events = parse_ical_events(payload)
        due = calendar_record_from_ical_event(self.calendar, events[0], "America/Chicago")
        available = calendar_record_from_ical_event(self.calendar, events[1], "America/Chicago")
        self.assertEqual("due", due.event_kind)
        self.assertEqual("HW 2", due.title)
        self.assertEqual("2026-09-30T14:00:00+00:00", due.starts_at.isoformat())
        self.assertEqual("available", available.event_kind)
        self.assertEqual("Module 3", available.title)

    def test_calendar_feed_filters_unmapped_and_recurring_events(self) -> None:
        payload = """BEGIN:VCALENDAR\nBEGIN:VEVENT\nUID:old-course\nDTSTART:20260901T140000Z\nDTEND:20260901T150000Z\nSUMMARY:Unrelated\nLOCATION:Other Course\nEND:VEVENT\nBEGIN:VEVENT\nUID:recurring\nDTSTART:20260902T140000Z\nDTEND:20260902T150000Z\nRRULE:FREQ=WEEKLY\nSUMMARY:COURSE C\nLOCATION:COURSE C\nEND:VEVENT\nEND:VCALENDAR\n"""
        events = parse_ical_events(payload)
        self.assertIsNone(calendar_record_from_ical_event(self.calendar, events[0], "America/Chicago"))
        self.assertIsNone(calendar_record_from_ical_event(self.calendar, events[1], "America/Chicago"))

    def test_calendar_feed_filters_other_merged_sections_and_cleans_links(self) -> None:
        payload = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:wrong-section
DTSTART:20261216T200000Z
DTEND:20261216T230000Z
SUMMARY:Final Exam - Section 01
LOCATION:COURSE C
END:VEVENT
BEGIN:VEVENT
UID:module
DTSTART:20260914T150000Z
DTEND:20260914T150000Z
SUMMARY:Module 3 - Available
LOCATION:COURSE C
DESCRIPTION:Open - https://app.tophat.com/"
END:VEVENT
END:VCALENDAR
"""
        events = parse_ical_events(payload)
        self.assertIsNone(calendar_record_from_ical_event(self.calendar, events[0], "America/Chicago"))
        module = calendar_record_from_ical_event(self.calendar, events[1], "America/Chicago")
        self.assertEqual("https://app.tophat.com/", module.details_url)


if __name__ == "__main__":
    unittest.main()
