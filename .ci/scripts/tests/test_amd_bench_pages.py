# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of amd_bench_pages.py.

    python3 -m unittest discover -s .ci/scripts/tests -v

fixtures/bench_pages holds the three documents of the first published bench
run (ROCm/nixl run 44820022442): index.json with its 20 failing cases, three of
its 24 skips, all 16 blocked groups and three of its 37 ratios; manifest.json
and latest.json with that one shard. The totals are those of the whole run.
"""

from __future__ import annotations

import copy
import html
import html.parser
import json
import pathlib
import re
import sys
import unittest
from typing import Any, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_pages as pages  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "bench_pages"
SHARD = "20261120T204631Z-for-upstream-feat-rocm-wheels-44820022442.a1"
SLUG = "for-upstream-feat-rocm-wheels"
VERDICT = "20 of 307 cases failed or timed out · 24 skipped · 82 blocked · 181 passed"
VOID = frozenset(
    "area base br col embed hr img input link meta source track wbr".split()
)
HOSTILE = '"\'><script>alert(1)</script><a href="javascript:alert(1)" onclick="x">'
BAD_LINKS = (
    "javascript:alert(1)",
    "https://evil.example/x",
    "https://github.com.evil.example/x",
    "//evil.example/x",
    "http://github.com/ROCm/nixl",
    "https://github.com/a b",
)
BAD_PATHS = ("../../../etc/passwd", "/etc/passwd", "night/../../x", "a b", "a?b=c")
SAFE_HREF_RE = re.compile(r"[A-Za-z0-9._+/-]*(#[A-Za-z0-9_-]+)?")


def html_escape(text: str) -> str:
    return html.escape(text, quote=True)


def load(name: str) -> dict[str, Any]:
    document = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


class Audit(html.parser.HTMLParser):
    """What a page is made of, and whether its tags balance."""

    def __init__(self, page: str) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.problems: list[str] = []
        self.tags: list[tuple[str, dict[str, Optional[str]]]] = []
        self.ids: list[str] = []
        self.hrefs: list[str] = []
        self.text: list[str] = []
        self.marks: list[tuple[str, str]] = []
        self._mark: Optional[list[str]] = None
        self.feed(page)
        self.close()
        if self.stack:
            self.problems.append(f"left open: {self.stack}")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, Optional[str]]]) -> None:
        values = dict(attrs)
        if len(values) != len(attrs):
            self.problems.append(f"<{tag}> repeats an attribute")
        self.tags.append((tag, values))
        if tag not in VOID:
            self.stack.append(tag)
        if values.get("id") is not None:
            self.ids.append(str(values["id"]))
        if "href" in values:
            self.hrefs.append(str(values["href"]))
        if tag in ("a", "span") and str(values.get("class", "")).startswith("st "):
            self._mark = [str(values["class"])[3:]]

    def handle_endtag(self, tag: str) -> None:
        if tag in VOID:
            self.problems.append(f"</{tag}> closes a void element")
        elif not self.stack or self.stack[-1] != tag:
            self.problems.append(f"</{tag}> closes {self.stack[-1:]}")
        else:
            self.stack.pop()
        if self._mark is not None and tag in ("a", "span"):
            self.marks.append((self._mark[0], "".join(self._mark[1:])))
            self._mark = None

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._mark is not None:
            self._mark.append(data)

    def words(self) -> str:
        return " ".join(" ".join(self.text).split())

    def headings(self, tag: str = "h2") -> list[Optional[str]]:
        return [values.get("id") for name, values in self.tags if name == tag]


def poison(value: Any, keep: frozenset[str] = frozenset(), key: str = "") -> Any:
    """value with every string replaced by markup, except under the keys kept."""
    if isinstance(value, dict):
        return {name: poison(item, keep, name) for name, item in value.items()}
    if isinstance(value, list):
        return [poison(item, keep, key) for item in value]
    if isinstance(value, str) and key not in keep:
        return HOSTILE
    return value


def lane(status: str, n: list[int], **more: Any) -> dict[str, Any]:
    return {"status": status, "n": n, **more}


def shard(stamp: str, run: str, lanes: dict[str, Any], **more: Any) -> dict[str, Any]:
    """A manifest entry with what the landing page reads."""
    slug = more.pop("slug", "main")
    counts = [sum(entry["n"][at] for entry in lanes.values()) for at in range(7)]
    entry = {
        "id": f"{stamp}-{slug}-{run}.a1",
        "stamp": stamp,
        "slug": slug,
        "branch": slug,
        "sha": "0123456789abcdef",
        "tier": "nightly",
        "reason": "nightly",
        "n": counts,
        "lanes": lanes,
        "superseded_by": {},
    }
    entry.update(more)
    return entry


def branch(of: dict[str, Any], **more: Any) -> dict[str, Any]:
    """A latest.json branch entry for the night of one manifest entry."""
    entry = {
        "branch": of["branch"],
        "tier": of["tier"],
        "stamp": of["stamp"],
        "sha": of["sha"],
        "headline": "nightly",
        "stale": False,
        "newer": None,
        "shards": [of["id"]],
        "totals": dict(zip(pages.STATUSES, of["n"])),
        "tests": {"pass": 0, "fail": 0, "skip": 0, "timeout": 0},
        "lanes": [
            {
                "lane": key.partition("/")[0],
                "rocm_version": key.partition("/")[2],
                "shard": of["id"],
                **value,
            }
            for key, value in sorted(of["lanes"].items())
        ],
        "attention": [],
        "attention_total": 0,
        "tests_attention": [],
        "tests_attention_total": 0,
        "ratios": [],
    }
    entry.update(more)
    return entry


def manifest_of(*shards: dict[str, Any]) -> dict[str, Any]:
    return {"schema": 1, "links": {"docs": None}, "shards": list(shards)}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.index = load("index")
        self.manifest = load("manifest")
        self.latest = load("latest")

    def audit(self, page: str) -> Audit:
        """page checked for what every page must hold, and parsed."""
        self.assertTrue(page.endswith("</html>\n"))
        audit = Audit(page)
        self.assertEqual(audit.problems, [])
        self.assertNotIn("<script", page.lower())
        head = [name for name, _ in audit.tags][:3]
        self.assertEqual(head, ["html", "head", "meta"])
        self.assertEqual(audit.tags[2][1], {"charset": "utf-8"})
        self.assertTrue(page[: page.index('<meta charset="utf-8">')].isascii())
        for name, values in audit.tags:
            self.assertNotIn(name, ("script", "iframe", "object", "embed", "img"))
            self.assertNotIn(name, ("link", "base", "form"))
            for attribute in values:
                self.assertFalse(attribute.startswith("on"), (name, attribute))
                self.assertNotIn(attribute, ("src", "style", "srcset", "action"))
        for href in audit.hrefs:
            if not href.startswith("https://github.com/"):
                self.assertTrue(SAFE_HREF_RE.fullmatch(href), href)
                inside = href.split("#")[0].split("/")
                while inside[:1] == [".."]:
                    del inside[0]
                self.assertNotIn("..", inside, href)
                self.assertNotIn("", inside[:-1], href)
        self.assertEqual(len(audit.ids), len(set(audit.ids)))
        policy = [v for n, v in audit.tags if v.get("http-equiv")]
        self.assertEqual(len(policy), 1)
        self.assertIn("default-src 'none'", str(policy[0]["content"]))
        return audit


class Helpers(Base):
    def test_the_failure_groups_of_the_first_run(self) -> None:
        groups = pages.failure_groups(self.index["attention"])
        self.assertEqual([group["count"] for group in groups], [10, 3, 2, 2, 2, 1])
        first = groups[0]
        self.assertEqual(
            (first["status"], first["reason"], first["backend"], first["via"]),
            ("fail", "consistency", "UCX", "rc"),
        )
        self.assertEqual(
            first["memory"],
            [["DRAM", "VRAM", 4], ["VRAM", "DRAM", 4], ["VRAM", "VRAM", 2]],
        )
        self.assertEqual(first["lanes"], [["vm2-net", 6], ["vm1-loop", 4]])
        self.assertEqual(first["rocm"], ["10.1.0"])
        self.assertEqual(first["rows"], [5, 10])
        self.assertEqual(first["duration"], [5.098, 6.138])
        self.assertTrue(first["excerpt"].startswith("Consistency check failed"))
        self.assertEqual(
            [(g["status"], g["reason"], g["backend"], g["via"]) for g in groups[1:]],
            [
                ("timeout", "timeout", "UCX", "ud"),
                ("fail", "consistency", "AIS", "compat-allowed"),
                ("fail", "consistency", "AIS", "fast-required"),
                ("fail", "exit-nonzero", "AIS", "fast-required"),
                ("timeout", "timeout", "UCX", "tcp"),
            ],
        )
        self.assertEqual(groups[5]["rows"], [7, 11])
        self.assertEqual(
            set(groups[0]),
            {
                "status",
                "reason",
                "backend",
                "via",
                "count",
                "memory",
                "lanes",
                "rocm",
                "rows",
                "duration",
                "excerpt",
            },
        )

    def test_failure_groups_leave_out_skips_and_take_nulls(self) -> None:
        entry = {
            "status": "crash",
            "reason": "signal",
            "backend": "UCX",
            "engine": "",
            "transport_req": "rc",
            "init_mem": "DRAM",
            "tgt_mem": "DRAM",
            "lane": "a",
            "rocm_version": "1",
            "rows_seen": None,
            "rows_expected": 4,
            "duration_s": None,
            "error_excerpt": "first",
        }
        groups = pages.failure_groups(
            [
                {**entry, "status": "skip"},
                {**entry, "status": "fail", "error_excerpt": "a fail"},
                entry,
                {**entry, "error_excerpt": "second", "lane": "b", "duration_s": 2},
                {**entry, "status": "not-run", "reason": "no record"},
                {**entry, "status": "timeout", "engine": None},
            ]
        )
        # The count first, then the severity: crash, fail, timeout, not-run.
        self.assertEqual(
            [(group["count"], group["status"]) for group in groups],
            [(2, "crash"), (1, "fail"), (1, "timeout"), (1, "not-run")],
        )
        self.assertEqual(groups[0]["rows"], [0, 8])
        self.assertEqual(groups[0]["duration"], [2.0, 2.0])
        self.assertEqual(groups[0]["excerpt"], "first")
        self.assertEqual(groups[0]["lanes"], [["a", 1], ["b", 1]])
        self.assertEqual(groups[1]["duration"], [None, None])
        self.assertEqual(groups[2]["via"], "rc")
        self.assertEqual(pages.failure_groups([]), [])

    def test_the_blocked_groups_count_distinct_case_keys(self) -> None:
        blocked = self.index["blocked"]
        self.assertEqual(len(blocked), 16)
        self.assertEqual(sum(entry["cases"] for entry in blocked), 82)
        groups = pages.blocked_groups(blocked)
        self.assertEqual(len(groups), 10)
        self.assertEqual(sum(group["cases"] for group in groups), 68)
        container = [group for group in groups if group["lane"] == "bench-container"]
        self.assertEqual(
            [(group["blocked_by"], group["cases"]) for group in container],
            [(["E8"], 2), (["nixlbench-redis-read-sweep"], 1), (["ucx-shm-ep"], 4)],
        )
        for group in container:
            self.assertEqual(group["rocm"], ["10.1.0", "10.2.0", "7.14.1"])
            self.assertEqual(group["case_keys"], sorted(set(group["case_keys"])))
            self.assertEqual(len(group["case_keys"]), group["cases"])
        self.assertEqual(
            [group["lane"] for group in groups], sorted(g["lane"] for g in groups)
        )
        self.assertEqual(
            set(groups[0]), {"lane", "blocked_by", "cases", "case_keys", "rocm"}
        )

    def test_exit_text(self) -> None:
        self.assertEqual(pages.exit_text(None), "-")
        self.assertEqual(pages.exit_text(0), "0")
        self.assertEqual(pages.exit_text(1), "1")
        self.assertEqual(pages.exit_text(124), "124 (timed out)")
        self.assertEqual(pages.exit_text(134), "134 (signal 6 ABRT)")
        self.assertEqual(pages.exit_text(137), "137 (signal 9 KILL)")
        self.assertEqual(pages.exit_text(139), "139 (signal 11 SEGV)")
        self.assertEqual(pages.exit_text(133), "133 (signal 5)")
        self.assertEqual(pages.exit_text(255), "255")

    def test_run_page_path_takes_a_shard_id_only(self) -> None:
        self.assertEqual(pages.run_page_path(SHARD), f"bench/runs/{SHARD}.html")
        self.assertEqual(pages.LANDING, "bench/index.html")
        self.assertEqual(pages.DATA_ROOT, "data/bench/v1")
        self.assertEqual(
            pages.SHARD_ID_RE.fullmatch(SHARD).groups(),  # type: ignore[union-attr]
            ("20261120T204631Z", SLUG, "44820022442", "1"),
        )
        wide = "２０２６１１２０T２０４６３１Z-main-1.a1"
        for bad in (
            "",
            "../x",
            SHARD + ".html",
            "20261120T204631Z-..-1.a1",
            "20261120T204631Z-a/b-1.a1",
            "20261120T204631Z-main-1.a12345",
            "20261120T204631Z-main-x.a1",
            wide,
            SHARD + "\n",
        ):
            with self.assertRaises(pages.PageError, msg=bad):
                pages.run_page_path(bad)
        with self.assertRaises(pages.PageError):
            pages.run_page_path(None)  # type: ignore[arg-type]
        self.assertTrue(issubclass(pages.PageError, ValueError))

    def test_the_status_lists(self) -> None:
        self.assertEqual(
            pages.STATUSES,
            ("pass", "fail", "timeout", "crash", "skip", "blocked", "not-run"),
        )
        self.assertEqual(pages.FAILING, ("crash", "fail", "timeout", "not-run"))
        self.assertEqual(self.manifest["statuses"], list(pages.STATUSES))


class Fixture(Base):
    def test_the_index_holds_the_failures_of_the_first_run(self) -> None:
        failing = [e for e in self.index["attention"] if e["status"] != "skip"]
        self.assertEqual(len(failing), 20)
        found: dict[tuple[Any, ...], int] = {}
        for entry in failing:
            self.assertEqual(
                (entry["variant"], entry["round"], entry["required"]),
                ("head", 0, False),
            )
            key = (
                entry["status"],
                entry["error_class"],
                entry["backend"],
                entry["engine"] or entry["transport_req"],
                entry["lane"],
            )
            found[key] = found.get(key, 0) + 1
        self.assertEqual(
            found,
            {
                ("fail", "consistency", "AIS", "compat-allowed", "vm1-fabric"): 2,
                ("fail", "consistency", "AIS", "fast-required", "vm1-fabric"): 2,
                ("fail", "exit-nonzero", "AIS", "fast-required", "vm1-local"): 2,
                ("fail", "consistency", "UCX", "rc", "vm1-loop"): 4,
                ("fail", "consistency", "UCX", "rc", "vm2-net"): 6,
                ("timeout", "timeout", "UCX", "ud", "vm1-loop"): 1,
                ("timeout", "timeout", "UCX", "ud", "vm2-net"): 2,
                ("timeout", "timeout", "UCX", "tcp", "vm2-net"): 1,
            },
        )
        exits = {
            (e["backend"], e["op"], e["status"], e["exit_init"], e["exit_tgt"])
            for e in failing
        }
        self.assertEqual(
            exits,
            {
                ("AIS", "WRITE", "fail", 1, None),
                ("AIS", "READ", "fail", 1, None),
                ("UCX", "WRITE", "fail", 134, 1),
                ("UCX", "READ", "fail", 1, 134),
                ("UCX", "WRITE", "timeout", 124, 124),
            },
        )
        for entry in failing:
            self.assertEqual(entry["engine"] == "", entry["backend"] == "UCX")
            self.assertEqual(
                entry["transport_req"] == "none", entry["backend"] == "AIS"
            )
            self.assertLessEqual(len(entry["error_excerpt"]), 500)
            self.assertLessEqual(len(entry["cmdline"]), 601)
        skips = [e for e in self.index["attention"] if e["status"] == "skip"]
        self.assertEqual(
            [(e["reason"], e["error_excerpt"], e["rocm_version"]) for e in skips],
            [
                ("env", "needs s3", version)
                for version in ("10.1.0", "10.2.0", "7.14.1")
            ],
        )

    def test_the_documents_agree(self) -> None:
        totals = [self.index["totals"][status] for status in pages.STATUSES]
        self.assertEqual(totals, [181, 16, 4, 0, 24, 82, 0])
        lanes = self.index["lanes"]
        self.assertEqual(len(lanes), 10)
        self.assertEqual([sum(e["n"][at] for e in lanes) for at in range(7)], totals)
        shard = self.manifest["shards"][0]
        self.assertEqual(shard["id"], SHARD)
        self.assertEqual(shard["id"], self.index["shard"]["id"])
        self.assertEqual(shard["n"], totals)
        self.assertEqual(
            sorted(shard["lanes"]),
            sorted(f"{e['lane']}/{e['rocm_version']}" for e in lanes),
        )
        branch = self.latest["branches"][SLUG]
        self.assertEqual(branch["shards"], [SHARD])
        self.assertEqual(branch["totals"], self.index["totals"])
        self.assertEqual(
            (branch["headline"], branch["attention_total"]), ("manual", 20)
        )
        self.assertEqual(
            [e["case_key"] for e in branch["attention"]],
            [e["case_key"] for e in self.index["attention"] if e["status"] != "skip"],
        )
        self.assertEqual(
            set(branch["attention"][0]),
            {
                "shard",
                "lane",
                "rocm_version",
                "case_key",
                "variant",
                "round",
                "status",
                "reason",
                "required",
                "error_excerpt",
            },
        )
        self.assertEqual(sum(f["bytes"] for f in shard["files"]), shard["bytes"])


class RunPage(Base):
    def render(self) -> tuple[str, Audit]:
        page = pages.render_run(self.index)
        return page, self.audit(page)

    def test_the_first_run(self) -> None:
        page, audit = self.render()
        words = audit.words()
        self.assertIn(VERDICT, words)
        self.assertIn("Failed, timed out, crashed or not run (20)", words)
        self.assertIn("By cause (6 groups)", words)
        self.assertIn("Blocked: what each lane waits for (68 cases)", words)
        self.assertIn("82 records over 3 ROCm versions.", words)
        self.assertIn("DRAM→VRAM ×4, VRAM→DRAM ×4, VRAM→VRAM ×2", words)
        self.assertIn("vm2-net ×6, vm1-loop ×4", words)
        self.assertIn("134 (signal 6 ABRT) / 1", words)
        self.assertIn("nixlbench: SG mode — set NEURON_RT_NUM_CORES=1", words)
        self.assertIn("nixlbench run of 2026-11-20 20:46 UTC", words)
        self.assertIn("branch for-upstream/feat/rocm-wheels", words)
        for entry in self.index["attention"]:
            self.assertIn(f"<code>{entry['case_key']}</code>", page)
        self.assertLess(len(page.encode("utf-8")), 300_000)
        self.assertEqual(page, pages.render_run(copy.deepcopy(self.index)))

    def test_the_failures_come_before_every_other_section(self) -> None:
        _, audit = self.render()
        self.assertEqual(
            audit.headings(), ["failures", "skipped", "lanes", "coverage", "ratios"]
        )
        self.assertEqual(audit.headings("h3"), ["groups", "cases"])
        self.assertEqual(audit.headings("h1"), [None])
        banner = [v for n, v in audit.tags if "verdict" in str(v.get("class"))]
        self.assertEqual(banner, [{"class": "verdict fail"}])

    def test_failed_probes_come_first(self) -> None:
        self.index["tests_attention"] = [
            {
                "lane": "vm2-net",
                "rocm_version": "10.1.0",
                "run_uid": "f0671ab182f823861917",
                "suite": "probe",
                "test_id": "kv-generic-node/start",
                "status": "fail",
                "error_excerpt": "no such device | second line",
                "duration_s": 0.3,
            }
        ]
        self.index["tests"]["fail"] = 1
        page, audit = self.render()
        self.assertEqual(audit.headings("h3"), ["tests", "groups", "cases"])
        self.assertLess(page.index('id="tests"'), page.index('id="groups"'))
        words = audit.words()
        self.assertIn("Failed probes and tests (1)", words)
        self.assertIn("skipped as device-suspect", words)
        self.assertIn(VERDICT + " · 1 probe or test failed", words)
        self.assertIn("<pre>no such device\nsecond line</pre>", page)

    def test_failed_probes_alone_still_fail_the_run(self) -> None:
        self.index["attention"] = []
        self.index["totals"].update({"fail": 0, "timeout": 0})
        self.index["tests"]["timeout"] = 2
        self.index["tests_attention"] = [
            {
                "lane": "vm2-net",
                "rocm_version": "10.1.0",
                "run_uid": "x",
                "suite": "probe",
                "test_id": "a",
                "status": "timeout",
                "error_excerpt": "",
                "duration_s": None,
            }
        ]
        page, audit = self.render()
        self.assertIn('class="verdict fail"', page)
        self.assertIn("0 of 287 cases failed", audit.words())
        self.assertIn("2 probes or tests failed", audit.words())
        self.assertIn("No case failed.", audit.words())
        self.assertEqual(audit.headings("h3"), ["tests"])

    def test_no_failure(self) -> None:
        self.index["attention"] = [
            entry for entry in self.index["attention"] if entry["status"] == "skip"
        ]
        self.index["totals"].update({"fail": 0, "timeout": 0, "pass": 201})
        page, audit = self.render()
        self.assertIn('<p class="verdict ok">', page)
        self.assertNotIn("verdict fail", page)
        words = audit.words()
        self.assertIn("0 of 307 cases failed · 24 skipped", words)
        self.assertIn("Failed, timed out, crashed or not run (0)", words)
        self.assertIn("No case failed.", words)
        self.assertEqual(audit.headings("h3"), [])
        self.assertEqual(audit.headings()[0], "failures")

    def test_empty_run(self) -> None:
        for name in ("attention", "tests_attention", "blocked", "ratios", "lanes"):
            self.index[name] = []
        self.index["totals"] = dict.fromkeys(pages.STATUSES, 0)
        self.index["collect"] = None
        _, audit = self.render()
        words = audit.words()
        self.assertIn("0 of 0 cases failed", words)
        self.assertIn("No case was skipped.", words)
        self.assertIn("No case is blocked.", words)
        self.assertIn("The matrix declares no ratio", words)
        self.assertIn("Collector summary not archived.", words)

    def test_a_timeout_row_leads_with_the_time(self) -> None:
        page, _ = self.render()
        at = page.index('id="case-10-1-0-vm2-net-ucx-tcp-VV-WRITE-N-head-0"')
        row = page[at : page.index('<tr class="main"', at)]
        detail = row[row.index('<tr class="more">') :]
        self.assertTrue(
            detail.startswith(
                '<tr class="more"><td></td><td colspan="12"><div><b>timed out'
                " after 363.3 s</b> · rows 7/11</div>"
            ),
            detail[:200],
        )
        self.assertIn("last stderr lines (not the cause)", detail)
        self.assertNotIn(">error excerpt<", detail)
        self.assertIn("124 (timed out) / 124 (timed out)", row)
        self.assertIn("Both sides exited non-zero", detail)
        self.assertIn("<summary>Command line</summary><pre>UCX_LOG_FILE=", detail)
        self.assertIn(
            "<code>vm2-net-10.1.0/logs/012-vm2-net__ucx__tcp__VV__WRITE__N/</code>"
            ' in the <a href="https://github.com/ROCm/nixl/releases/download/',
            detail,
        )

    def test_a_failed_row(self) -> None:
        page, _ = self.render()
        at = page.index('id="case-10-1-0-vm1-fabric-ais-compat-allowed-WRITE-S-head-0"')
        row = page[at : page.index('<tr class="main"', at)]
        self.assertIn('<div class="label">error excerpt</div><pre>Consistency', row)
        self.assertIn("iov 0:0\nConsistency check failed for iov 0:1\n", row)
        self.assertNotIn(" | ", row[row.index("<pre>") : row.index("</pre>")])
        self.assertNotIn("timed out after", row)
        self.assertNotIn("Both sides exited non-zero", row)
        self.assertIn("<td>1 / -</td>", row)
        self.assertIn("<td>VRAM→FILE</td>", row)
        self.assertIn("<td>none</td>", row)
        # One side only exited non-zero in the UCX rows: READ is 1/134.
        at = page.index('id="case-10-1-0-vm1-loop-ucx-rc-DV-READ-S-head-0"')
        row = page[at : page.index('<tr class="main"', at)]
        self.assertIn("<td>1 / 134 (signal 6 ABRT)</td>", row)
        self.assertIn("Both sides exited non-zero", row)
        self.assertIn("<td>rc → rc</td>", row)

    def test_variant_round_and_required_are_marked(self) -> None:
        first = self.index["attention"][0]
        first.update({"variant": "aa", "round": 2, "required": True})
        page, audit = self.render()
        self.assertIn("variant aa, round 2", audit.words())
        self.assertIn('<span class="badge">required</span>', page)
        self.assertEqual(page.count('<span class="badge">required</span>'), 1)
        self.assertEqual(page.count("variant aa"), 1)

    def test_no_logs_archived(self) -> None:
        self.index["links"]["raw_logs"] = None
        page, audit = self.render()
        self.assertIn("· no logs archived ·", audit.words())
        self.assertEqual(audit.words().count("Logs: no logs archived"), 20)
        self.assertNotIn(".logs.tar.gz", page)
        self.assertNotIn(">logs tarball<", page)

    def test_the_source_run_label_depends_on_the_reason(self) -> None:
        source = "https://github.com/ROCm/nixl/actions/runs/44814378049"
        page, _ = self.render()
        self.assertIn(f'<a href="{source}">source run (wheels build)</a>', page)
        self.assertNotIn("nightly run<", page)
        self.index["shard"]["reason"] = "nightly"
        page, _ = self.render()
        self.assertIn(f'<a href="{source}">nightly run</a>', page)
        self.assertNotIn("wheels build", page)
        self.index["links"]["source_run"] = None
        page, _ = self.render()
        self.assertIn("no nightly run recorded", page)

    def test_null_links_and_a_null_branch(self) -> None:
        self.index["shard"].update({"branch": None, "sha": None})
        self.index["links"].update({"commit": None, "raw_records": None})
        page, audit = self.render()
        words = audit.words()
        self.assertIn(f"branch {SLUG} · unknown commit · NIXL 1.6.0", words)
        self.assertIn("raw records not archived", words)
        self.assertNotIn("/commit/", page)

    def test_links_inside_the_site_name_their_file(self) -> None:
        _, audit = self.render()
        data = f"../../data/bench/v1/night/20261120T204631Z/{SLUG}/44820022442.a1"
        matrix = self.index["links"]["matrix"]
        inside = [h for h in audit.hrefs if not h.startswith(("https://", "#"))]
        self.assertEqual(
            sorted(set(inside)),
            sorted(
                [
                    "../../index.html",
                    "../index.html",
                    f"../../data/bench/v1/{matrix}",
                    "../../data/bench/v1/manifest.json",
                    f"{data}/index.json",
                    *(
                        f"{data}/{name}.ndjson.gz"
                        for name in ("runs", "cases", "samples", "counters", "tests")
                    ),
                ]
            ),
        )
        self.assertFalse(matrix.startswith("data/"))
        fragments = {h[1:] for h in audit.hrefs if h.startswith("#")}
        self.assertEqual(
            fragments, {"failures", "skipped", "lanes", "coverage", "ratios"}
        )
        self.assertLessEqual(fragments, set(audit.ids))
        docs = self.index["links"]["docs"]
        self.assertIn(f"{docs}#blocker-codes", audit.hrefs)
        self.assertIn(docs, audit.hrefs)

    def test_the_lanes_table(self) -> None:
        page, audit = self.render()
        for entry in self.index["lanes"]:
            anchor = pages.lane_anchor(entry["lane"], entry["rocm_version"])
            self.assertIn(anchor, audit.ids)
        self.assertEqual(pages.lane_anchor("vm2-net", "10.1.0"), "lane-vm2-net-10-1-0")
        self.assertIn(
            '<tr id="lane-vm2-net-10-1-0"><td>vm2-net</td><td>10.1.0</td>'
            "<td>gha-kvm-vm2</td><td>emulated</td>"
            '<td><span class="st pass">complete</span></td>'
            '<td class="num">25</td><td class="num">6</td><td class="num">3</td>'
            '<td class="num">0</td><td class="num">0</td><td class="num">2</td>'
            '<td class="num">0</td><td class="num">82/0/0/0</td>'
            '<td class="num">20.31</td>'
            '<td class="num">21:28</td>'
            '<td class="num">17m 14s</td></tr>',
            page,
        )
        self.assertIn('<span class="st blocked">blocked</span>', page)
        # A wheels run started by hand gives no suffix, a nightly the slug;
        # the records of a bench started by hand do not say which it was.
        self.assertIn(
            "artifact <code>bench-diagnostics-vm2-net-10.1.0</code>, or"
            f" <code>bench-diagnostics-vm2-net-10.1.0-{SLUG}</code> when the source"
            " run is a nightly, on the",
            page,
        )
        self.assertEqual(page.count("guest and NIC diagnostics"), 7)
        self.assertNotIn("bench-diagnostics-vm1-pcie", page)
        self.assertEqual(page.count("expires with the run"), 7)
        self.index["shard"]["reason"] = "nightly"
        nightly, _ = self.render()
        self.assertIn(
            f"artifact <code>bench-diagnostics-vm2-net-10.1.0-{SLUG}</code> on the",
            nightly,
        )
        self.assertNotIn("<code>bench-diagnostics-vm2-net-10.1.0</code>", nightly)
        self.assertNotIn("when the source run is a nightly", nightly)
        vm2 = next(e for e in self.index["lanes"] if e["lane"] == "vm2-net")
        vm2.update({"filtered": True, "status": "partial"})
        page, _ = self.render()
        self.assertIn('<span class="st skip">partial</span> filtered</td>', page)

    def test_the_skipped_section_groups_by_reason_and_lane(self) -> None:
        extra = dict(self.index["attention"][-1])
        extra.update({"case_key": "bench-container/obj/crt/WRITE/N"})
        other = dict(extra, reason="budget", error_excerpt="", case_key="z/last")
        self.index["attention"] += [extra, other]
        page, audit = self.render()
        self.assertIn("Skipped (5)", audit.words())
        at = page.index('<h2 id="skipped">')
        section = page[at : page.index('<h2 id="lanes">')]
        self.assertEqual(section.count("<tr>"), 1 + 4)
        self.assertLess(section.index("budget"), section.index("env"))
        self.assertIn("<summary>2 case keys</summary>", section)
        self.assertEqual(section.count("<summary>1 case key</summary>"), 3)
        self.assertEqual(section.count("needs s3"), 3)

    def test_a_lane_that_did_not_report_is_one_row(self) -> None:
        template = self.index["attention"][0]
        lost = [
            dict(
                template,
                lane="vm1-local",
                rocm_version="10.1.0",
                case_key=f"vm1-local/lost/{number}",
                status="not-run",
                reason="no record",
                error_excerpt="",
                cmdline=None,
                log_ref=None,
                exit_init=None,
                duration_s=None,
            )
            for number in range(40)
        ]
        kept = dict(lost[0], lane="vm2-net", case_key="vm2-net/not-run/one")
        self.index["attention"] += lost + [kept]
        local = next(e for e in self.index["lanes"] if e["lane"] == "vm1-local")
        local["status"] = "missing"
        self.index["totals"]["not-run"] = 41
        page, audit = self.render()
        words = audit.words()
        self.assertIn("lane did not report: 40 cases of vm1-local 10.1.0", words)
        self.assertEqual(page.count("lane did not report"), 1)
        self.assertNotIn("vm1-local/lost/", page)
        # A not-run case of a lane that did report keeps its own row.
        self.assertIn("<code>vm2-net/not-run/one</code>", page)
        self.assertIn("Failed, timed out, crashed or not run (61)", words)
        self.assertIn("61 of 348 cases failed, timed out or did not run", words)
        self.assertIn('<span class="st fail">missing</span>', page)
        self.assertIn("Logs: the case has no log directory.", words)

    def test_the_log_files_named_are_those_a_case_has(self) -> None:
        page, _ = self.render()
        self.assertEqual(
            page.count(
                "(<code>init.stdout.log</code>, <code>init.stderr.log</code>;"
                " two-sided cases also <code>init.ucx.log</code> and"
                " <code>tgt.*</code>)"
            ),
            20,
        )

    def test_a_number_no_float_can_hold_is_shown_as_absent(self) -> None:
        huge = 10**400
        entry = self.index["attention"][0]
        entry.update(duration_s=huge, rows_seen=huge, exit_init=huge)
        self.index["lanes"][0]["calib_gbps"] = huge
        self.index["ratios"][0]["points"] = [[4096, 4, huge, huge, huge]]
        self.index["ratios"][0]["status"] = "ok"
        self.assertEqual(pages.failure_groups(self.index["attention"])[0]["count"], 10)
        page, _ = self.render()
        self.assertIn(str(huge), page)

    def test_the_case_table_is_capped(self) -> None:
        template = self.index["attention"][0]
        self.index["attention"] = [
            dict(template, case_key=f"vm1-fabric/many/{number:04d}")
            for number in range(pages.MAX_CASE_ROWS + 7)
        ]
        page, audit = self.render()
        self.assertEqual(page.count('<tr class="more"><td></td><td colspan="12">'), 500)
        self.assertIn("vm1-fabric/many/0499", page)
        self.assertNotIn("vm1-fabric/many/0500", page)
        self.assertIn("7 more cases are not shown: every case record is in", page)
        self.assertIn(
            f"night/20261120T204631Z/{SLUG}/44820022442.a1/cases.ndjson.gz", page
        )
        self.assertIn("By cause (1 group)", audit.words())
        self.assertIn('<td class="num">507</td>', page)

    def test_the_order_of_the_case_table_is_that_of_attention(self) -> None:
        page, _ = self.render()
        expected = [
            pages.case_anchor(
                e["rocm_version"], e["case_key"], e["variant"], e["round"]
            )
            for e in self.index["attention"]
            if e["status"] != "skip"
        ]
        found = re.findall(r'<tr class="main" id="(case-[^"]+)">', page)
        self.assertEqual(found, expected)
        self.index["attention"].reverse()
        page, _ = self.render()
        found = re.findall(r'<tr class="main" id="(case-[^"]+)">', page)
        self.assertEqual(found, expected[::-1])

    def test_the_ratios(self) -> None:
        page, audit = self.render()
        at = page.index('<details class="section">')
        section = page[at : page.index("</details>\n<footer>")]
        self.assertTrue(
            section.startswith(
                '<details class="section">\n<summary><h2 id="ratios">Declared'
                " ratios (3)</h2></summary>\n"
            )
        )
        self.assertNotIn("<details open", page)
        self.assertIn("emulated devices", section)
        self.assertIn("unavailable: num timeout", section)
        self.assertIn("unavailable: num blocked", section)
        ok = next(r for r in self.index["ratios"] if r["status"] == "ok")
        block, batch, num, den, value = ok["points"][0]
        self.assertIn(
            f"{block}×{batch}: <b>{value:.4f}</b> ({num:.3f} / {den:.3f})", section
        )
        self.assertEqual(section.count("<tr>"), 1 + 3)

    def test_the_footer(self) -> None:
        self.index["collect"] = {
            "rejected": 2,
            "warnings": ["a run record of another stamp"],
            "required_failures": ["box/posix/AIO/buffered/WRITE/S"],
        }
        self.index["warnings"] = ["logs tarball not archived: x/y.log, secret"]
        _, audit = self.render()
        words = audit.words()
        self.assertIn("Collector: 2 rejected, 1 warning, 1 required failure.", words)
        self.assertIn("Collector warnings: a run record of another stamp", words)
        self.assertIn("Required cases that failed: box/posix/AIO", words)
        self.assertIn("Publisher warnings: logs tarball not archived", words)

    def test_a_missing_key_is_a_page_error(self) -> None:
        for key in (
            "shard",
            "links",
            "totals",
            "tests",
            "lanes",
            "attention",
            "tests_attention",
            "blocked",
            "ratios",
            "collect",
            "warnings",
        ):
            broken = copy.deepcopy(self.index)
            del broken[key]
            with self.assertRaisesRegex(pages.PageError, key):
                pages.render_run(broken)
        for where, key in (
            ("shard", "stamp"),
            ("links", "raw_logs"),
            ("links", "data"),
            ("totals", "not-run"),
        ):
            broken = copy.deepcopy(self.index)
            del broken[where][key]
            with self.assertRaisesRegex(pages.PageError, key):
                pages.render_run(broken)
        for where, key in (
            ("attention", "case_key"),
            ("attention", "cmdline"),
            ("lanes", "n"),
            ("blocked", "case_keys"),
            ("ratios", "points"),
        ):
            broken = copy.deepcopy(self.index)
            del broken[where][0][key]
            with self.assertRaisesRegex(pages.PageError, key):
                pages.render_run(broken)
        odd: list[Any] = [None, [], "x", {"shard": []}]
        for value in odd:
            with self.assertRaises(pages.PageError):
                pages.render_run(value)
        broken = copy.deepcopy(self.index)
        broken["lanes"][0]["n"] = [1, 2, 3]
        with self.assertRaises(pages.PageError):
            pages.render_run(broken)
        broken["lanes"][0]["n"] = ["1"] * 7
        with self.assertRaises(pages.PageError):
            pages.render_run(broken)

    def test_extra_keys_are_ignored(self) -> None:
        page, _ = self.render()
        self.index["later"] = {"x": 1}
        self.index["shard"]["later"] = "<b>"
        self.index["attention"][0]["later"] = "<b>"
        self.index["lanes"][0]["later"] = "<b>"
        self.assertEqual(pages.render_run(self.index), page)


class RunPageHostile(Base):
    KEEP = frozenset({"status", "reason"})

    def check(self, page: str, shown: bool = True) -> Audit:
        audit = self.audit(page)
        self.assertNotIn("<script", page)
        self.assertNotIn('onclick="x"', page)
        for href in audit.hrefs:
            self.assertNotIn("javascript", href)
            self.assertNotIn("evil", href)
        if shown:
            self.assertIn(html_escape(HOSTILE), page)
            self.assertIn(HOSTILE, audit.words())
        return audit

    def test_every_string_of_the_index(self) -> None:
        poisoned = poison(self.index)
        audit = self.check(pages.render_run(poisoned))
        self.assertEqual(
            sorted(set(audit.hrefs)),
            [
                "#coverage",
                "#failures",
                "#lanes",
                "#ratios",
                "#skipped",
                "../../data/bench/v1/manifest.json",
                "../../index.html",
                "../index.html",
            ],
        )
        self.assertIn("no logs archived", audit.words())

    def test_every_text_field_with_the_statuses_kept(self) -> None:
        poisoned = poison(self.index, self.KEEP)
        page = pages.render_run(poisoned)
        audit = self.check(page)
        # Every failing case is still listed, with its key as text.
        self.assertEqual(page.count('<tr class="more"><td></td><td colspan="12">'), 20)
        self.assertIn(f"<code>{html_escape(HOSTILE)}</code>", page)
        self.assertIn(f"<pre>{html_escape(HOSTILE)}</pre>", page)
        self.assertEqual(len(audit.ids), len(set(audit.ids)))
        for ident in audit.ids:
            self.assertRegex(ident, r"\A[A-Za-z0-9_-]+\Z")

    def test_hostile_excerpts_branches_and_case_keys(self) -> None:
        first = self.index["attention"][0]
        first["error_excerpt"] = (
            "</pre><script>alert(1)</script> | <img src=x onerror=y>"
        )
        first["case_key"] = 'vm1/<svg onload=alert(1)>/" id="x'
        first["cmdline"] = "nixlbench --x='</pre></details><script>'"
        first["log_ref"] = "../../../<b>etc</b>"
        self.index["shard"]["branch"] = "feat/<script>alert('b')</script>&amp;\"x\""
        self.index["shard"]["slug"] = 'a"><script>'
        self.index["lanes"][0]["lane"] = 'lane" onmouseover="alert(1)'
        self.index["blocked"][0]["blocked_by"] = ["<W1>", '"W2"']
        self.index["blocked"][0]["case_keys"] = ["k/<i>"]
        self.index["ratios"][1]["why"] = "<u>why</u>"
        self.index["warnings"] = ["<script>w</script>"]
        page = pages.render_run(self.index)
        audit = self.check(page, shown=False)
        self.assertIn(
            "<pre>&lt;/pre&gt;&lt;script&gt;alert(1)&lt;/script&gt;\n"
            "&lt;img src=x onerror=y&gt;</pre>",
            page,
        )
        self.assertIn("&lt;svg onload=alert(1)&gt;/&quot; id=&quot;x</code>", page)
        self.assertIn(
            "--x=&#x27;&lt;/pre&gt;&lt;/details&gt;&lt;script&gt;&#x27;", page
        )
        self.assertIn("<code>../../../&lt;b&gt;etc&lt;/b&gt;/</code>", page)
        self.assertIn(
            "<b>feat/&lt;script&gt;alert(&#x27;b&#x27;)&lt;/script&gt;"
            "&amp;amp;&quot;x&quot;</b>",
            page,
        )
        self.assertIn("<title>nixlbench run 2026-11-20 20:46 UTC · feat/&lt;", page)
        self.assertIn('<tr id="lane-lane--onmouseover--alert-1--10-1-0">', page)
        self.assertIn("lane&quot; onmouseover=&quot;alert(1)", page)
        self.assertIn("&lt;W1&gt;</a>, <a ", page)
        self.assertIn("<code>k/&lt;i&gt;</code>", page)
        self.assertIn("unavailable: &lt;u&gt;why&lt;/u&gt;", page)
        self.assertIn("<li>&lt;script&gt;w&lt;/script&gt;</li>", page)
        self.assertIn('vm1/<svg onload=alert(1)>/" id="x', audit.words())

    def test_links_that_are_not_github_are_not_links(self) -> None:
        names = (
            "actions_run",
            "source_run",
            "commit",
            "raw_release",
            "raw_records",
            "raw_logs",
            "raw_collect",
            "docs",
        )
        for bad in BAD_LINKS:
            for name in names:
                self.index["links"][name] = bad
            page = pages.render_run(self.index)
            audit = self.check(page, shown=False)
            self.assertEqual(
                [href for href in audit.hrefs if href.startswith("http")], [], bad
            )
            self.assertNotIn(html_escape(bad), page)
            words = audit.words()
            self.assertIn("no logs archived", words)
            self.assertIn("Actions run 44820022442 attempt 1 ·", words)
            self.assertIn("(no link)", words)

    def test_paths_that_leave_the_site_are_not_links(self) -> None:
        for bad in BAD_PATHS:
            self.index["links"]["data"] = bad
            self.index["links"]["matrix"] = bad
            page = pages.render_run(self.index)
            audit = self.check(page, shown=False)
            inside = [h for h in audit.hrefs if h.startswith("../")]
            self.assertEqual(
                sorted(set(inside)),
                [
                    "../../data/bench/v1/manifest.json",
                    "../../index.html",
                    "../index.html",
                ],
                bad,
            )
            self.assertIn(f"<code>{html_escape(bad)}/index.json</code>", page)
            self.assertIn(f"matrix <code>{html_escape(bad)}</code>", page)

    def test_odd_values_do_not_break_the_page(self) -> None:
        first = self.index["attention"][0]
        first.update(
            {
                "exit_init": "boom",
                "exit_tgt": 9999,
                "duration_s": "long",
                "rows_seen": {"a": 1},
                "round": None,
                "order": None,
                "required": "yes",
            }
        )
        self.index["lanes"][0].update(
            {"started_at": "yesterday", "finished_at": 5, "calib_gbps": "fast"}
        )
        self.index["shard"].update({"stamp": 20261120, "run_attempt": None})
        page = pages.render_run(self.index)
        self.check(page, shown=False)
        self.assertIn("<td>boom / 9999</td>", page)
        self.assertIn('<td class="num">yesterday</td>', page)
        self.assertIn("nixlbench run of 20261120", page)


class Landing(Base):
    def render(
        self,
        manifest: Optional[dict[str, Any]] = None,
        latest: Optional[dict[str, Any]] = None,
    ) -> tuple[str, Audit]:
        page = pages.render_landing(
            self.manifest if manifest is None else manifest,
            self.latest if latest is None else latest,
        )
        return page, self.audit(page)

    def test_the_first_run(self) -> None:
        page, audit = self.render()
        words = audit.words()
        self.assertIn(VERDICT, words)
        self.assertIn(
            "latest run, started by hand; no nightly bench published yet"
            " · 2026-11-20 20:46 UTC",
            words,
        )
        self.assertNotIn("latest nightly", words)
        self.assertIn("Failed, timed out, crashed or not run (20)", words)
        self.assertIn("Showing 20 of 20: all 20 on the run page", words)
        self.assertIn("nixlbench: SG mode — set NEURON_RT_NUM_CORES=1", words)
        self.assertIn(
            f'<p class="verdict fail"><a href="runs/{SHARD}.html#failures">', page
        )
        for entry in self.latest["branches"][SLUG]["attention"]:
            self.assertIn(f"<code>{entry['case_key']}</code>", page)
        self.assertEqual(
            audit.headings(), ["branch-for-upstream-feat-rocm-wheels", "runs", "data"]
        )
        self.assertLess(len(page.encode("utf-8")), 60_000)
        self.assertEqual(
            page,
            pages.render_landing(
                copy.deepcopy(self.manifest), copy.deepcopy(self.latest)
            ),
        )

    def test_the_banner_and_the_failures_precede_everything_else(self) -> None:
        page, _ = self.render()
        order = [
            page.index("<h1>"),
            page.index('<p class="note">'),
            page.index("<h2 id="),
            page.index('<p class="verdict fail">'),
            page.index("<h3>Failed, timed out"),
            page.index("vm1-fabric/ais/compat-allowed/WRITE/S"),
            page.index('<h2 id="runs">'),
            page.index('<ul class="legend">'),
            page.index('<h2 id="data">'),
        ]
        self.assertEqual(order, sorted(order))

    def test_links_inside_the_site_name_their_file(self) -> None:
        page, audit = self.render()
        inside = {h.split("#")[0] for h in audit.hrefs if not h.startswith("https")}
        self.assertEqual(
            inside,
            {
                "../index.html",
                "../wheels/index.html",
                f"runs/{SHARD}.html",
                "../data/bench/v1/manifest.json",
                "../data/bench/v1/latest.json",
            },
        )
        self.assertEqual(
            [h for h in audit.hrefs if h.startswith("https")],
            [self.manifest["links"]["docs"]],
        )
        # A failing case links to its row of the run page, a lane to its row.
        run = Audit(pages.render_run(self.index))
        fragments = {h.split("#")[1] for h in audit.hrefs if "#" in h}
        self.assertEqual(len(fragments), 1 + 10 + 20)
        self.assertLessEqual(fragments, set(run.ids))
        self.assertIn(
            f'<a href="runs/{SHARD}.html#case-10-1-0-vm2-net-ucx-ud-VD-WRITE-S-head-0">'
            "<code>vm2-net/ucx/ud/VD/WRITE/S</code></a>",
            page,
        )

    def test_the_cells_of_the_first_run(self) -> None:
        _, audit = self.render()
        grid = [
            mark for mark in audit.marks if mark[0] != "fail" or "failed" in mark[1]
        ]
        row = [
            ("pass", "ok 26"),
            ("pass", "ok 26"),
            ("pass", "ok 26"),
            ("fail", "4 failed"),
            ("fail", "2 failed"),
            ("fail", "5 failed"),
            ("blocked", "blocked"),
            ("blocked", "blocked"),
            ("blocked", "blocked"),
            ("fail", "9 failed"),
        ]
        self.assertEqual(grid[:10], row)
        self.assertEqual(grid[10:21], row + [("fail", "20 failed")])
        titles = [
            str(v.get("title")) for n, v in audit.tags if n == "a" and "title" in v
        ]
        self.assertEqual(
            titles[0],
            "pass 26, fail 0, timeout 0, crash 0, skip 8, blocked 7, not-run 0",
        )
        self.assertEqual(len(titles), 20)

    def test_every_cell_of_the_precedence_list(self) -> None:
        winner = "20261119T010000Z-main-12.a1"
        lanes = {
            "a/1": lane("missing", [0, 0, 0, 0, 0, 0, 9]),
            "b/1": lane("blocked", [0, 0, 0, 0, 0, 5, 0]),
            "c/1": lane("complete", [3, 1, 1, 1, 0, 0, 1]),
            "d/1": lane("partial", [3, 1, 0, 0, 0, 0, 0]),
            "e/1": lane("complete", [3, 0, 0, 1, 0, 0, 0], filtered=True),
            "f/1": lane("partial", [7, 0, 0, 0, 2, 0, 0]),
            "g/1": lane("complete", [8, 0, 0, 0, 1, 0, 0], filtered=True),
            "h/1": lane("complete", [9, 0, 0, 0, 4, 2, 0]),
            "i/1": lane("partial", [1, 2, 0, 0, 0, 0, 0], filtered=True),
            "j/1": lane("complete", [5, 5, 0, 0, 0, 0, 0]),
            "k/1": lane("missing", [0, 0, 0, 0, 0, 0, 3]),
        }
        older = shard(
            "20261119T010000Z",
            "11",
            lanes,
            superseded_by={"j/1": winner, "k/1": winner},
        )
        newer = shard(
            "20261119T010000Z", "12", {"z/2": lane("complete", [1, 0, 0, 0, 0, 0, 0])}
        )
        page, audit = self.render(manifest_of(newer, older), {"branches": {}})
        at = page.index('<h2 id="runs">')
        rows = page[at:].split("<tr>")[2:4]
        self.assertEqual(re.findall(r"<td>-</td>", rows[0]), ["<td>-</td>"] * 11)
        self.assertEqual(rows[1].count("<td>-</td>"), 1)
        marks = audit.marks[: audit.marks.index(("pass", "ok N"))]
        self.assertEqual(
            marks,
            [
                ("pass", "ok 1"),
                ("pass", "ok 1"),
                ("fail", "missing"),
                ("blocked", "blocked"),
                ("fail", "4 failed"),
                ("fail", "1 failed · partial"),
                ("fail", "1 failed · filtered"),
                ("skip", "partial 7"),
                ("skip", "filtered 8"),
                ("pass", "ok 9"),
                ("fail", "2 failed · partial"),
                ("muted", "superseded"),
                ("muted", "superseded"),
                ("fail", "25 failed"),
            ],
        )
        self.assertIn(
            f'<a class="st muted" title="superseded by {winner}; pass 5, fail 5,'
            ' timeout 0, crash 0, skip 0, blocked 0, not-run 0"'
            ' href="runs/20261119T010000Z-main-11.a1.html#lane-j-1">superseded</a>',
            page,
        )
        self.assertIn(
            'title="pass 9, fail 0, timeout 0, crash 0, skip 4, blocked 2, not-run 0"'
            ' href="runs/20261119T010000Z-main-11.a1.html#lane-h-1">ok 9</a>',
            page,
        )
        heads = re.findall(r"<th>([a-z])<br>([0-9])</th>", page[at:])
        self.assertEqual([name for name, _ in heads], list("abcdefghijkz"))
        self.assertIn("No bench run", "No bench run has been published yet.")
        self.assertNotIn("No bench run has been published yet.", page)

    def test_branches_are_ordered_by_stamp_then_slug(self) -> None:
        lanes = {"a/1": lane("complete", [1, 0, 0, 0, 0, 0, 0])}
        shards = {
            slug: shard(stamp, "5", lanes, slug=slug)
            for slug, stamp in (
                ("zeta", "20261118T000000Z"),
                ("beta", "20261119T000000Z"),
                ("alpha", "20261118T000000Z"),
                ("gamma", "20261120T000000Z"),
            )
        }
        latest = {"branches": {slug: branch(entry) for slug, entry in shards.items()}}
        manifest = manifest_of(*shards.values())
        page, audit = self.render(manifest, latest)
        self.assertEqual(
            audit.headings()[:4],
            ["branch-gamma", "branch-beta", "branch-alpha", "branch-zeta"],
        )
        self.assertEqual(page.count("latest nightly · "), 4)
        self.assertEqual(page.count('<p class="verdict ok">'), 4)
        self.assertEqual(page.count("<p>No case failed.</p>"), 4)
        self.assertNotIn("started by hand", page)
        # The runs table keeps the order of the manifest.
        dates = re.findall(r'<tr><td><a href="runs/([0-9TZ]+)-', page)
        self.assertEqual(dates, [entry["stamp"] for entry in shards.values()])

    def test_stale_branches_are_collapsed(self) -> None:
        lanes = {"a/1": lane("complete", [1, 2, 0, 0, 0, 0, 0])}
        fresh = shard("20261120T000000Z", "5", lanes, slug="fresh")
        old = shard("20261001T000000Z", "3", lanes, slug="old")
        older = shard("20260901T000000Z", "2", lanes, slug="older", branch=None)
        latest = {
            "branches": {
                "older": branch(older, stale=True, branch=None),
                "fresh": branch(fresh),
                "old": branch(
                    old,
                    stale=True,
                    attention_total=2,
                    attention=[
                        {
                            "shard": old["id"],
                            "lane": "a",
                            "rocm_version": "1",
                            "case_key": "a/stale/case",
                            "variant": "head",
                            "round": 0,
                            "status": "fail",
                            "reason": "exit-nonzero",
                            "required": False,
                            "error_excerpt": "x",
                        }
                    ],
                ),
            }
        }
        page, audit = self.render(manifest_of(fresh, old, older), latest)
        self.assertEqual(audit.headings(), ["branch-fresh", "runs", "data"])
        self.assertEqual(page.count("<details>"), 1)
        self.assertNotIn("<details open", page)
        at = page.index("<details>")
        block = page[at : page.index("</details>")]
        self.assertIn("<summary>Branches with no recent run (2)</summary>", block)
        self.assertEqual(block.count("<li>"), 2)
        self.assertLess(block.index("<li>old "), block.index("<li>older "))
        self.assertIn(
            "<li>old · 2026-10-01 00:00 UTC · 2 of 3 cases failed · 0 skipped"
            " · 0 blocked · 1 passed · "
            '<a href="runs/20261001T000000Z-old-3.a1.html">run page</a></li>',
            block,
        )
        self.assertNotIn("a/stale/case", page)
        self.assertEqual(page.count('class="verdict'), 1)
        self.assertLess(at, page.index('<h2 id="runs">'))
        self.assertLess(page.index('<h2 id="branch-fresh">'), at)
        self.assertNotIn("newer manual run", page)
        # A run after the headline's is named in the collapsed line too.
        hand = shard("20261003T000000Z", "4", lanes, slug="old", reason="dispatch")
        latest["branches"]["old"]["newer"] = {
            "stamp": hand["stamp"],
            "shards": [hand["id"]],
            "reasons": ["dispatch"],
            "totals": dict(zip(pages.STATUSES, hand["n"])),
            "failing": 2,
        }
        page, _ = self.render(manifest_of(fresh, hand, old, older), latest)
        self.assertIn(
            '<a href="runs/20261001T000000Z-old-3.a1.html">run page</a>'
            f' · newer manual run: <a href="runs/{hand["id"]}.html">2026-10-03'
            " 00:00 UTC</a> (2 failed)</li>",
            page,
        )
        self.assertEqual(page.count("newer manual run"), 1)

    def test_a_newer_manual_run_is_a_second_line(self) -> None:
        lanes = {"a/1": lane("complete", [4, 1, 0, 0, 0, 0, 0])}
        night = shard("20261119T010000Z", "7", lanes)
        hand = shard("20261120T090000Z", "8", lanes, reason="dispatch")
        again = shard("20261120T090000Z", "9", lanes, reason="dispatch")
        newer = {
            "stamp": hand["stamp"],
            "shards": [again["id"], hand["id"]],
            "reasons": ["dispatch"],
            "totals": dict(zip(pages.STATUSES, [0, 3, 0, 0, 0, 0, 0])),
            "failing": 3,
        }
        latest = {"branches": {"main": branch(night, newer=newer)}}
        page, audit = self.render(manifest_of(again, hand, night), latest)
        self.assertIn(
            f'<p>newer manual run: <a href="runs/{again["id"]}.html">2026-11-20'
            f' 09:00 UTC</a>, <a href="runs/{hand["id"]}.html">run 2</a>'
            " (3 failed)</p>",
            page,
        )
        self.assertIn("latest nightly · 2026-11-19 01:00 UTC", audit.words())
        self.assertIn(
            f'<p class="verdict fail"><a href="runs/{night["id"]}.html#failures">'
            "1 of 5 cases failed · 0 skipped · 0 blocked · 4 passed</a></p>",
            page,
        )
        self.assertLess(
            page.index('<p class="verdict fail">'), page.index("newer manual run")
        )
        self.assertEqual(page.count('class="verdict'), 1)
        self.assertNotIn("newer manual run", self.render()[0])

    def test_a_night_of_several_runs_and_failed_probes(self) -> None:
        lanes = {"a/1": lane("complete", [4, 0, 0, 0, 0, 0, 0])}
        first = shard("20261119T010000Z", "7", lanes)
        second = shard("20261119T010000Z", "8", lanes)
        probe = {
            "shard": second["id"],
            "lane": "a",
            "rocm_version": "1",
            "suite": "probe",
            "test_id": "nvme/identify",
            "status": "fail",
        }
        entry = branch(
            first,
            shards=[second["id"], first["id"]],
            tests={"pass": 3, "fail": 1, "skip": 0, "timeout": 1},
            tests_attention=[probe],
            tests_attention_total=2,
        )
        page, audit = self.render(
            manifest_of(second, first), {"branches": {"main": entry}}
        )
        words = audit.words()
        self.assertIn("0 of 4 cases failed", words)
        self.assertIn("4 passed · 2 probes or tests failed", words)
        self.assertIn('<p class="verdict fail">', page)
        self.assertIn("Merged over the runs of that night:", words)
        self.assertIn("Failed probes and tests (2)", words)
        self.assertIn(
            f'<a href="runs/{second["id"]}.html#tests"><code>probe/nvme/identify'
            "</code></a> on a 1</li>",
            page,
        )
        self.assertIn("<p>No case failed.</p>", page)

    def test_a_lane_that_did_not_report_is_one_row(self) -> None:
        entry = self.latest["branches"][SLUG]
        template = dict(entry["attention"][0])
        lost = [
            dict(
                template,
                lane="vm1-local",
                rocm_version="10.1.0",
                case_key=f"vm1-local/lost/{number}",
                status="not-run",
                reason="no record",
                error_excerpt="",
            )
            for number in range(40)
        ]
        kept = dict(lost[0], lane="vm2-net", case_key="vm2-net/not-run/one")
        entry["attention"] = entry["attention"][:18] + lost + [kept]
        entry["attention_total"] = 59
        local = next(e for e in entry["lanes"] if e["lane"] == "vm1-local")
        local.update(status="missing", n=[0, 0, 0, 0, 0, 0, 40])
        page, audit = self.render()
        self.assertEqual(page.count("lane did not report:"), 1)
        self.assertIn(
            f'<td class="key"><a href="runs/{SHARD}.html#failures">lane did not'
            " report: 40 cases</a></td><td>vm1-local</td><td>10.1.0</td>"
            "<td>no record</td>",
            page,
        )
        self.assertNotIn("vm1-local/lost/", page)
        self.assertIn("<code>vm2-net/not-run/one</code>", page)
        self.assertIn("Showing 59 of 59: all 59 on the run page", audit.words())
        # The run page has one row for the lane and no row for any of its
        # cases, so nothing on the landing page may point at one.
        self.index["attention"] = [
            dict(self.index["attention"][0], **item) for item in entry["attention"]
        ]
        lane_of = next(e for e in self.index["lanes"] if e["lane"] == "vm1-local")
        lane_of["status"] = "missing"
        run = Audit(pages.render_run(self.index))
        fragments = {h.split("#")[1] for h in audit.hrefs if "#" in h}
        self.assertLessEqual(fragments, set(run.ids))
        self.assertEqual(len(fragments), 1 + 10 + 18 + 1)

    def test_the_failures_table_is_capped(self) -> None:
        entry = self.latest["branches"][SLUG]
        template = entry["attention"][0]
        entry["attention"] = [
            dict(template, case_key=f"vm1-fabric/many/{number:03d}")
            for number in range(100)
        ]
        entry["attention_total"] = 240
        page, audit = self.render()
        self.assertEqual(page.count("vm1-fabric/many/"), pages.MAX_LANDING_FAILURES)
        self.assertIn("vm1-fabric/many/049", page)
        self.assertNotIn("vm1-fabric/many/050", page)
        self.assertIn("Showing 50 of 240: all 240 on the run page", audit.words())
        self.assertIn("Failed, timed out, crashed or not run (240)", audit.words())

    def test_the_runs_table_is_capped(self) -> None:
        lanes = {"a/1": lane("complete", [1, 0, 0, 0, 0, 0, 0])}
        shards = [
            shard(f"2026{month:02d}{day:02d}T000000Z", "5", lanes)
            for month in (11, 10, 9, 8, 7)
            for day in range(28, 0, -1)
        ]
        page, audit = self.render(manifest_of(*shards), {"branches": {}})
        self.assertEqual(len(shards), 140)
        self.assertEqual(page.count('<tr><td><a href="runs/'), 120)
        self.assertIn("The newest 120 of 140 runs", audit.words())
        self.assertIn(shards[119]["id"], page)
        self.assertNotIn(shards[120]["id"], page)

    def test_an_empty_site(self) -> None:
        page, audit = self.render(manifest_of(), {"branches": {}})
        words = audit.words()
        self.assertIn("No bench run has been published yet.", words)
        self.assertEqual(audit.headings(), ["data"])
        self.assertNotIn("<table", page)
        self.assertEqual(
            audit.hrefs,
            [
                "../index.html",
                "../wheels/index.html",
                "../data/bench/v1/manifest.json",
                "../data/bench/v1/latest.json",
            ],
        )
        self.assertIn("emulated devices", words)

    def test_a_missing_key_is_a_page_error(self) -> None:
        for key in ("shards", "links"):
            broken = copy.deepcopy(self.manifest)
            del broken[key]
            with self.assertRaisesRegex(pages.PageError, key):
                pages.render_landing(broken, self.latest)
        with self.assertRaisesRegex(pages.PageError, "branches"):
            pages.render_landing(self.manifest, {})
        for key in (
            "branch",
            "stamp",
            "headline",
            "stale",
            "newer",
            "shards",
            "totals",
            "tests",
            "lanes",
            "attention",
            "attention_total",
            "tests_attention",
            "tests_attention_total",
        ):
            broken = copy.deepcopy(self.latest)
            del broken["branches"][SLUG][key]
            with self.assertRaisesRegex(pages.PageError, key):
                pages.render_landing(self.manifest, broken)
        for key in ("id", "stamp", "n", "lanes", "superseded_by", "reason"):
            broken = copy.deepcopy(self.manifest)
            del broken["shards"][0][key]
            with self.assertRaisesRegex(pages.PageError, key):
                pages.render_landing(broken, self.latest)
        with self.assertRaises(pages.PageError):
            pages.render_landing(None, None)  # type: ignore[arg-type]

    def test_hostile_strings(self) -> None:
        for keep in (frozenset(), frozenset({"status", "headline"})):
            manifest = poison(self.manifest, keep)
            latest = poison(self.latest, keep)
            page = pages.render_landing(manifest, latest)
            audit = self.audit(page)
            self.assertIn(html_escape(HOSTILE), page)
            self.assertEqual(
                sorted(set(audit.hrefs)),
                [
                    "../data/bench/v1/latest.json",
                    "../data/bench/v1/manifest.json",
                    "../index.html",
                    "../wheels/index.html",
                ],
            )
            for name, values in audit.tags:
                self.assertNotIn("script", name)
                if "title" in values:
                    self.assertTrue(
                        str(values["title"]).startswith(("pass ", "superseded by "))
                    )

    def test_hostile_slugs_ids_and_lane_keys(self) -> None:
        bad_id = "20261120T204631Z-a/../../x-1.a1"
        entry = self.latest["branches"].pop(SLUG)
        entry["branch"] = "<img src=x onerror=alert(1)>"
        entry["shards"] = [bad_id]
        entry["attention"][0]["shard"] = "javascript:alert(1)"
        entry["attention"][0]["case_key"] = 'k"><script>x</script>'
        entry["lanes"][0]["shard"] = "../../index"
        entry["lanes"][0]["lane"] = 'x" onfocus="y'
        self.latest["branches"]['s"><script>alert(1)</script>'] = entry
        first = self.manifest["shards"][0]
        first["id"] = bad_id
        first["lanes"]['evil"><script>/1'] = lane("complete", [1, 0, 0, 0, 0, 0, 0])
        first["superseded_by"] = {"vm2-net/10.1.0": '"><script>w</script>'}
        self.manifest["links"]["docs"] = "javascript:alert(1)"
        page, audit = self.render()
        self.assertNotIn("javascript", " ".join(audit.hrefs))
        self.assertNotIn(bad_id, " ".join(audit.hrefs))
        self.assertIn('<h2 id="branch-s---script-alert-1---script-">', page)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;</h2>", page)
        self.assertIn("<code>k&quot;&gt;&lt;script&gt;x&lt;/script&gt;</code>", page)
        self.assertIn("evil&quot;&gt;&lt;script&gt;<br>1</th>", page)
        self.assertIn(
            'title="superseded by &quot;&gt;&lt;script&gt;w&lt;/script&gt;; pass 25',
            page,
        )
        self.assertIn('<span class="st muted" title="superseded by', page)
        # With no usable shard id nothing links to a run page.
        self.assertEqual(
            sorted({h.split("#")[0] for h in audit.hrefs if h.startswith("runs/")}),
            [f"runs/{SHARD}.html"],
        )
        self.assertIn('<p class="verdict fail">20 of 307', page)


if __name__ == "__main__":
    unittest.main()
