from __future__ import annotations

import hashlib
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from ..models import CalendarIssue, CrawlResult, DeadlineRecord, HealthStatus, SourceConfig
from ..links import clean_resource_url
from .base import Adapter
from .brightspace import announcement_record_from_entry


ASYNC_WORK = re.compile(r"\b(?:due(?!\s+to\b)|deadline|fill\s+out|submit\s+(?:this|the)\s+form|before\s+class)\b", re.I)
NO_ASSIGNED_ITEMS = re.compile(r"no (?:assigned |graded )?(?:items|content)|nothing.*assigned|0 items", re.I)


def record_from_content(source: SourceConfig, title: str, body: str, url: str,
                        *, transcript: bool = False, assigned: bool = False) -> DeadlineRecord | None:
    """Retain separately actionable work; ordinary live questions are excluded."""
    if not assigned and not ASYNC_WORK.search(body):
        return None
    if not assigned and re.search(r"\b(?:during class|in[ -]class)\b", body, re.I):
        if not re.search(r"\b(?:due(?!\s+to\b)|deadline|before\s+class)\b", body, re.I):
            return None
    record = announcement_record_from_entry(source, {
        "title": title, "body": body, "href": url,
        "item_id": hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:24],
    }, str(source.options.get("timezone", "America/Chicago")))
    if record is None:
        if not assigned:
            return None
        # Placement under Assigned for Grades establishes an obligation even
        # when its title has no homework keyword or published deadline.
        record = DeadlineRecord(
            course_id=source.course_id, source_id=source.id,
            source_platform=source.platform, source_course_id=source.source_course_id,
            source_item_id=hashlib.sha256(f"{url}|{title}".encode()).hexdigest()[:24],
            title=title, details_url=url,
        )
    # Top Hat labels transcripts as AI-generated. Keep their wording for review;
    # they cannot establish an exact deadline without authoritative corroboration.
    return replace(
        record, source_platform="tophat", component_kind="asynchronous obligation",
        due_at=None if transcript else record.due_at,
        timing_text=(record.timing_text or body[:1200]) if transcript or record.due_at is None else None,
        description=("Slide transcript; verify against the original slide. " if transcript else "") + body[:1600],
    )


def authenticated(page: Any, source: SourceConfig) -> bool:
    return (
        urlsplit(page.url).hostname == "app.tophat.com"
        and page.get_by_role("navigation", name="Course View", exact=True).count() == 1
        and urlsplit(page.url).path.startswith(f"/e/{source.source_course_id}/")
    )


def wait_for_assigned_content(page: Any) -> bool:
    """Wait for settled rows or an explicit empty state after the heading loads."""
    previous = None
    stable = 0
    for _ in range(60):
        page.wait_for_timeout(250)
        text = page.get_by_role("navigation", name="Course Content", exact=True).inner_text()
        ids = tuple(page.get_by_role("treeitem").evaluate_all("xs => xs.map(x => x.id)"))
        snapshot = (text, ids)
        stable = stable + 1 if snapshot == previous else 0
        previous = snapshot
        if stable >= 2:
            if NO_ASSIGNED_ITEMS.search(text):
                return False
            if ids:
                return True
    raise ValueError("Assigned-for-Grades exposed neither settled rows nor a verified empty state")


