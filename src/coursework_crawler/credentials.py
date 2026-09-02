from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass


KEYCHAIN_PREFIX = "com.local.vanderbilt-coursework-crawler.credentials"
PLATFORM_CREDENTIAL = {
    "brightspace": "vanderbilt",
    "webwork": "webwork",
    "gradescope": "gradescope",
    "zybooks": "zybooks",
}
CREDENTIAL_NAMES = tuple(sorted(set(PLATFORM_CREDENTIAL.values())))


@dataclass(frozen=True)
class Credentials:
    username: str
    password: str


class KeychainCredentialStore:
    """Store login material in the user's macOS login Keychain."""

    @staticmethod
    def _service(name: str) -> str:
        if name not in CREDENTIAL_NAMES:
            raise ValueError(f"Unknown credential name: {name}")
        return f"{KEYCHAIN_PREFIX}.{name}"

    def set(self, name: str, credentials: Credentials) -> None:
        if not credentials.username or not credentials.password:
            raise ValueError("Username and password are required")
        payload = json.dumps(
            {"username": credentials.username, "password": credentials.password},
            separators=(",", ":"),
        )
        # ``security`` cannot accept a prompted secret from ordinary stdin: it
        # reads /dev/tty instead. ``-X`` is its noninteractive interface. We
        # pass hexadecimal password data directly to Popen (never through a
        # shell or logs) so scheduled installs can write predictably.
        encoded_payload = payload.encode().hex()
        result = subprocess.run(
            [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-a",
            "coursework-crawler",
            "-s",
            self._service(name),
            "-l",
            f"Coursework Crawler — {name}",
            "-X",
            encoded_payload,
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode:
            raise RuntimeError("macOS Keychain rejected the credential update")

    def get(self, name: str) -> Credentials | None:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                "coursework-crawler",
                "-s",
                self._service(name),
                "-w",
            ],
            text=True,
            capture_output=True,
        )
        if result.returncode:
            return None
        try:
            value = json.loads(result.stdout)
            username = str(value["username"])
            password = str(value["password"])
        except (json.JSONDecodeError, KeyError, TypeError):
            raise RuntimeError(f"The {name} Keychain item is malformed") from None
        if not username or not password:
            raise RuntimeError(f"The {name} Keychain item is incomplete")
        return Credentials(username, password)

    def configured(self, name: str) -> bool:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                "coursework-crawler",
                "-s",
                self._service(name),
            ],
            text=True,
            capture_output=True,
        )
        return result.returncode == 0

    def for_platform(self, platform: str) -> Credentials | None:
        name = PLATFORM_CREDENTIAL.get(platform)
        return self.get(name) if name else None
