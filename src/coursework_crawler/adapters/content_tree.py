from __future__ import annotations

from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit

from ..models import CrawlResult, DeadlineRecord, HealthStatus, SourceConfig
from .base import Adapter
from .brightspace import (
    _is_authenticated, _within_active_term, content_record_from_api_item,
    enrich_brightspace_record, module_description_records,
)
from .schedules import BrightspaceScheduleAdapter


class ContentHTML(HTMLParser):
    """Extract content and resource labels without executing course HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.text: list[str] = []
        self.links: list[list[str]] = []
        self.anchor: list[str] | None = None
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        if tag == "a":
            self.anchor = ["", dict(attrs).get("href") or ""]

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        if tag == "a" and self.anchor:
            self.links.append(self.anchor)
            self.anchor = None

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.text.append(data)
            if self.anchor is not None:
                self.anchor[0] += data


def html_details(raw: str) -> tuple[str, list[list[str]]]:
    parser = ContentHTML()
    parser.feed(raw)
    return " ".join(" ".join(parser.text).split()), parser.links


class BrightspaceContentTreeAdapter(Adapter):
    """Read published modules recursively, including description-only resources."""

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        records: list[DeadlineRecord] = []
        visited: set[str] = set()
        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            page.locator(f'a[href="/d2l/home/{source.source_course_id}"]').first.wait_for(
                state="attached", timeout=15_000
            )
            if not _is_authenticated(page, source):
                raise PermissionError("Course session required")

            def walk(module_id: str, ancestors: tuple[str, ...] = ()) -> None:
                if module_id in visited:
                    raise ValueError("Cycle in content hierarchy")
                visited.add(module_id)
                module = BrightspaceScheduleAdapter._json(
                    page, source, f"content/modules/{module_id}"
                )
                module = {**module, "Type": 0}
                record = content_record_from_api_item(source, module, ancestors)
                description = module.get("Description") or {}
                body, links = html_details(description.get("Html") or description.get("Text") or "")
                record = enrich_brightspace_record(record, body, links)
                if record.due_at or record.timing_text:
                    records.append(record)
                records.extend(module_description_records(source, module, ancestors))
                children = BrightspaceScheduleAdapter._json(
                    page, source, f"content/modules/{module_id}/structure/"
                )
                if not isinstance(children, list):
                    raise ValueError("Expected a content structure list")
                lineage = (*ancestors, str(module["Title"]))
                for child in children:
                    if child.get("IsHidden"):
                        continue
                    if child.get("Type") == 0:
                        walk(str(child["Id"]), lineage)
                        continue
                    topic = content_record_from_api_item(source, child, lineage)
                    raw_url = urljoin(source.url, child.get("Url") or "")
                    # Reading HTML resources avoids launching quizzes, submissions,
                    # or external activities that can mutate course progress.
                    if urlsplit(raw_url).path.lower().endswith((".html", ".htm")) and (
                        urlsplit(raw_url).netloc == urlsplit(source.url).netloc
                    ):
                        response = page.context.request.get(raw_url, timeout=30_000)
                        if response.status in {401, 403}:
                            raise PermissionError("Content resource session required")
                        BrightspaceScheduleAdapter._raise_for_login(response)
                        if not response.ok:
                            raise ValueError(f"Content resource returned {response.status}")
                        text, links = html_details(response.text())
                        links = [[label, urljoin(raw_url, href)] for label, href in links]
                        topic = enrich_brightspace_record(topic, text, links)
                    if _within_active_term(topic, source):
                        records.append(topic)

            walk(str(source.options["module_id"]))
        except PermissionError:
            return CrawlResult(source, HealthStatus.LOGIN_REQUIRED,
                               message="Brightspace content login required; prior records retained.")
        except Exception as exc:
            if not _is_authenticated(page, source):
                return CrawlResult(source, HealthStatus.LOGIN_REQUIRED,
                                   message="Brightspace content login required; prior records retained.")
            return CrawlResult(source, HealthStatus.PARSER_FAILED,
                               message=f"Content traversal failed: {type(exc).__name__}: {exc}")
        return CrawlResult(
            source, HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO,
            tuple(records),
            f"Inspected {len(visited)} published modules; retained {len(records)} topics and obligations.",
            source.course_name,
        )