def read_rendered_file(page: Any, links: dict[int, tuple[tuple[str, str], ...]] | None = None) -> dict[int, str]:
    """Read every page of the virtualized, read-only PDF viewer.

    Reaching the scroll boundary alone is insufficient: page numbers must also
    form an unbroken sequence, and every page must expose a text layer.
    """
    main = page.get_by_role("main")
    main.locator(".react-pdf__Page[data-page-number]").first.wait_for(timeout=15_000)
    main.evaluate("x => { x.scrollTop = 0; }")
    pages: dict[int, str] = {}
    stable_bottom = 0
    for _ in range(1000):
        page.wait_for_timeout(250)
        visible = main.locator(".react-pdf__Page[data-page-number]").evaluate_all("""xs => xs.map(x => ({
            number: Number(x.dataset.pageNumber),
            text: x.querySelector('.textLayer')?.innerText || '',
            links: [...x.querySelectorAll('a[href]')].map(a => ({
                href: a.href, label: a.getAttribute('title') || a.textContent || 'Open linked resource'
            }))
        }))""")
        for entry in visible:
            number = int(entry["number"])
            if entry["text"].strip() or number not in pages:
                pages[number] = entry["text"].strip()
            if links is not None:
                clean_links = dict((url, label) for label, url in links.get(number, ()))
                for link in entry["links"]:
                    url = clean_resource_url(link["href"])
                    if url:
                        label = link["label"].strip()
                        if not label or re.search(r"https?://", label):
                            label = "Open linked resource"
                        clean_links[url] = label
                links[number] = tuple((label, url) for url, label in clean_links.items())
        at_bottom = main.evaluate("""x => {
            const bottom = x.scrollTop + x.clientHeight >= x.scrollHeight - 2;
            if (!bottom) x.scrollTop += Math.max(100, x.clientHeight * 0.7);
            return bottom;
        }""")
        stable_bottom = stable_bottom + 1 if at_bottom else 0
        if stable_bottom >= 3:
            validate_file_pages(pages)
            return pages
    raise ValueError("File viewer did not reach a stable end")


def validate_file_pages(pages: dict[int, str]) -> None:
    if not pages or set(pages) != set(range(1, max(pages) + 1)):
        raise ValueError("File viewer omitted one or more pages")
    if any(not text.strip() for text in pages.values()):
        raise ValueError("File contains pages without readable text; visual review is required")


def tree_scroll_container(page: Any) -> Any:
    return page.get_by_role("treeitem").first.evaluate_handle("""x => {
        for (let p = x.parentElement; p; p = p.parentElement) {
            if (p.scrollHeight > p.clientHeight &&
                /auto|scroll/.test(getComputedStyle(p).overflowY)) return p;
        }
        return x.parentElement;
    }""")


def scan_tree(page: Any) -> list[dict[str, Any]]:
    """Collect rendered metadata while scrolling the virtualized content tree."""
    container = tree_scroll_container(page)
    container.evaluate("x => { x.scrollTop = 0; }")
    rows: dict[str, dict[str, Any]] = {}
    for _ in range(500):
        page.wait_for_timeout(150)
        visible = page.get_by_role("treeitem").evaluate_all("""xs => xs.map(x => ({
            id: x.id, label: x.getAttribute('aria-label') || '',
            text: x.innerText || '',
            level: Number(x.getAttribute('aria-level')),
            total: Number(x.getAttribute('aria-setsize'))
        }))""")
        for row in visible:
            rows[row["id"]] = row
        bottom = container.evaluate("""x => {
            const bottom = x.scrollTop + x.clientHeight >= x.scrollHeight - 2;
            if (!bottom) x.scrollTop += Math.max(100, x.clientHeight * 0.7);
            return bottom;
        }""")
        if bottom:
            if not rows or any(row["total"] != len(rows) for row in rows.values()):
                raise ValueError("Content tree enumeration differs from its advertised total")
            return list(rows.values())
    raise ValueError("Content tree did not reach its end")


def find_tree_item(page: Any, item_id: str) -> Any:
    item = page.locator(f'[role="treeitem"][id="{item_id}"]')
    if item.count():
        item.scroll_into_view_if_needed()
        return item
    container = tree_scroll_container(page)
    # Sequential traversal normally needs the next viewport, not a fresh scan
    # from the top for every off-screen slide. Wrap once for earlier items.
    for reset in (False, True):
        if reset:
            container.evaluate("x => { x.scrollTop = 0; }")
        for _ in range(500):
            page.wait_for_timeout(100)
            if item.count():
                item.scroll_into_view_if_needed()
                return item
            bottom = container.evaluate("""x => {
                const bottom = x.scrollTop + x.clientHeight >= x.scrollHeight - 2;
                if (!bottom) x.scrollTop += Math.max(100, x.clientHeight * 0.7);
                return bottom;
            }""")
            if bottom:
                break
    raise ValueError("Previously enumerated content item is no longer visible")


