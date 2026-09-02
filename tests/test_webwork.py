from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.adapters.webwork import (
    canonicalize_webwork_url,
    parse_webwork_date,
    record_from_row,
)
from coursework_crawler.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class WeBWorKParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        self.source = next(source for source in config.sources if source.id == "math2420-webwork")

    def test_parses_explicit_due_date(self) -> None:
        label, value, abbreviation = parse_webwork_date(
            "Open. Due September 1, 2026 at 11:59:00 PM CDT."
        )
        self.assertEqual("Due", label)
        self.assertEqual("2026-09-01T23:59:00-05:00", value.isoformat())
        self.assertEqual("America/Chicago", abbreviation)

    def test_future_item_has_availability_but_no_due_date(self) -> None:
        record = record_from_row(
            self.source,
            {
                "title": "Homework 1.3",
                "href": "/webwork2/ExampleCourse2026/Homework_1.3?effectiveUser=student",
                "date_text": "Will open on August 31, 2026 at 12:00:00 AM CDT.",
                "status": "not-open",
            },
        )
        self.assertIsNone(record.due_at)
        self.assertEqual("2026-08-31T00:00:00-05:00", record.available_from.isoformat())

    def test_canonical_url_removes_only_effective_user(self) -> None:
        canonical = canonicalize_webwork_url(
            self.source.url,
            "/webwork2/ExampleCourse2026/Homework_1.1?effectiveUser=student&foo=bar",
        )
        self.assertEqual(
            "https://webwork.example.invalid/webwork2/ExampleCourse2026/Homework_1.1?foo=bar",
            canonical,
        )


if __name__ == "__main__":
    unittest.main()
