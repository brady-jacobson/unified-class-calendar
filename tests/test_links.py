import unittest

from coursework_crawler.links import clean_resource_url


class ResourceLinkTests(unittest.TestCase):
    def test_strips_session_data_but_keeps_stable_ids(self) -> None:
        self.assertEqual(
            "https://example.invalid/details?ou=100001&db=900001",
            clean_resource_url("https://example.invalid/details?ou=100001&token=private&db=900001&effectiveUser=private"),
        )

    def test_rejects_signed_downloads_and_non_web_links(self) -> None:
        for url in (
            "https://example.invalid/file?X-Amz-Signature=private",
            "https://example.invalid/file?Signature=private",
            "javascript:alert(1)",
            "https://user:private@example.invalid/file",
        ):
            self.assertIsNone(clean_resource_url(url))
