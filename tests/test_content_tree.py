from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.adapters.brightspace import content_record_from_api_item, module_description_records
from coursework_crawler.adapters.content_tree import html_details
from coursework_crawler.config import load_config


class ContentTreeTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(Path(__file__).resolve().parents[1] / "config/sources.example.toml")
        self.source = next(s for s in config.sources if s.id == "cs2281-brightspace-homework")

    def test_topic_keeps_package_identity_and_availability_separate(self) -> None:
        record = content_record_from_api_item(self.source, {
            "Id": 90001, "Type": 1, "Title": "FAQ and submission policy",
            "StartDate": "2026-09-01T14:00:00Z", "EndDate": "2026-09-09T14:00:00Z",
        }, ("Homeworks", "HW 1"))
        self.assertIsNone(record.due_at)
        self.assertEqual("due:hw:1", record.canonical_key)
        self.assertEqual("FAQ", record.component_kind)
        self.assertIn("/topics/90001", record.details_url)
        self.assertEqual(9, record.available_until.day)

    def test_description_links_keep_each_assignment_identity(self) -> None:
        records = module_description_records(self.source, {
            "Id": 90002, "Title": "Homework", "Description": {
                "Html": '<a href="/files/hw1.pdf">hw1</a> <a href="/files/hw2.pdf">hw2</a>'
            },
        })
        self.assertEqual(["due:hw:1", "due:hw:2"], [r.canonical_key for r in records])
        self.assertTrue(all(r.due_at is None for r in records))

    def test_html_keeps_resource_labels_and_ignores_scripts(self) -> None:
        text, links = html_details('<p>Before class meeting</p><a href="/setup">Install software</a><script>private()</script>')
        self.assertIn("Before class meeting", text)
        self.assertNotIn("private()", text)
        self.assertEqual([["Install software", "/setup"]], links)


if __name__ == "__main__":
    unittest.main()
