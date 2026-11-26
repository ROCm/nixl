# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of the site amd_site_fixture.py builds for the browser tests.

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

import amd_site_fixture as fixture  # noqa: E402

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "amd_site_fixture.py"


def snapshot(site: pathlib.Path) -> dict[str, bytes]:
    return {
        path.relative_to(site).as_posix(): path.read_bytes()
        for path in sorted(site.rglob("*"))
        if path.is_file()
    }


def load(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


class Fixture(unittest.TestCase):
    site: pathlib.Path
    scratch: tempfile.TemporaryDirectory[str]

    @classmethod
    def setUpClass(cls) -> None:
        cls.scratch = tempfile.TemporaryDirectory(prefix="amd-site-fixture-test-")
        cls.site = pathlib.Path(cls.scratch.name) / "site"
        fixture.build(cls.site)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.scratch.cleanup()

    def test_it_holds_what_the_app_reads(self) -> None:
        files = snapshot(self.site)
        for name in (
            "classic.html",
            "site.json",
            "data/nightly/index.json",
            "data/bench/v1/latest.json",
            "data/bench/v1/manifest.json",
            "bench/index.html",
        ):
            self.assertIn(name, files)
        # index.html is the app's: the fixture leaves it to the server.
        self.assertNotIn("index.html", files)

    def test_the_nights_cover_each_state_of_a_branch(self) -> None:
        index = load(self.site / "data" / "nightly" / "index.json")
        runs = index["runs"]
        self.assertEqual(len(runs), fixture.NIGHTS)
        self.assertEqual(runs[0]["stamp"], fixture.stamp(fixture.NIGHTS - 1))
        states = set()
        for run in runs:
            for branch in run["branches"]:
                states.add((branch["passed"], branch["supported"]))
        self.assertEqual(states, {(True, True), (False, True), (False, False)})
        first = [branch["branch"] for branch in runs[-1]["branches"]]
        last = [branch["branch"] for branch in runs[0]["branches"]]
        self.assertIn(fixture.RETIRED_BRANCH, first)
        self.assertNotIn(fixture.RETIRED_BRANCH, last)

    def test_the_failed_night_has_a_failed_check(self) -> None:
        path = self.site / "data" / "nightly" / fixture.stamp(fixture.FAILED_NIGHT)
        summary = load(path / "summary.json")
        (wheels,) = [
            b for b in summary["branches"] if b["branch"] == fixture.WHEELS_BRANCH
        ]
        self.assertFalse(wheels["passed"])
        statuses = [
            check["status"]
            for test in wheels["results"]["tests"]
            for check in test["checks"]
        ]
        self.assertEqual(statuses.count("FAIL"), 1)
        self.assertIn("SKIP", statuses)

    def test_the_bench_run_is_of_the_branch_the_nights_build(self) -> None:
        latest = load(self.site / "data" / "bench" / "v1" / "latest.json")
        self.assertEqual(list(latest["branches"]), [fixture.WHEELS_SLUG])

    def test_it_is_the_same_every_time(self) -> None:
        with tempfile.TemporaryDirectory(prefix="amd-site-fixture-test-") as scratch:
            again = pathlib.Path(scratch) / "site"
            fixture.build(again)
            self.assertEqual(snapshot(again), snapshot(self.site))

    def test_the_command_refuses_a_directory_that_is_not_empty(self) -> None:
        done = subprocess.run(
            [sys.executable, "-I", "-B", str(SCRIPT), "--site", str(self.site)],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 1)
        self.assertIn("is not empty", done.stderr)


if __name__ == "__main__":
    unittest.main()