def folder_children(rows: list[dict[str, Any]], parent: dict[str, Any]) -> list[dict[str, Any]]:
    index = next(i for i, row in enumerate(rows) if row["id"] == parent["id"])
    children = []
    for row in rows[index + 1:]:
        if row["level"] <= parent["level"]:
            break
        if row["level"] == parent["level"] + 1:
            children.append(row)
    expected = re.search(r", (?:Folder|Presentation), (\d+) items?", parent["label"])
    # Folder labels count contained resources recursively, not child folders.
    total = 0
    for child in children:
        nested = re.search(r", (?:Folder|Presentation), (\d+) items?", child["label"])
        total += int(nested.group(1)) if nested else 1
    if not expected or total != int(expected.group(1)):
        raise ValueError("Folder child count does not match its advertised count")
    return children


class TopHatAdapter(Adapter):
    """Monitor assigned metadata and announcement slides without entering questions."""

    def crawl(self, page: Any, source: SourceConfig) -> CrawlResult:
        records: list[DeadlineRecord] = []
        issues: list[CalendarIssue] = []
        inspected = 0
        file_page_count = 0

        def issue(key: str, title: str, message: str) -> None:
            issues.append(CalendarIssue(source.course_id, source.id, key, title,
                page.url, message, "Content could not be verified", "warning"))

        try:
            page.goto(source.url, wait_until="domcontentloaded", timeout=30_000)
            try:
                page.get_by_role("navigation", name="Course View", exact=True).wait_for(timeout=20_000)
            except Exception:
                if not authenticated(page, source):
                    return CrawlResult(source, HealthStatus.LOGIN_REQUIRED,
                                       message="Top Hat sign-in required; prior records retained.")
                raise
            if not authenticated(page, source):
                return CrawlResult(source, HealthStatus.LOGIN_REQUIRED,
                                   message="Top Hat sign-in required; prior records retained.")
            page.get_by_role("button", name="Assigned for Grades", exact=True).click()
            page.get_by_role("heading", name="Assigned for Grades", exact=True).wait_for(timeout=15_000)
            assigned = scan_tree(page) if wait_for_assigned_content(page) else []
            for row in assigned:
                if ", Folder," in row["label"] or ", Presentation," in row["label"]:
                    issue(row["id"], row["label"], "Assigned container requires review of its component assignments.")
                    continue
                text = row["text"] or row["label"]
                record = record_from_content(source, text.splitlines()[0], text, page.url, assigned=True)
                if record:
                    records.append(replace(record, source_item_id=row["id"]))

            page.get_by_role("button", name="All Content", exact=True).click()
            page.get_by_role("heading", name="All Content", exact=True).wait_for(timeout=15_000)
            page.get_by_role("treeitem").first.wait_for(timeout=15_000)
            for _ in range(100):
                expanded = page.get_by_role("treeitem").filter(
                    has=page.get_by_role("button", name=re.compile(r"^Collapse "))
                )
                if not expanded.count():
                    break
                expanded.first.get_by_role("button", name=re.compile(r"^Collapse ")).click()
            roots = [row for row in scan_tree(page) if row["level"] == 1]
            if not roots:
                raise ValueError("Content tree exposed no root items")
            pending = list(roots)
            visited: set[str] = set()
            while pending:
                root = pending.pop(0)
                if root["id"] in visited:
                    raise ValueError("Content tree repeated an item identity")
                visited.add(root["id"])
                if ", Folder," in root["label"]:
                    item = find_tree_item(page, root["id"])
                    expand = item.get_by_role("button", name=re.compile(r"^Expand "))
                    if expand.count():
                        expand.click()
                    else:
                        item.click()
                    page.wait_for_timeout(500)
                    pending[0:0] = folder_children(scan_tree(page), root)
                    continue
                if root["label"].endswith(", File"):
                    find_tree_item(page, root["id"]).click()
                    try:
                        file_links: dict[int, tuple[tuple[str, str], ...]] = {}
                        file_pages = read_rendered_file(page, file_links)
                        file_page_count += len(file_pages)
                    except Exception as exc:
                        if not authenticated(page, source):
                            raise
                        issue(root["id"], root["label"], f"File review incomplete: {type(exc).__name__}: {exc}")
                        continue
                    for number, body in file_pages.items():
                        title = f'{root["label"][:-6]} · page {number}'
                        record = record_from_content(source, title, body, page.url)
                        if record:
                            records.append(replace(record, related_links=file_links.get(number, ()), source_item_id=hashlib.sha256(
                                f'{root["id"]}|page|{number}'.encode()
                            ).hexdigest()[:24]))
                    continue
                if root["label"].endswith((", Question", ", Discussion")):
                    # Inspect only the already-visible metadata. Never enter a
                    # live question, discussion, attempt, or response form.
                    record = record_from_content(source, root["label"], root["label"], source.url)
                    if record:
                        records.append(replace(record, source_item_id=root["id"]))
                    continue
                if ", Presentation," in root["label"]:
                    find_tree_item(page, root["id"]).click()
                    page.wait_for_timeout(500)
                    pending[0:0] = folder_children(scan_tree(page), root)
                    continue
                if root["label"].endswith(", Slide"):
                    find_tree_item(page, root["id"]).click()
                    title = root["label"][:-7]
                    try:
                        transcript_control = page.get_by_role("button", name=re.compile(
                            r"^(?:Show Slide Transcripts|Hide Slide Transcripts|Slide transcript unavailable, try again soon)$"
                        ))
                        transcript_control.first.wait_for(timeout=10_000)
                        if page.get_by_role("button", name="Slide transcript unavailable, try again soon", exact=True).count():
                            raise ValueError("Top Hat explicitly reports that this slide transcript is unavailable")
                        show = page.get_by_role("button", name="Show Slide Transcripts", exact=True)
                        main = page.get_by_role("main")
                        if not main.get_by_role("heading", name="Transcript", exact=True).count():
                            show.wait_for(timeout=10_000)
                            show.click()
                        main.get_by_role("heading", name="Transcript", exact=True).wait_for(timeout=10_000)
                        page.wait_for_timeout(1000)
                        body = main.inner_text()
                        if "AI-generated transcript" not in body or len(body.strip()) < 80:
                            raise ValueError("No usable transcript text was exposed")
                    except Exception as exc:
                        if not authenticated(page, source):
                            raise
                        issue(root["id"], title, f"Slide review incomplete: {type(exc).__name__}: {exc}")
                        continue
                    record = record_from_content(source, title, body, page.url, transcript=True)
                    if record:
                        records.append(replace(record, source_item_id=root["id"]))
                    inspected += 1
                    continue
                issue(root["id"], root["label"], "This resource has no supported read-only view.")
            return CrawlResult(
                source, HealthStatus.PARTIAL if issues else (HealthStatus.SUCCESS if records else HealthStatus.VERIFIED_ZERO),
                tuple(records),
                f"Checked {len(assigned)} assigned rows, {inspected} slide transcripts, and "
                f"{file_page_count} file pages across {len(visited)} resources; "
                f"retained {len(records)} asynchronous obligations for review. "
                f"{len(issues)} resources or slides require manual review.",
                source.course_name,
                issues=tuple(issues),
            )
        except Exception as exc:
            if not authenticated(page, source):
                return CrawlResult(source, HealthStatus.LOGIN_REQUIRED,
                                   message="Top Hat sign-in required; prior records retained.")
            issue("incomplete-enumeration", "Top Hat scan incomplete",
                  f"Traversal stopped after {inspected} slides: {type(exc).__name__}: {exc}")
            return CrawlResult(source, HealthStatus.PARTIAL if records else HealthStatus.PARSER_FAILED,
                               tuple(records), f"Top Hat monitoring failed: {type(exc).__name__}: {exc}",
                               issues=tuple(issues))
