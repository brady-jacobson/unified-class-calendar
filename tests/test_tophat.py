import unittest
from coursework_crawler.adapters.tophat import folder_children, record_from_content, validate_file_pages
from coursework_crawler.models import SourceConfig


class TopHatTests(unittest.TestCase):
    source = SourceConfig("example-tophat", "example", "Example", "tophat", "tophat",
                          "900001", "https://example.invalid/course")

    def test_file_page_sequence_must_be_complete(self) -> None:
        validate_file_pages({1: "Title", 2: "Required form", 3: "End"})
        for pages in ({}, {2: "Missing first"}, {1: "First", 3: "Missing middle"}):
            with self.subTest(pages=pages), self.assertRaises(ValueError):
                validate_file_pages(pages)

    def test_image_only_file_page_requires_review(self) -> None:
        with self.assertRaisesRegex(ValueError, "visual review"):
            validate_file_pages({1: "Text", 2: " "})

    def test_folder_enumeration_counts_direct_children_not_descendants(self) -> None:
        parent = {"id": "folder", "level": 1, "label": "Readings, Folder, 3 items"}
        rows = [parent, {"id": "nested", "level": 2, "label": "Nested, Folder, 2 items"},
                {"id": "leaf", "level": 3, "label": "Page, File"},
                {"id": "file", "level": 2, "label": "Reading, File"},
                {"id": "next-folder", "level": 1, "label": "Next, Folder, 1 item"}]
        self.assertEqual(["nested", "file"], [row["id"] for row in folder_children(rows, parent)])
        with self.assertRaisesRegex(ValueError, "advertised count"):
            folder_children(rows[:2], parent)

    def test_live_participation_is_not_an_asynchronous_obligation(self) -> None:
        self.assertIsNone(record_from_content(self.source, "Question 1", "Answer during today's lecture.", self.source.url))
        self.assertIsNone(record_from_content(self.source, "Live poll", "Fill out this form during class.", self.source.url))
        self.assertIsNone(record_from_content(self.source, "Homework example", "Homework runtime increases due to recursion.", self.source.url))

    def test_assigned_work_without_deadline_keywords_is_retained(self) -> None:
        record = record_from_content(self.source, "Practice A", "Practice A", self.source.url, assigned=True)
        self.assertIsNotNone(record)
        self.assertIsNone(record.due_at)
        self.assertEqual("Practice A", record.timing_text)

    def test_unassigned_metadata_without_obligation_is_ignored(self) -> None:
        self.assertIsNone(record_from_content(self.source, "Practice A", "Practice A", self.source.url))

    def test_form_deadline_without_time_is_retained_as_prose(self) -> None:
        record = record_from_content(self.source, "Pair-Share", "Fill out the form by Friday September 4th.", self.source.url, transcript=True)
        self.assertIsNotNone(record)
        self.assertIsNone(record.due_at)
        self.assertIn("September 4th", record.timing_text)

    def test_generated_transcript_cannot_establish_exact_deadline(self) -> None:
        record = record_from_content(self.source, "Homework 1", "Homework 1 due September 9, 2026 9:00 AM.", self.source.url, transcript=True)
        self.assertIsNone(record.due_at)
        self.assertIn("verify", record.description)
