# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of the root page amd_results_site.py renders, and of its bench link.

    python3 -m unittest discover -s .ci/scripts/tests -v
"""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile
import unittest
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_results_site as site  # noqa: E402

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "amd_results_site.py"
NOTE = '<p class="note">The transfer rates come from emulated devices'
SUMMARY: dict[str, Any] = {
    "schema": 1,
    "run_id": "44814378049",
    "stamp": "20261120T033018Z",
    "upstream_main_sha": "0123456789abcdef0123",
    "branches": [
        {
            "branch": "for-upstream/feat/rocm-wheels",
            "slug": "for-upstream-feat-rocm-wheels",
            "sha": "5a84605a50fe69a86689",
            "passed": True,
            "results": {"supported": True, "version": "1.6.0"},
        }
    ],
}


class RootPage(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.site = pathlib.Path(scratch.name) / "site"
        summary = pathlib.Path(scratch.name) / "nightly-summary.json"
        summary.write_text(json.dumps(SUMMARY), encoding="utf-8")
        site.add(self.site, summary)

    def page(self) -> str:
        site.render(self.site)
        return (self.site / "index.html").read_text(encoding="utf-8")

    def publish_bench(self) -> None:
        (self.site / "bench").mkdir()
        (self.site / "bench" / "index.html").write_text("<p>bench</p>\n")

    def test_the_template_holds_the_placeholder_before_the_note(self) -> None:
        self.assertEqual(site.PAGE.count("@@BENCH@@"), 1)
        self.assertEqual(site.PAGE.count("@@RUNS@@"), 1)
        self.assertIn("\n@@BENCH@@" + NOTE, site.PAGE)
        self.assertEqual(
            site.BENCH_LINK,
            '<p><a href="bench/index.html">nixlbench runs: failures, skips and'
            " blocked lanes of each bench run</a></p>\n",
        )

    def test_without_a_bench_page_the_root_page_does_not_change(self) -> None:
        page = self.page()
        rows = site.run_rows(site.scan(self.site))
        self.assertIn("2026-11-20 03:30 UTC", rows)
        self.assertEqual(
            page, site.PAGE.replace("@@BENCH@@", "").replace("@@RUNS@@", rows)
        )
        self.assertNotIn("bench/", page)
        self.assertNotIn("nixlbench", page)
        self.assertNotIn("@@", page)

    def test_a_bench_directory_without_its_page_is_not_linked(self) -> None:
        plain = self.page()
        (self.site / "bench" / "runs").mkdir(parents=True)
        (self.site / "bench" / "runs" / "x.html").write_text("x\n")
        self.assertEqual(self.page(), plain)
        (self.site / "bench" / "index.html").mkdir()
        self.assertEqual(self.page(), plain)

    def test_with_a_bench_page_the_link_is_before_the_note(self) -> None:
        plain = self.page()
        self.publish_bench()
        page = self.page()
        self.assertEqual(page.count(site.BENCH_LINK), 1)
        self.assertEqual(page.count("bench/index.html"), 1)
        self.assertIn("night by night.</p>\n" + site.BENCH_LINK + NOTE, page)
        self.assertEqual(page.replace(site.BENCH_LINK, ""), plain)
        self.assertNotIn("@@", page)

    def test_a_run_cannot_fill_the_placeholder(self) -> None:
        summary = dict(SUMMARY, stamp="20261121T033018Z")
        summary["branches"] = [
            dict(SUMMARY["branches"][0], branch="@@BENCH@@ @@RUNS@@", slug="x")
        ]
        path = self.site.parent / "second.json"
        path.write_text(json.dumps(summary), encoding="utf-8")
        site.add(self.site, path)
        self.publish_bench()
        page = self.page()
        self.assertEqual(page.count(site.BENCH_LINK), 1)
        self.assertIn("<b>@@BENCH@@ @@RUNS@@</b>", page)

    def test_the_command_still_runs_isolated(self) -> None:
        self.publish_bench()
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), "render", "--site", self.site],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        self.assertEqual(done.stdout, "index.html\n")
        page = (self.site / "index.html").read_text(encoding="utf-8")
        self.assertEqual(page.count(site.BENCH_LINK), 1)
        self.assertFalse((self.site / "index.html.tmp").exists())

    def test_with_the_app_published_the_page_moves_to_classic(self) -> None:
        plain = self.page()
        app = "<!doctype html><div id=root></div>\n"
        (self.site / "index.html").write_text(app, encoding="utf-8")
        (self.site / site.APP_MARKER).write_text("{}\n", encoding="utf-8")
        self.assertEqual(site.render(self.site), "classic.html")
        self.assertEqual((self.site / "index.html").read_text(encoding="utf-8"), app)
        self.assertEqual(
            (self.site / "classic.html").read_text(encoding="utf-8"), plain
        )
        (self.site / site.APP_MARKER).unlink()
        self.assertEqual(site.render(self.site), "index.html")


if __name__ == "__main__":
    unittest.main()
