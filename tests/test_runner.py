from __future__ import annotations

import unittest
from pathlib import Path

from coursework_crawler.config import load_config
from coursework_crawler.cli import run_headless
from coursework_crawler.models import CrawlResult, HealthStatus
from coursework_crawler.runner import authentication_preflight, crawl_with_interactive_auth


ROOT = Path(__file__).resolve().parents[1]


class SequencedAdapter:
    def __init__(self, results: list[CrawlResult]):
        self.results = results
        self.calls = 0

    def crawl(self, page: object, source: object) -> CrawlResult:
        result = self.results[self.calls]
        self.calls += 1
        return result


class FakePage:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self) -> None:
        self.pages = [FakePage()]

    def new_page(self) -> FakePage:
        page = FakePage()
        self.pages.append(page)
        return page


class InteractiveAuthenticationTests(unittest.TestCase):
    def setUp(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        self.source = next(source for source in config.sources if source.id == "math2420-webwork")

    def test_headed_run_waits_for_login_but_does_not_retry_crawl(self) -> None:
        adapter = SequencedAdapter([
            CrawlResult(self.source, HealthStatus.LOGIN_REQUIRED, message="sign in"),
        ])
        prompts: list[str] = []
        result = crawl_with_interactive_auth(
            adapter,
            object(),
            self.source,
            headless=False,
            input_fn=lambda prompt: prompts.append(prompt) or "",
        )
        self.assertEqual(HealthStatus.LOGIN_REQUIRED, result.health)
        self.assertEqual(1, adapter.calls)
        self.assertIn("open Chrome window", prompts[0])

    def test_headless_run_returns_login_without_prompting(self) -> None:
        adapter = SequencedAdapter([
            CrawlResult(self.source, HealthStatus.LOGIN_REQUIRED, message="sign in"),
        ])
        result = crawl_with_interactive_auth(
            adapter,
            object(),
            self.source,
            headless=True,
            input_fn=lambda prompt: self.fail("headless crawl prompted for input"),
        )
        self.assertEqual(HealthStatus.LOGIN_REQUIRED, result.health)
        self.assertEqual(1, adapter.calls)

    def test_plain_terminal_run_is_interactive(self) -> None:
        self.assertFalse(run_headless(force_headed=False, stdin_is_tty=True))

    def test_unattended_run_remains_headless(self) -> None:
        self.assertTrue(run_headless(force_headed=False, stdin_is_tty=False))

    def test_preflight_collects_all_login_tabs_before_crawling(self) -> None:
        config = load_config(ROOT / "config" / "sources.example.toml")
        representatives = []
        platforms = set()
        for source in config.sources:
            if source.enabled and source.platform not in platforms:
                representatives.append(source)
                platforms.add(source.platform)

        calls: dict[str, int] = {}
        page_platforms: dict[int, str] = {}

        def checker(page: FakePage, source: object) -> CrawlResult:
            page_platforms[id(page)] = source.platform
            calls[source.platform] = calls.get(source.platform, 0) + 1
            needs_first_login = source.platform in {"webwork", "zybooks"} and calls[source.platform] == 1
            health = HealthStatus.LOGIN_REQUIRED if needs_first_login else HealthStatus.SUCCESS
            return CrawlResult(source, health)

        prompts: list[str] = []
        context = FakeContext()
        failures, crawl_page = authentication_preflight(
            context,
            tuple(representatives),
            headless=False,
            input_fn=lambda prompt: prompts.append(prompt) or "",
            checker=checker,
            authenticator=lambda page, source: False,
        )

        self.assertEqual((), failures)
        self.assertIsNotNone(crawl_page)
        self.assertEqual(1, len(prompts))
        self.assertIn("webwork", prompts[0])
        self.assertIn("zybooks", prompts[0])
        self.assertEqual(2, calls["webwork"])
        self.assertEqual(2, calls["zybooks"])
        self.assertEqual(1, calls["gradescope"])
        self.assertEqual(1, calls["brightspace"])
        for page in context.pages:
            platform = page_platforms.get(id(page))
            if platform in {"gradescope", "brightspace"}:
                self.assertTrue(page.closed)

    def test_preflight_automatically_logs_in_before_prompting(self) -> None:
        source = self.source
        context = FakeContext()
        checks = 0
        login_attempts = 0

        def checker(page: FakePage, configured_source: object) -> CrawlResult:
            nonlocal checks
            checks += 1
            health = HealthStatus.LOGIN_REQUIRED if checks == 1 else HealthStatus.SUCCESS
            return CrawlResult(configured_source, health)

        def authenticator(page: FakePage, configured_source: object) -> bool:
            nonlocal login_attempts
            login_attempts += 1
            return True

        failures, crawl_page = authentication_preflight(
            context,
            (source,),
            headless=True,
            checker=checker,
            authenticator=authenticator,
            input_fn=lambda prompt: self.fail("successful automatic login prompted"),
        )
        self.assertEqual((), failures)
        self.assertIsNotNone(crawl_page)
        self.assertEqual(2, checks)
        self.assertEqual(1, login_attempts)


if __name__ == "__main__":
    unittest.main()
