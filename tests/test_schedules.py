from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.adapters.schedules import (
    parse_cs2281_lectures,
    parse_cs3250_calendar,
    parse_math2420_text,
    parse_math3320_schedule,
)
from coursework_crawler.config import load_config
from coursework_crawler.semantics import canonical_event_key


ROOT = Path(__file__).resolve().parents[1]


class ScheduleParsingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(ROOT / "config" / "sources.example.toml")

    def source(self, source_id: str):
        return next(source for source in self.config.sources if source.id == source_id)

    def meeting(self, course_id: str):
        return next(meeting for meeting in self.config.classes if meeting.course_id == course_id)

    def test_math2420_pdf_uses_explicit_rows_and_flags_wrong_break_year(self) -> None:
        text = """
        Wednesday, September 23, 2026 Test 1
        Friday, October 23, 2026 Fall Break - No Class 4.7: 9-20
        Thanksgiving Break (November 21 - 29, 2025)
        """
        events, issues = parse_math2420_text(
            text, self.source("math2420-course-schedule"), self.meeting("math2420")
        )
        test_one = next(event for event in events if event.title == "Test 1")
        self.assertEqual("2026-09-23", test_one.starts_at.date().isoformat())
        self.assertEqual("09:00:00", test_one.starts_at.time().isoformat())
        self.assertEqual("exam", test_one.event_kind)
        self.assertTrue(any(issue.issue_key == "thanksgiving-year" for issue in issues))

    def test_math3320_invalid_and_off_pattern_dates_make_source_ambiguous(self) -> None:
        text = "\n".join((
            "September 26: Overview.",
            "September 28: Sections 1.2, 1.3, 1.4.",
            "September 31: Sections 1.6, 1.7.",
        ))
        events, issues = parse_math3320_schedule(
            text, self.source("math3320-course-schedule"), self.meeting("math3320")
        )
        self.assertEqual((), events)
        self.assertGreaterEqual(len(issues), 2)

    def test_cs3250_keeps_week_topics_and_exact_assessment_times(self) -> None:
        html = """
        <h2>WEEK OF SEPTEMBER 7th MODULE 2 - GRAPH CONCEPTS</h2>
        <p>Exam #1 - Wednesday, October 14th (in class)</p>
        <p>HW #2 Due - Wednesday, September 30th by 9AM</p>
        <p>FALL BREAK - Friday, October 23rd - No Class.</p>
        <p>LAST DAY OF CLASSES WEDNESDAY, December 9th</p>
        <p>Section 03 (MWF 1:25pm section) - THURSDAY, DECEMBER 17th, 2:00 PM - 5:00 PM.</p>
        """
        events = parse_cs3250_calendar(
            html, self.source("cs3250-course-calendar"), self.meeting("cs3250")
        )
        by_key = {event.canonical_key: event for event in events}
        self.assertIn("week:2026-09-07", by_key)
        self.assertEqual("09:00:00", by_key["due:hw:2"].starts_at.time().isoformat())
        self.assertEqual("13:00:00", by_key["exam:1"].starts_at.time().isoformat())
        self.assertEqual("14:00:00", by_key["exam:final:section-3"].starts_at.time().isoformat())

    def test_cs2281_lecture_number_is_stable_identity(self) -> None:
        html = """
        <p><span>AUGUST 28</span><span>LECTURE 2</span></p>
        <p><span style="font-size: 1.15em;">Digital abstraction, Binary numbers</span></p>
        """
        events = parse_cs2281_lectures(
            html, self.source("cs2281-lecture-schedule"), self.meeting("cs2281")
        )
        self.assertEqual(1, len(events))
        self.assertEqual("lecture:2", events[0].source_event_id)
        self.assertEqual("2026-08-28", events[0].starts_at.date().isoformat())

    def test_availability_and_decimal_homework_are_not_false_conflicts(self) -> None:
        self.assertEqual(
            "available:test-1",
            canonical_event_key("Test 1", "available"),
        )
        self.assertEqual(
            "due:homework-1-1",
            canonical_event_key("Homework 1.1", "due"),
        )


if __name__ == "__main__":
    unittest.main()
