from __future__ import annotations

import json
import subprocess
import unittest
from unittest.mock import patch

from coursework_crawler.credentials import (
    Credentials,
    KeychainCredentialStore,
    PLATFORM_CREDENTIAL,
)


class KeychainCredentialTests(unittest.TestCase):
    def test_webwork_has_a_distinct_credential_record(self) -> None:
        self.assertEqual("webwork", PLATFORM_CREDENTIAL["webwork"])
        self.assertNotEqual(PLATFORM_CREDENTIAL["webwork"], PLATFORM_CREDENTIAL["brightspace"])

    @patch("coursework_crawler.credentials.subprocess.run")
    def test_password_is_hex_encoded_for_noninteractive_keychain_write(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess([], 0, "", "")
        store = KeychainCredentialStore()
        store.set("vanderbilt", Credentials("student", "top-secret"))
        arguments = run.call_args.args[0]
        self.assertNotIn("top-secret", arguments)
        self.assertEqual("-X", arguments[-2])
        payload = json.loads(bytes.fromhex(arguments[-1]).decode())
        self.assertEqual("student", payload["username"])
        self.assertEqual("top-secret", payload["password"])

    @patch("coursework_crawler.credentials.subprocess.run")
    def test_reads_structured_secret_from_keychain(self, run: object) -> None:
        value = json.dumps({"username": "student", "password": "secret"}) + "\n"
        run.return_value = subprocess.CompletedProcess([], 0, value, "")
        credentials = KeychainCredentialStore().get("zybooks")
        self.assertEqual(Credentials("student", "secret"), credentials)

    @patch("coursework_crawler.credentials.subprocess.run")
    def test_missing_item_returns_none(self, run: object) -> None:
        run.return_value = subprocess.CompletedProcess([], 44, "", "not found")
        self.assertIsNone(KeychainCredentialStore().get("gradescope"))


if __name__ == "__main__":
    unittest.main()
