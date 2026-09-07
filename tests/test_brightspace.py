from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.adapters.brightspace import (
    announcement_record_from_entry,
    assignment_record_from_row,
    calendar_record_from_ical_event,
    component_kind_from_text,
    content_record_from_row,
    coursework_canonical_key,
    enrich_brightspace_record,
    extract_relative_timing,
    parse_ical_events,
    parse_content_date_labels,
    parse_date_labels,
    quiz_record_from_row,
    split_lab_obligations,
)
from coursework_crawler.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class BrightspaceParsingTests(unittest.TestCase):
    def test_relative_reminder_with_abbreviated_homework_identity(self) -> None:
        source = load_config(ROOT / "config/sources.example.toml").sources[0]
        record = announcement_record_from_entry(source, {
            "title": "HW1 reminder", "item_id": "900099", "href": "/announcement/900099",
            "body": "The deadline of HW1 is within 2 hours. Please submit your work.",
        }, "America/Chicago")
        self.assertIsNotNone(record)
        self.assertIsNone(record.due_at)
        self.assertIn("within 2 hours", record.timing_text)
        self.assertEqual("due:hw:1", record.canonical_key)

    def setUp(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        self.assignments = next(s for s in config.sources if s.id == "cs3265-brightspace-assignments")
        self.quizzes = next(s for s in config.sources if s.id == "cs3250-brightspace-quizzes")
        self.survey = next(s for s in config.sources if s.id == "cs2281-brightspace-survey")
        self.calendar = next(s for s in config.sources if s.id == "brightspace-calendar-feed")
        self.announcements = next(
            s for s in config.sources if s.id == "math3320-brightspace-announcements"
        )

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

    def test_quiz_without_due_time_remains_an_untimed_obligation(self) -> None:
        record = quiz_record_from_row(
            self.quizzes,
            {"item_id": "900099", "title": "HW1 quiz", "status": "0 / 1",
             "date_text": "Available on Nov 13, 2026 10:00 AM until Dec 2, 2026 9:00 AM"},
            "America/Chicago",
        )
        self.assertIsNone(record.due_at)
        self.assertIsNotNone(record.available_until)
        self.assertEqual("quiz", record.component_kind)
        self.assertIn("No explicit due time", record.timing_text)

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

    def test_announcement_keeps_date_only_deadline_as_prose(self) -> None:
        record = announcement_record_from_entry(
            self.announcements,
            {
                "item_id": "900010",
                "title": "Pair-Share form",
                "href": "/announcements/900010",
                "body": "Please fill out the pair-share form. It is due Friday September 4th.",
                "links": [["Open form", "https://forms.example.invalid/pair-share"]],
            },
            "America/Chicago",
        )
        self.assertIsNotNone(record)
        self.assertIsNone(record.due_at)
        self.assertIn("due Friday", record.timing_text)
        self.assertEqual(
            (("Open form", "https://forms.example.invalid/pair-share"),),
            record.related_links,
        )

    def test_announcement_parses_only_explicit_full_due_timestamp(self) -> None:
        record = announcement_record_from_entry(
            self.announcements,
            {
                "item_id": "900011",
                "title": "Homework 2 deadline changed",
                "href": "/announcements/900011",
                "body": "The new deadline is September 11, 2026 11:59 PM CDT.",
                "links": [],
            },
            "America/Chicago",
        )
        self.assertEqual("2026-09-11T23:59:00-05:00", record.due_at.isoformat())
        self.assertIsNone(record.timing_text)

    def test_non_actionable_announcement_is_not_coursework(self) -> None:
        self.assertIsNone(
            announcement_record_from_entry(
                self.announcements,
                {
                    "item_id": "900012",
                    "title": "Welcome",
                    "href": "/announcements/900012",
                    "body": "Welcome to the course. Office hours are listed in Content.",
                    "links": [],
                },
                "America/Chicago",
            )
        )

    def test_assignment_detail_preserves_relative_lab_components_and_links(self) -> None:
        base = assignment_record_from_row(
            self.assignments,
            {
                "title": "Lab 1",
                "href": "/d2l/lms/dropbox/user/folder_submit_files.d2l?db=900020&ou=100005",
                "date_text": "",
                "status": "Not submitted",
            },
            "America/Chicago",
        )
        body = (
            "The Lab 1 pre-lab demonstration is required at the start of your lab section "
            "during the week of September 7. Your individual report must be submitted "
            "before the start of your lab section during the week of September 14."
        )
        enriched = enrich_brightspace_record(
            base,
            body,
            [["Lab instructions", "https://lms.example.invalid/lab1.pdf"]],
        )
        records = split_lab_obligations(enriched, body)
        self.assertEqual(2, len(records))
        self.assertEqual({"pre-lab", "report"}, {record.component_kind for record in records})
        self.assertTrue(all(record.due_at is None for record in records))
        self.assertTrue(all(record.timing_text for record in records))
        self.assertTrue(all(record.canonical_key == "due:lab:1" for record in records))
        self.assertEqual(
            (("Lab instructions", "https://lms.example.invalid/lab1.pdf"),),
            records[0].related_links,
        )

    def test_no_prelab_language_does_not_create_a_requirement(self) -> None:
        base = assignment_record_from_row(
            self.assignments,
            {
                "title": "Lab 0",
                "href": "/d2l/lms/dropbox/user/folder_submit_files.d2l?db=900021&ou=100005",
                "date_text": "",
                "status": "Submitted",
            },
            "America/Chicago",
        )
        body = "No pre-lab demonstration is required for Lab 0. Take the quiz after the lab."
        records = split_lab_obligations(enrich_brightspace_record(base, body, []), body)
        self.assertFalse(any(record.component_kind == "pre-lab" for record in records))

    def test_content_identity_and_relative_timing_are_conservative(self) -> None:
        self.assertEqual("due:hw:4", coursework_canonical_key("Homework 4 FAQ"))
        self.assertEqual("due:zy:2", coursework_canonical_key("ZY-2 reading"))
        self.assertEqual("todo:software-setup", coursework_canonical_key("Software Setup"))
        self.assertEqual("FAQ", component_kind_from_text("Homework 4 FAQ"))
        self.assertIsNone(extract_relative_timing("This module opens next week."))
        self.assertIn(
            "before the next class",
            extract_relative_timing("Install the software before the next class meeting."),
        )


if __name__ == "__main__":
    unittest.main()
