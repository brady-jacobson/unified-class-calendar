from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.adapters.zybooks import parse_zybooks_due, record_from_row
from coursework_crawler.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class ZyBooksParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        self.source = next(source for source in config.sources if source.id == "cs2281-zybooks")

    def test_parses_explicit_central_due_date(self) -> None:
        parsed = parse_zybooks_due("Due: 09/04/2026, 11:59 PM CDT")
        self.assertEqual("2026-09-04T23:59:00-05:00", parsed[0].isoformat())
        self.assertEqual("America/Chicago", parsed[1])

    def test_uses_live_assignment_id_and_course_details_url(self) -> None:
        record = record_from_row(
            self.source,
            {
                "item_id": "900003",
                "title": "ZY-1",
                "date_text": "Due: 09/04/2026, 11:59 PM CDT",
                "status": "0 / 154 points",
                "details_url": self.source.url,
            },
        )
        self.assertEqual("900003", record.source_item_id)
        self.assertEqual(self.source.url, record.details_url)
        self.assertEqual("2026-09-04T23:59:00-05:00", record.due_at.isoformat())


if __name__ == "__main__":
    unittest.main()
