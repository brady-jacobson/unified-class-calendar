from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.adapters.gradescope import (
    parse_gradescope_datetime,
    record_from_row,
)
from coursework_crawler.config import load_config


ROOT = Path(__file__).resolve().parents[1]


class GradescopeParsingTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        self.source = next(source for source in config.sources if source.id == "math3320-gradescope")

    def test_parses_offset_datetime(self) -> None:
        value = parse_gradescope_datetime("2026-09-04 11:59:00 -0500")
        self.assertEqual("2026-09-04T11:59:00-05:00", value.isoformat())

    def test_preserves_due_and_late_due_separately(self) -> None:
        record = record_from_row(
            self.source,
            {
                "item_id": "900001",
                "title": "HW1",
                "details_url": "/courses/200001/assignments/900001/submissions/submit_images",
                "status": "No Submission",
                "released_at": "2026-08-27 17:00:00 -0500",
                "due_at": "2026-09-04 11:59:00 -0500",
                "late_due_at": "2026-09-04 12:10:00 -0500",
                "raw_date_label": "Released at August 27 at 5:00PM | Due at September 04 at 11:59AM | Late Due Date at September 04 at 12:10PM",
            },
            "America/Chicago",
        )
        self.assertEqual("2026-09-04T11:59:00-05:00", record.due_at.isoformat())
        self.assertEqual("2026-09-04T12:10:00-05:00", record.late_due_at.isoformat())
        self.assertNotEqual(record.due_at, record.late_due_at)
        self.assertEqual("due:hw:1", record.canonical_key)
        self.assertEqual("submission", record.component_kind)
        self.assertEqual(
            "https://gradescope.example.invalid/courses/200001/assignments/900001/submissions/submit_images",
            record.details_url,
        )


if __name__ == "__main__":
    unittest.main()
