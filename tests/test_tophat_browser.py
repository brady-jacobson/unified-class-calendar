"""Opt-in local DOM integration checks; no course accounts or network requests.

Run with RUN_BROWSER_TESTS=1 using an environment with Playwright and Chrome.
"""
from __future__ import annotations

import os
import unittest

from coursework_crawler.adapters.tophat import find_tree_item, read_rendered_file, scan_tree, wait_for_assigned_content


@unittest.skipUnless(os.environ.get("RUN_BROWSER_TESTS") == "1", "opt-in Chrome integration tests")
class TopHatBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        from playwright.sync_api import sync_playwright
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self) -> None:
        self.page = self.browser.new_page()

    def tearDown(self) -> None:
        self.page.close()

    def test_assigned_empty_state_waits_for_async_content(self) -> None:
        self.page.set_content('''<h1>Assigned for Grades</h1>
            <nav aria-label="Course Content">Loading</nav>
            <script>setTimeout(() => document.querySelector('nav').textContent =
                'No items are currently assigned for grades', 450);</script>''')
        self.assertFalse(wait_for_assigned_content(self.page))

    def test_file_scrolling_collects_all_pages_and_clean_links(self) -> None:
        self.page.set_content('''
            <main style="height:120px;overflow:auto"><div style="height:600px;position:relative"></div></main>
            <script>
            const main=document.querySelector('main'), content=main.firstElementChild;
            function render(){
                const number=Math.min(3,Math.floor(main.scrollTop/200)+1);
                content.innerHTML=`<div class="react-pdf__Page" data-page-number="${number}"
                    style="position:absolute;top:${(number-1)*200}px;height:200px">
                    <div class="textLayer">Page ${number}: required reading.</div>
                    <a href="https://example.invalid/resource?token=private">Open resource</a></div>`;
            }
            main.addEventListener('scroll',render);render();
            </script>''')
        links = {}
        pages = read_rendered_file(self.page, links)
        self.assertEqual([1, 2, 3], sorted(pages))
        self.assertIn("Page 3", pages[3])
        self.assertEqual((("Open resource", "https://example.invalid/resource"),), links[3])

    def test_virtual_tree_enumeration_and_offscreen_lookup(self) -> None:
        self.page.set_content('''
            <nav aria-label="Course Content" style="height:120px;overflow:auto">
                <div style="height:1000px;position:relative"></div></nav>
            <script>
            const nav=document.querySelector('nav'), content=nav.firstElementChild;
            function render(){
                const start=Math.floor(nav.scrollTop/100);
                content.innerHTML=Array.from({length:Math.min(3,10-start)},(_,i)=>{
                    const n=start+i;
                    return `<div role="treeitem" id="item-${n}" aria-label="Page ${n}, Slide"
                        aria-level="1" aria-setsize="10" style="position:absolute;top:${n*100}px;height:100px">
                        Page ${n}</div>`;
                }).join('');
            }
            nav.addEventListener('scroll',render);render();
            </script>''')
        rows = scan_tree(self.page)
        self.assertEqual([f"item-{n}" for n in range(10)], [row["id"] for row in rows])
        self.assertEqual("Page 0", find_tree_item(self.page, "item-0").inner_text())
