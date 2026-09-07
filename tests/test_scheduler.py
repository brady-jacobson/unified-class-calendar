from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from coursework_crawler.scheduler import (
    calendar_server_payload,
    daily_runner_script,
    launch_agent_payload,
    write_launch_agent,
)


class SchedulerTests(unittest.TestCase):
    def test_payload_runs_at_login_and_daily_from_native_root(self) -> None:
        root = Path("/tmp/coursework-crawler")
        payload = launch_agent_payload(root, 6, 15)
        self.assertEqual({"Hour": 6, "Minute": 15}, payload["StartCalendarInterval"])
        self.assertTrue(payload["RunAtLoad"])
        self.assertEqual(str(root), payload["WorkingDirectory"])
        self.assertEqual([str(root / "bin/run-daily")], payload["ProgramArguments"])

    def test_write_requires_installed_crawler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(FileNotFoundError):
                write_launch_agent(root, root / "agent.plist", 6, 0)

    def test_calendar_server_is_local_persistent_and_does_not_open_a_browser(self) -> None:
        root = Path("/tmp/coursework-crawler")
        payload = calendar_server_payload(root)
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        arguments = payload["ProgramArguments"]
        self.assertIn("127.0.0.1", arguments)
        self.assertIn("8765", arguments)
        self.assertNotIn("open", " ".join(arguments))

    def test_daily_runner_has_guard_and_force_without_notifications(self) -> None:
        script = daily_runner_script(Path("/tmp/coursework-crawler"))
        self.assertIn("last-run-date", script)
        self.assertIn("--force", script)
        self.assertIn("login_required", script)
        self.assertNotIn("osascript", script)
        self.assertNotIn("display notification", script)
        self.assertIn('[[ "$STATUS" -eq 0 ]]', script)
        self.assertIn('export COURSEWORK_CRAWLER_HOME="$RUNTIME_ROOT"', script)
        self.assertIn('cd "$RUNTIME_ROOT" || exit 1', script)


if __name__ == "__main__":
    unittest.main()
