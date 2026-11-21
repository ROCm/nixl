# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of amd_bench_publish.py.

    python3 -m unittest discover -s .ci/scripts/tests -v

The fixture under fixtures/bench_publish/collected is the collected artifact
of the first real bench run, less its logs. Every other artifact is derived
from it at run time. The tests that need git work in a throwaway repository
and with an environment in which neither the user's git configuration nor a
surrounding hook can be seen.
"""

from __future__ import annotations

import contextlib
import copy
import gzip
import hashlib
import html
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from typing import Any, Callable, Iterable, Optional
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_lib as lib  # noqa: E402
import amd_bench_pages as pages  # noqa: E402
import amd_bench_publish as publish  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
COLLECTED = HERE / "fixtures" / "bench_publish" / "collected"
STAMP = "20261120T204631Z"
SLUG = "for-upstream-feat-rocm-wheels"
RUN = "44820022442"
LATER_RUN = "44820099999"
ID = f"{STAMP}-{SLUG}-{RUN}.a1"
DATA = "data/bench/v1"
SHARD_DIR = f"{DATA}/night/{STAMP}/{SLUG}/{RUN}.a1"
NOW = "2026-11-21T00:10:00Z"
MATRIX_FILE = next(COLLECTED.glob("matrix-*.json"))
MATRIX_SHA = MATRIX_FILE.stem[len("matrix-") :]
VM2 = "vm2-net/10.1.0"
BLOCKED_LANES = ("vm1-pcie/none", "vm1-s3/none", "vm2-lfverbs/none")
KINDS = ("runs", "cases", "samples", "counters", "tests")
OUT_KEYS = ["changed", "generation", "generation_moved", "shards", "bytes_added"]
OTHERS = {
    ".nojekyll": b"",
    "index.html": b"<!doctype html><title>nightly</title>\n",
    "data/nightly/index.json": b'{"runs": []}\n',
    "data/nightly/20261120T033018Z/summary.json": b'{"schema": 1}\n',
    "wheels/index.html": b"<!doctype html><title>wheels</title>\n",
    "wheels/main-amd/index.json": b'{"schema": 1}\n',
}
GIT = shutil.which("git")

ROOT = pathlib.Path()
PLAIN_SITE = pathlib.Path()
ENV: dict[str, str] = {}
_CLEANUP: list[tempfile.TemporaryDirectory[str]] = []
_RECORDS: list[dict[str, Any]] = []


def fake(*parts: str) -> str:
    """A secret-looking string, assembled so that no scanner sees it in here."""
    return "".join(parts)


AWS_KEY = fake("AKIA", "ABCDEFGHIJKLMNOP")
PLAIN = fake("hunter", "2-value")
UNSTABLE = fake("MY_AU", "TH=", PLAIN)


def scrubbed(home: pathlib.Path) -> dict[str, str]:
    """An environment in which git sees no configuration but its defaults."""
    env = {key: value for key, value in os.environ.items() if key[:4] != "GIT_"}
    who = {"NAME": "Bench Test", "EMAIL": "bench@example.invalid"}
    for role in ("AUTHOR", "COMMITTER"):
        env[f"GIT_{role}_DATE"] = "2026-11-21T00:00:00Z"
        for key, value in who.items():
            env[f"GIT_{role}_{key}"] = value
    env.update(
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
    )
    return env


class Result:
    def __init__(self, code: int, stdout: str, stderr: str) -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.report: dict[str, Any] = {}
        self.out: dict[str, str] = {}
        self.out_lines: list[str] = []
        self.stage = pathlib.Path()
        self.summary = pathlib.Path()


def tool(*args: Any, env: Optional[dict[str, str]] = None, stdin: str = "") -> Result:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        stack.enter_context(mock.patch.dict(os.environ, env or ENV, clear=True))
        stack.enter_context(mock.patch.object(sys, "stdin", io.StringIO(stdin)))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        try:
            code = publish.main([str(arg) for arg in args])
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 2
    return Result(code, out.getvalue(), err.getvalue())


def git(site: pathlib.Path, *args: str, env: Optional[dict[str, str]] = None) -> str:
    done = subprocess.run(
        ["git", "-C", str(site), *args],
        env=env or ENV,
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout


def snapshot(site: pathlib.Path) -> dict[str, bytes]:
    return {
        path.relative_to(site).as_posix(): path.read_bytes()
        for path in sorted(site.rglob("*"))
        if path.is_file() and ".git" not in path.relative_to(site).parts
    }


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_json(path: pathlib.Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest(site: pathlib.Path) -> dict[str, Any]:
    doc: dict[str, Any] = load_json(site / DATA / "manifest.json")
    return doc


def entry(site: pathlib.Path, shard_id: str) -> dict[str, Any]:
    (found,) = [item for item in manifest(site)["shards"] if item["id"] == shard_id]
    return dict(found)


def index(site: pathlib.Path, shard_id: str = ID) -> dict[str, Any]:
    doc: dict[str, Any] = load_json(
        site / DATA / entry(site, shard_id)["path"] / "index.json"
    )
    return doc


def branch(site: pathlib.Path, slug: str = SLUG) -> dict[str, Any]:
    doc: dict[str, Any] = load_json(site / DATA / "latest.json")["branches"][slug]
    return doc


def fixture_records() -> list[dict[str, Any]]:
    if not _RECORDS:
        raw = next(COLLECTED.glob("bench-*.ndjson.gz")).read_bytes()
        text = gzip.decompress(raw).decode("ascii")
        _RECORDS.extend(json.loads(line) for line in text.splitlines())
    return copy.deepcopy(_RECORDS)


def pack(records: Iterable[dict[str, Any]]) -> bytes:
    text = "".join(json.dumps(r, separators=(",", ":")) + "\n" for r in records)
    return gzip.compress(text.encode("ascii"), compresslevel=9, mtime=0)


def build(
    directory: pathlib.Path,
    *,
    stamp: str = STAMP,
    slug: str = SLUG,
    run: str = RUN,
    attempt: int = 1,
    lanes: Optional[tuple[str, ...]] = None,
    change: Optional[Callable[[list[dict[str, Any]]], None]] = None,
    matrix: bool = True,
    name: Optional[str] = None,
    **ident: Any,
) -> str:
    """An artifact derived from the fixture; returns the id of its shard.

    The uids are recomputed, as a lane job of that bench run would have made
    them. 'lanes' keeps the runs of those lanes and the blocked ones.
    """
    records = []
    runs: dict[str, str] = {}
    cases: dict[str, str] = {}
    for record in fixture_records():
        kind = record["kind"]
        if kind == "run":
            if lanes is not None and not (
                record["lane"] in lanes or record["status"] == "blocked"
            ):
                continue
            record.update(stamp=stamp, slug=slug, bench_run_id=run, **ident)
            record["run_attempt"] = attempt
            uid = lib.run_uid(
                record["repo"], run, attempt, record["lane"], record["rocm_version"]
            )
            runs[record["run_uid"]] = record["run_uid"] = uid
        elif kind in ("case", "test"):
            if record["run_uid"] not in runs:
                continue
            record["run_uid"] = runs[record["run_uid"]]
            if kind == "case":
                uid = lib.case_uid(
                    record["run_uid"],
                    record["case_key"],
                    record["variant"],
                    record["round"],
                )
                cases[record["case_uid"]] = record["case_uid"] = uid
        else:
            if record["case_uid"] not in cases:
                continue
            record["case_uid"] = cases[record["case_uid"]]
        records.append(record)
    if change is not None:
        change(records)
    directory.mkdir(parents=True, exist_ok=True)
    shard_id = f"{stamp}-{slug}-{run}.a{attempt}"
    (directory / (name or f"bench-{shard_id}.ndjson.gz")).write_bytes(pack(records))
    if matrix:
        shutil.copy(MATRIX_FILE, directory)
    return shard_id


def of_kind(records: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [record for record in records if record["kind"] == kind]


def vm2_run(records: list[dict[str, Any]]) -> dict[str, Any]:
    (run,) = [r for r in of_kind(records, "run") if r["lane"] == "vm2-net"]
    return run


def tarball(
    path: pathlib.Path,
    members: dict[str, Optional[bytes]],
    owner: str = "",
    trailer: bytes = b"",
) -> None:
    """A gzip tar as the collector writes it; None makes a symbolic link."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.uname = owner
            if data is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "elsewhere"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    data = buffer.getvalue() + trailer
    path.write_bytes(gzip.compress(data, compresslevel=9, mtime=0))


def setUpModule() -> None:
    global ROOT, PLAIN_SITE
    scratch = tempfile.TemporaryDirectory(prefix="amd-bench-publish-test-")
    _CLEANUP.append(scratch)
    ROOT = pathlib.Path(scratch.name)
    (ROOT / "home").mkdir()
    ENV.update(scrubbed(ROOT / "home"))
    PLAIN_SITE = ROOT / "plain"
    PLAIN_SITE.mkdir()
    done = tool("publish", "--site", PLAIN_SITE, "--input", COLLECTED, "--now", NOW)
    if done.code != 0:
        raise AssertionError(f"the fixture does not publish:\n{done.stderr}")


def tearDownModule() -> None:
    for scratch in _CLEANUP:
        scratch.cleanup()
    _CLEANUP.clear()


class Case(unittest.TestCase):
    maxDiff = None

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory(prefix="amd-bench-publish-test-")
        self.addCleanup(scratch.cleanup)
        self.tmp = pathlib.Path(scratch.name)
        self.calls = 0

    def site(self, published: bool = True) -> pathlib.Path:
        """A plain directory: empty, or holding the published fixture."""
        self.calls += 1
        site = self.tmp / f"site-{self.calls}"
        if published:
            shutil.copytree(PLAIN_SITE, site)
        else:
            site.mkdir()
        return site

    def git_site(self, published: bool = False) -> pathlib.Path:
        """A repository shaped like gh-pages, with the other writers' files."""
        self.calls += 1
        site = self.tmp / f"pages-{self.calls}"
        site.mkdir()
        git(site, "init", "-q", "-b", "gh-pages")
        for name, data in OTHERS.items():
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_bytes(data)
        git(site, "add", "-A")
        git(site, "commit", "-q", "-m", "seed")
        if published:
            for part in (DATA, "bench"):
                shutil.copytree(PLAIN_SITE / part, site / part)
            self.stage(site)
            git(site, "commit", "-q", "-m", "bench: publish")
        return site

    def stage(self, site: pathlib.Path) -> None:
        git(site, "add", "-A", "--", DATA, "bench")

    def publish(self, site: pathlib.Path, *inputs: pathlib.Path, **more: Any) -> Result:
        self.calls += 1
        report = self.tmp / f"report-{self.calls}.json"
        out = self.tmp / f"out-{self.calls}.env"
        args: list[Any] = ["publish", "--site", site, "--report", report]
        args += ["--output", out]
        if "now" not in more:
            args += ["--now", NOW]
        for directory in inputs:
            args += ["--input", directory]
        for key, value in more.items():
            flag = "--" + key.replace("_", "-")
            if value is True:
                args.append(flag)
            else:
                for item in value if isinstance(value, list) else [value]:
                    args += [flag, item]
        done = tool(*args)
        if report.is_file():
            done.report = load_json(report)
        if out.is_file():
            done.out_lines = out.read_text().splitlines()
            done.out = dict(line.split("=", 1) for line in done.out_lines)
        return done

    def check(self, site: pathlib.Path, *more: str) -> Result:
        self.calls += 1
        out = self.tmp / f"staged-{self.calls}.env"
        done = tool("check-staged", "--site", site, "--output", out, *more)
        if out.is_file():
            done.out = dict(line.split("=", 1) for line in out.read_text().splitlines())
        return done

    def inspect(
        self, *inputs: pathlib.Path, stage: bool = False, repo: Optional[str] = None
    ) -> Result:
        self.calls += 1
        plan = self.tmp / f"plan-{self.calls}.json"
        args: list[Any] = ["inspect", "--out", plan]
        if repo is not None:
            args += ["--repo", repo]
        for directory in inputs:
            args += ["--input", directory]
        staged = self.tmp / f"stage-{self.calls}"
        summary = self.tmp / f"summary-{self.calls}.md"
        if stage:
            args += ["--stage", staged]
        args += ["--summary-out", summary]
        done = tool(*args)
        done.stage, done.summary = staged / "bench-raw-2026-11", summary
        if plan.is_file():
            done.report = load_json(plan)
        return done

    def collected(self, name: str = "collected") -> pathlib.Path:
        """A copy of the fixture artifact that a test may add files to."""
        target = self.tmp / name
        shutil.copytree(COLLECTED, target)
        return target


class Names(unittest.TestCase):
    def test_an_id_is_parsed_with_a_greedy_slug(self) -> None:
        name = publish.parse_id(ID)
        assert name is not None
        self.assertEqual((name.stamp, name.slug, name.run), (STAMP, SLUG, RUN))
        self.assertEqual((name.attempt, name.tag), (1, "bench-raw-2026-11"))
        name = publish.parse_id(f"{STAMP}-a-1-2-3.a12")
        assert name is not None
        self.assertEqual((name.slug, name.run, name.order), ("a-1-2", "3", (3, 12)))

    def test_what_is_not_an_id(self) -> None:
        wide = "".join(chr(ord(c) + 0xFEE0) if c.isdigit() else c for c in STAMP)
        for text in (
            f"{STAMP}-..-1.a1",
            f"{STAMP}-.x-1.a1",
            f"{STAMP}-a/b-1.a1",
            f"{STAMP}-x-1.a0",
            f"{STAMP}-x-1.a01",
            f"{STAMP}-x-01.a1",
            f"{STAMP}-x-1.a1\n",
            f"{STAMP}-{'x' * 101}-1.a1",
            "20261340T204631Z-x-1.a1",
            f"{wide}-x-1.a1",
            f"{STAMP}-x-1",
        ):
            with self.subTest(text=text):
                self.assertIsNone(publish.parse_id(text))

    def test_the_allowlist_is_the_contracted_paths(self) -> None:
        good = (
            f"{DATA}/manifest.json",
            f"{DATA}/latest.json",
            f"{DATA}/matrix/{MATRIX_SHA}.json",
            f"{SHARD_DIR}/index.json",
            f"{SHARD_DIR}/cases.ndjson.gz",
            f"{DATA}/weekly/{STAMP}/{SLUG}/{RUN}.a2/tests.ndjson.gz",
            "bench/index.html",
            f"bench/runs/{ID}.html",
        )
        bad = (
            "index.html",
            ".nojekyll",
            "data/nightly/index.json",
            "wheels/index.html",
            "perf/index.html",
            f"{DATA}/annotations.json",
            f"{DATA}/month/2026-11/cases.parquet",
            f"{SHARD_DIR}/cases.parquet",
            f"{SHARD_DIR}/other.ndjson.gz",
            f"{DATA}/night/{STAMP}/../{RUN}.a1/index.json",
            f"{DATA}/matrix/{MATRIX_SHA}.json.tmp",
            f"bench/runs/{ID}.html\n",
            "bench/runs/x.html",
            "bench/other.html",
        )
        for path in good:
            self.assertIsNotNone(publish.STAGED_RE.fullmatch(path), path)
        for path in bad:
            self.assertIsNone(publish.STAGED_RE.fullmatch(path), path)

    def test_gunzip_stops_at_the_limit(self) -> None:
        data = gzip.compress(b"x" * 5000)
        self.assertEqual(len(publish.gunzip(data, 5000)), 5000)
        for bad, why in (
            (data, "inflates"),
            (data[:20], "truncated"),
            (b"plain", "not a gzip"),
            (b"", "empty"),
        ):
            with self.assertRaisesRegex(publish.Invalid, why):
                publish.gunzip(bad, 4999 if bad is data else 5000)
        self.assertEqual(publish.gunzip(data + gzip.compress(b"y"), 5001)[-1:], b"y")


class FirstPublish(Case):
    def test_the_fixture_uids_are_the_ones_the_library_derives(self) -> None:
        self.assertEqual(build(self.tmp / "same"), ID)
        (made,) = (self.tmp / "same").glob("bench-*.ndjson.gz")
        self.assertEqual(
            gzip.decompress(made.read_bytes()),
            gzip.decompress(next(COLLECTED.glob("bench-*.ndjson.gz")).read_bytes()),
        )

    def test_only_the_contracted_paths_are_written(self) -> None:
        site = self.site()
        self.assertEqual(
            sorted(snapshot(site)),
            sorted(
                [
                    "bench/index.html",
                    f"bench/runs/{ID}.html",
                    f"{DATA}/latest.json",
                    f"{DATA}/manifest.json",
                    f"{DATA}/matrix/{MATRIX_SHA}.json",
                    f"{SHARD_DIR}/index.json",
                    *(f"{SHARD_DIR}/{kind}.ndjson.gz" for kind in KINDS),
                ]
            ),
        )
        self.assertEqual(
            (site / DATA / "matrix" / f"{MATRIX_SHA}.json").read_bytes(),
            MATRIX_FILE.read_bytes(),
        )

    def test_the_manifest_says_what_the_run_was(self) -> None:
        site = self.site()
        doc = manifest(site)
        self.assertEqual((doc["schema"], doc["generation"]), (1, 1))
        self.assertEqual((doc["repo"], doc["updated_at"]), ("ROCm/nixl", NOW))
        self.assertEqual(doc["statuses"], list(lib.CASE_STATUSES))
        self.assertEqual(doc["test_statuses"], list(lib.TEST_STATUSES))
        self.assertEqual(doc["quarantine"], [])
        (shard,) = doc["shards"]
        self.assertEqual(shard["id"], ID)
        self.assertEqual(shard["path"], SHARD_DIR[len(DATA) + 1 :])
        self.assertEqual((shard["tier"], shard["reason"]), ("nightly", "dispatch"))
        self.assertEqual(shard["branch"], "for-upstream/feat/rocm-wheels")
        self.assertEqual(shard["nightly_run_id"], "44814378049")
        self.assertEqual((shard["bench_run_id"], shard["run_attempt"]), (RUN, 1))
        self.assertEqual(shard["matrix_sha256"], MATRIX_SHA)
        self.assertEqual((shard["generation"], shard["published_at"]), (1, NOW))
        self.assertIs(shard["authoritative"], True)
        self.assertEqual(shard["rows"], 1655)
        self.assertEqual(shard["n"], [181, 16, 4, 0, 24, 82, 0])
        self.assertEqual(shard["tests"], [134, 0, 0, 0])
        self.assertEqual((shard["superseded_by"], shard["supersedes"]), ({}, []))
        self.assertEqual(
            {key: lane["n"] for key, lane in shard["lanes"].items()},
            {
                "bench-container/10.1.0": [26, 0, 0, 0, 8, 7, 0],
                "bench-container/10.2.0": [26, 0, 0, 0, 8, 7, 0],
                "bench-container/7.14.1": [26, 0, 0, 0, 8, 7, 0],
                "vm1-fabric/10.1.0": [24, 4, 0, 0, 0, 11, 0],
                "vm1-local/10.1.0": [38, 2, 0, 0, 0, 0, 0],
                "vm1-loop/10.1.0": [16, 4, 1, 0, 0, 0, 0],
                "vm1-pcie/none": [0, 0, 0, 0, 0, 36, 0],
                "vm1-s3/none": [0, 0, 0, 0, 0, 8, 0],
                "vm2-lfverbs/none": [0, 0, 0, 0, 0, 4, 0],
                VM2: [25, 6, 3, 0, 0, 2, 0],
            },
        )
        self.assertEqual(
            {lane["status"] for lane in shard["lanes"].values()},
            {"complete", "blocked"},
        )
        self.assertFalse(any("filtered" in lane for lane in shard["lanes"].values()))
        raw = next(COLLECTED.glob("bench-*.ndjson.gz")).read_bytes()
        self.assertEqual(
            shard["raw"],
            {
                "tag": "bench-raw-2026-11",
                "records": f"bench-{ID}.ndjson.gz",
                "bytes": len(raw),
                "sha256": sha256(raw),
                "logs": False,
                "collect": True,
            },
        )

    def test_every_listed_file_is_the_file_on_disk(self) -> None:
        site = self.site()
        shard = entry(site, ID)
        files = {item["name"]: item for item in shard["files"]}
        self.assertEqual(
            list(files), [f"{k}.ndjson.gz" for k in KINDS] + ["index.json"]
        )
        for name, item in files.items():
            data = (site / SHARD_DIR / name).read_bytes()
            self.assertEqual((item["bytes"], item["sha256"]), (len(data), sha256(data)))
        self.assertEqual(shard["bytes"], sum(item["bytes"] for item in files.values()))
        self.assertEqual(
            [files[f"{kind}.ndjson.gz"]["rows"] for kind in KINDS],
            [10, 307, 739, 465, 134],
        )
        # About 65 kB with CPython's zlib; another build may pack a little
        # differently, so only the order of magnitude is pinned.
        size = sum(files[f"{kind}.ndjson.gz"]["bytes"] for kind in KINDS)
        self.assertTrue(40000 < size < 80000, size)
        self.assertIsNone(files["index.json"]["rows"])

    def test_each_shard_file_holds_the_raw_records_of_its_kind_in_order(self) -> None:
        site = self.site()
        records = fixture_records()
        for kind in KINDS:
            data = gzip.decompress(
                (site / SHARD_DIR / f"{kind}.ndjson.gz").read_bytes()
            )
            lines = data.decode("ascii").splitlines()
            wanted = of_kind(records, kind[:-1])
            self.assertEqual([json.loads(line) for line in lines], wanted)
            self.assertEqual(
                [list(json.loads(line)) for line in lines[:3]],
                [list(lib.FIELDS[kind[:-1]])] * 3,
            )

    def test_the_manifest_is_one_line_per_shard(self) -> None:
        site = self.site()
        later = build(self.tmp / "later", run=LATER_RUN, lanes=("vm2-net",))
        self.assertEqual(self.publish(site, self.tmp / "later").code, 0)
        text = (site / DATA / "manifest.json").read_text(encoding="ascii")
        lines = text.splitlines()
        self.assertTrue(text.endswith("]\n}\n"))
        self.assertEqual(lines[0], "{")
        start = lines.index('"shards":[')
        keys = [json.loads("{" + line.rstrip(",") + "}") for line in lines[1:start]]
        self.assertEqual(
            [next(iter(item)) for item in keys],
            sorted(key for key in json.loads(text) if key != "shards"),
        )
        shards = lines[start + 1 : -2]
        self.assertEqual(
            [json.loads(line.rstrip(","))["id"] for line in shards], [later, ID]
        )
        self.assertTrue(shards[0].endswith(","))
        self.assertEqual(publish.manifest_text(json.loads(text)).decode("ascii"), text)

    def test_the_index_is_what_the_pages_need(self) -> None:
        doc = index(PLAIN_SITE)
        self.assertEqual(
            doc["totals"], dict(zip(lib.CASE_STATUSES, entry(PLAIN_SITE, ID)["n"]))
        )
        self.assertEqual(
            doc["tests"], {"pass": 134, "fail": 0, "skip": 0, "timeout": 0}
        )
        self.assertEqual(doc["shard"]["started_at"], "2026-11-20T21:25:44Z")
        self.assertEqual(doc["shard"]["finished_at"], "2026-11-20T21:45:43Z")
        self.assertEqual(doc["shard"]["nixlbench_version"], "1.6.0")
        self.assertEqual(
            [(lane["lane"], lane["rocm_version"]) for lane in doc["lanes"]],
            sorted((lane["lane"], lane["rocm_version"]) for lane in doc["lanes"]),
        )
        self.assertEqual(len(doc["lanes"]), 10)
        self.assertEqual(sum(lane["tests"][0] for lane in doc["lanes"]), 134)
        attention = doc["attention"]
        self.assertEqual(
            [sum(item["status"] == s for item in attention) for s in publish.SEVERITY],
            [0, 16, 4, 0, 24],
        )
        self.assertEqual(
            [item["status"] for item in attention],
            sorted((item["status"] for item in attention), key=publish.SEVERITY.index),
        )
        first = attention[0]
        self.assertEqual(first["case_key"], "vm1-fabric/ais/compat-allowed/WRITE/S")
        self.assertEqual((first["reason"], first["exit_init"]), ("consistency", 1))
        self.assertEqual(
            {item["reason"] for item in attention if item["status"] == "skip"}, {"env"}
        )
        self.assertEqual(doc["tests_attention"], [])
        self.assertEqual(len(doc["blocked"]), 16)
        self.assertEqual(sum(group["cases"] for group in doc["blocked"]), 82)
        self.assertEqual(len(doc["ratios"]), 37)
        self.assertEqual(sum(r["status"] == "ok" for r in doc["ratios"]), 34)
        self.assertEqual(
            sorted(r["why"] for r in doc["ratios"] if r["status"] != "ok"),
            ["den timeout", "num blocked", "num timeout"],
        )
        point = next(r for r in doc["ratios"] if r["status"] == "ok")["points"][0]
        self.assertEqual(point[4], round(point[2] / point[3], 4))
        self.assertEqual(
            doc["collect"], {"rejected": 0, "warnings": [], "required_failures": []}
        )
        self.assertEqual(doc["warnings"], [])
        self.assertNotIn("files", doc)

    def test_links_are_release_urls_and_data_root_relative_paths(self) -> None:
        links = index(PLAIN_SITE)["links"]
        base = "https://github.com/ROCm/nixl"
        assets = f"{base}/releases/download/bench-raw-2026-11"
        self.assertEqual(links["actions_run"], f"{base}/actions/runs/{RUN}/attempts/1")
        self.assertEqual(links["source_run"], f"{base}/actions/runs/44814378049")
        self.assertEqual(
            links["commit"], f"{base}/commit/{entry(PLAIN_SITE, ID)['sha']}"
        )
        self.assertEqual(links["raw_records"], f"{assets}/bench-{ID}.ndjson.gz")
        self.assertEqual(links["raw_collect"], f"{assets}/bench-{ID}.collect.json")
        self.assertIsNone(links["raw_logs"])
        self.assertEqual(links["docs"], f"{base}/blob/main-amd/.ci/docs/amd-bench.md")
        self.assertEqual(links["data"], SHARD_DIR[len(DATA) + 1 :])
        self.assertEqual(links["matrix"], f"matrix/{MATRIX_SHA}.json")
        for key in ("data", "matrix"):
            self.assertFalse(links[key].startswith("data/"), key)
            self.assertTrue((PLAIN_SITE / DATA / links[key]).exists(), key)

    def test_the_pages_group_the_real_failures_as_stated(self) -> None:
        doc = index(PLAIN_SITE)
        groups = pages.failure_groups(doc["attention"])
        self.assertEqual([group["count"] for group in groups], [10, 3, 2, 2, 2, 1])
        blocked = pages.blocked_groups(doc["blocked"])
        self.assertEqual(len(blocked), 10)
        self.assertEqual(sum(group["cases"] for group in blocked), 68)
        page = (PLAIN_SITE / pages.run_page_path(ID)).read_text(encoding="utf-8")
        failing = [a for a in doc["attention"] if a["status"] in publish.FAILING]
        self.assertEqual(len(failing), 20)
        for item in failing:
            self.assertIn(html.escape(item["case_key"]), page)
        self.assertEqual(page, pages.render_run(doc))

    def test_latest_is_a_manual_headline_with_slim_failures(self) -> None:
        latest = load_json(PLAIN_SITE / DATA / "latest.json")
        self.assertEqual((latest["schema"], latest["generation"]), (1, 1))
        self.assertEqual(list(latest["branches"]), [SLUG])
        head = latest["branches"][SLUG]
        self.assertEqual(
            (head["headline"], head["stale"], head["newer"]), ("manual", False, None)
        )
        self.assertEqual((head["stamp"], head["shards"]), (STAMP, [ID]))
        self.assertEqual(head["branch"], "for-upstream/feat/rocm-wheels")
        self.assertEqual(head["totals"], index(PLAIN_SITE)["totals"])
        self.assertEqual(head["tests"]["pass"], 134)
        self.assertEqual((head["attention_total"], len(head["attention"])), (20, 20))
        self.assertEqual(head["tests_attention_total"], 0)
        for item in head["attention"]:
            self.assertEqual(
                sorted(item),
                sorted(
                    "shard lane rocm_version case_key variant round status reason "
                    "required error_excerpt".split()
                ),
            )
            self.assertLessEqual(len(item["error_excerpt"]), 200)
            self.assertIn(item["status"], publish.FAILING)
        self.assertEqual(
            [item["case_key"] for item in head["attention"]],
            [
                item["case_key"]
                for item in index(PLAIN_SITE)["attention"]
                if item["status"] in publish.FAILING
            ],
        )
        self.assertEqual(len(head["lanes"]), 10)
        self.assertEqual({lane["shard"] for lane in head["lanes"]}, {ID})
        self.assertEqual(len(head["ratios"]), 37)

    def test_the_same_now_gives_the_same_tree(self) -> None:
        site = self.site(published=False)
        done = self.publish(site, COLLECTED)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(snapshot(site), snapshot(PLAIN_SITE))
        size = sum(len(data) for data in snapshot(site).values())
        self.assertEqual(done.report["bytes_added"], size)
        self.assertEqual(sum(done.report["tree_bytes"].values()), size)
        self.assertEqual(done.out["bytes_added"], str(size))
        self.assertEqual(done.report["published"][0]["id"], ID)
        self.assertEqual(done.report["published"][0]["bytes"], entry(site, ID)["bytes"])
        self.assertEqual(
            {item["path"]: item["sha256"] for item in done.report["verify"]},
            {
                path: sha256((site / path).read_bytes())
                for path in (
                    "bench/index.html",
                    f"{DATA}/latest.json",
                    f"bench/runs/{ID}.html",
                )
            },
        )

    def test_bytes_added_is_the_growth_of_the_tree(self) -> None:
        site = self.site()
        before = sum(len(data) for data in snapshot(site).values())
        build(self.tmp / "later", run=LATER_RUN, lanes=("vm2-net",))
        done = self.publish(site, self.tmp / "later")
        after = sum(len(data) for data in snapshot(site).values())
        self.assertEqual(done.report["bytes_added"], after - before)
        self.assertEqual(done.out["generation"], "2")
        self.assertEqual(done.out["generation_moved"], "true")

    def test_an_empty_directory_and_no_input_stay_empty(self) -> None:
        site = self.site(published=False)
        done = self.publish(site)
        self.assertEqual((done.code, done.out["changed"]), (0, "false"))
        self.assertEqual(snapshot(site), {})


@unittest.skipUnless(GIT, "git is not installed")
class Guard(Case):
    def test_a_publish_into_the_site_passes_the_guard(self) -> None:
        site = self.git_site()
        done = self.publish(site, COLLECTED)
        self.assertEqual(done.code, 0, done.stderr)
        for name, data in OTHERS.items():
            self.assertEqual((site / name).read_bytes(), data, name)
        self.assertEqual(
            {path: data for path, data in snapshot(site).items() if path not in OTHERS},
            snapshot(PLAIN_SITE),
        )
        self.stage(site)
        staged = git(site, "diff", "--cached", "--name-status").splitlines()
        self.assertEqual({line.split("\t")[0] for line in staged}, {"A"})
        self.assertEqual(len(staged), len(snapshot(PLAIN_SITE)))
        checked = self.check(site)
        self.assertEqual(checked.code, 0, checked.stderr)
        self.assertEqual(checked.out["staged"], str(len(staged)))
        self.assertGreater(int(checked.out["disk_bytes"]), 40000)
        self.assertEqual(list(checked.out), ["staged", "disk_bytes"])

    def test_a_second_publish_changes_nothing(self) -> None:
        site = self.git_site(published=True)
        done = self.publish(site, COLLECTED, now="2026-11-25T00:00:00Z")
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(git(site, "status", "--porcelain"), "")
        self.assertEqual(done.out["changed"], "false")
        self.assertEqual(done.out["generation"], "1")
        self.assertEqual(done.out["generation_moved"], "false")
        self.assertEqual(done.out["bytes_added"], "0")
        self.assertEqual((done.out["shards"], done.out["verify"]), ("", ""))
        self.assertEqual(
            [line.split("=")[0] for line in done.out_lines], OUT_KEYS + ["verify"]
        )
        self.assertEqual(done.report["noop"], [ID])
        self.assertEqual(done.report["published"], [])
        self.stage(site)
        checked = self.check(site)
        self.assertEqual((checked.code, checked.out["staged"]), (0, "0"))

    def test_the_output_file_is_written_anew(self) -> None:
        site = self.git_site(published=True)
        out = self.tmp / "out.env"
        out.write_text("changed=true\nstale=1\n" * 3)
        tool("publish", "--site", site, "--input", COLLECTED, "--output", out)
        self.assertEqual(
            [line.split("=")[0] for line in out.read_text().splitlines()],
            OUT_KEYS + ["verify"],
        )
        staged = self.tmp / "staged.env"
        staged.write_text("staged=9\nstaged=9\n")
        tool("check-staged", "--site", site, "--output", staged)
        self.assertEqual(staged.read_text(), "staged=0\ndisk_bytes=0\n")

    def test_what_the_guard_refuses(self) -> None:
        base = self.git_site(published=True)

        def edit(site: pathlib.Path, name: str, add: bool = True) -> None:
            with open(site / name, "ab") as stream:
                stream.write(b"x")
            if add:
                git(site, "add", "--", name)

        def remove(site: pathlib.Path, name: str) -> None:
            git(site, "rm", "-q", "--", name)

        def stray(site: pathlib.Path, name: str, add: bool = False) -> None:
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_bytes(b"x")
            if add:
                git(site, "add", "--", name)

        cases: dict[str, tuple[Callable[[pathlib.Path], None], str]] = {
            "an unstaged edit of the root page": (
                lambda site: edit(site, "index.html", add=False),
                "index.html: outside",
            ),
            "a staged edit of the root page": (
                lambda site: edit(site, "index.html"),
                "M index.html: not a path",
            ),
            "an edit of a wheel index": (
                lambda site: edit(site, "wheels/main-amd/index.json"),
                "wheels/main-amd/index.json: not a path",
            ),
            "a deleted nightly summary": (
                lambda site: remove(site, "data/nightly/index.json"),
                "D data/nightly/index.json",
            ),
            "a deleted run page": (
                lambda site: remove(site, f"bench/runs/{ID}.html"),
                "only additions",
            ),
            "a modified shard data file": (
                lambda site: edit(site, f"{SHARD_DIR}/cases.ndjson.gz"),
                "never modified",
            ),
            "a modified matrix": (
                lambda site: edit(site, f"{DATA}/matrix/{MATRIX_SHA}.json"),
                "never modified",
            ),
            "a modified index without --rerender": (
                lambda site: edit(site, f"{SHARD_DIR}/index.json"),
                "never modified",
            ),
            "a stray file at the top": (
                lambda site: stray(site, "stray.txt"),
                "stray.txt: outside",
            ),
            "a stray file under perf": (
                lambda site: stray(site, "perf/index.html"),
                "perf/index.html: outside",
            ),
            "an unstaged file in the own tree": (
                lambda site: stray(site, f"{DATA}/latest.json.tmp"),
                "latest.json.tmp: not staged",
            ),
            "a staged file that is not contracted": (
                lambda site: stray(site, f"{DATA}/annotations.json", add=True),
                "annotations.json: not a path",
            ),
        }
        for number, (label, (plant, text)) in enumerate(cases.items()):
            with self.subTest(label):
                site = self.tmp / f"planted-{number}"
                shutil.copytree(base, site)
                plant(site)
                checked = self.check(site)
                self.assertEqual(checked.code, 1, checked.stderr)
                self.assertIn(text, checked.stderr)

    def test_a_file_that_git_ignores_is_not_left_out_silently(self) -> None:
        site = self.git_site()
        (site / ".git" / "info" / "exclude").write_text("*.gz\n")
        self.assertEqual(self.publish(site, COLLECTED).code, 0)
        self.stage(site)
        staged = git(site, "diff", "--cached", "--name-only").splitlines()
        self.assertFalse([name for name in staged if name.endswith(".gz")])
        checked = self.check(site)
        self.assertEqual(checked.code, 1, checked.stderr)
        self.assertEqual(checked.stderr.count("ignored by git, so not staged"), 5)
        # Staged as the workflow stages, nothing is left out.
        git(site, "add", "-A", "-f", "--", DATA, "bench")
        self.assertEqual(self.check(site).code, 0)

    def test_a_symbolic_link_is_not_a_plain_file(self) -> None:
        site = self.git_site(published=True)
        os.symlink("../../../index.html", site / DATA / "matrix" / f"{'0' * 64}.json")
        self.stage(site)
        checked = self.check(site)
        self.assertEqual(checked.code, 1)
        self.assertIn("mode 120000", checked.stderr)

    def test_mutable_files_may_change_and_derived_ones_with_rerender(self) -> None:
        site = self.git_site(published=True)
        for name in (
            f"{DATA}/manifest.json",
            f"{DATA}/latest.json",
            "bench/index.html",
        ):
            with open(site / name, "ab") as stream:
                stream.write(b"\n")
        self.stage(site)
        self.assertEqual(self.check(site).code, 0)
        for name in (f"{SHARD_DIR}/index.json", f"bench/runs/{ID}.html"):
            with open(site / name, "ab") as stream:
                stream.write(b"\n")
        self.stage(site)
        self.assertEqual(self.check(site).code, 1)
        checked = self.check(site, "--rerender")
        self.assertEqual((checked.code, checked.out["staged"]), (0, "5"))

    def test_the_site_must_be_the_top_of_its_own_work_tree(self) -> None:
        site = self.git_site(published=True)
        for bad in (site / "data", self.tmp / "missing"):
            checked = self.check(bad)
            self.assertEqual(checked.code, 2, checked.stderr)
        plain = self.site()
        self.assertEqual(self.check(plain).code, 2)

    def test_git_location_variables_do_not_redirect_the_guard(self) -> None:
        site = self.git_site(published=True)
        decoy = self.git_site()
        (decoy / "planted.txt").write_bytes(b"x")
        git(decoy, "add", "planted.txt")
        env = dict(
            ENV,
            GIT_DIR=str(decoy / ".git"),
            GIT_INDEX_FILE=str(decoy / ".git" / "index"),
            GIT_WORK_TREE=str(decoy),
        )
        out = self.tmp / "staged.env"
        clean = tool("check-staged", "--site", site, "--output", out, env=env)
        self.assertEqual(clean.code, 0, clean.stderr)
        self.assertEqual(out.read_text(), "staged=0\ndisk_bytes=0\n")
        with open(site / "index.html", "ab") as stream:
            stream.write(b"x")
        dirty = tool("check-staged", "--site", site, env=env)
        self.assertEqual(dirty.code, 1)
        self.assertIn("index.html", dirty.stderr)
        self.assertNotIn("planted.txt", dirty.stderr)
        self.assertEqual(git(decoy, "status", "--porcelain"), "A  planted.txt\n")


class Supersession(Case):
    def test_a_later_run_of_one_lane_takes_that_lane_only(self) -> None:
        site = self.site()
        later = build(self.tmp / "later", run=LATER_RUN, lanes=("vm2-net",))
        done = self.publish(site, self.tmp / "later")
        self.assertEqual(done.code, 0, done.stderr)
        old, new = entry(site, ID), entry(site, later)
        self.assertEqual(old["superseded_by"], {VM2: later})
        self.assertEqual(old["supersedes"], [later])
        # Blocked, same matrix: the first run that found them blocked keeps them.
        self.assertEqual(new["superseded_by"], dict.fromkeys(BLOCKED_LANES, ID))
        self.assertEqual(new["supersedes"], [ID])
        self.assertEqual([s["id"] for s in manifest(site)["shards"]], [later, ID])
        self.assertEqual(
            done.report["superseded"], {ID: {VM2: later}, later: new["superseded_by"]}
        )
        self.assertEqual(old["generation"], 1)
        self.assertEqual(new["generation"], 2)
        head = branch(site)
        self.assertEqual(head["shards"], [ID, later])
        self.assertEqual(
            {lane["lane"]: lane["shard"] for lane in head["lanes"]}["vm2-net"], later
        )
        self.assertEqual(len(head["lanes"]), 10)
        self.assertEqual(head["attention_total"], 20)
        self.assertEqual(head["totals"], index(PLAIN_SITE)["totals"])

    def test_a_pr_tier_run_takes_no_nightly_lane(self) -> None:
        site = self.site()
        later = build(self.tmp / "pr", run=LATER_RUN, tier="pr")
        self.assertEqual(self.publish(site, self.tmp / "pr").code, 0)
        old, new = entry(site, ID), entry(site, later)
        self.assertEqual((new["tier"], new["path"].split("/")[0]), ("pr", "night"))
        self.assertEqual(old["superseded_by"], {})
        self.assertEqual(new["superseded_by"], dict.fromkeys(old["lanes"], ID))
        self.assertEqual(branch(site)["shards"], [ID])

    def test_missing_filtered_and_another_night_take_nothing(self) -> None:
        def missing(records: list[dict[str, Any]]) -> None:
            vm2_run(records)["status"] = "missing"

        def filtered(records: list[dict[str, Any]]) -> None:
            uid = vm2_run(records)["run_uid"]
            case = next(c for c in of_kind(records, "case") if c["run_uid"] == uid)
            case.update(status="skip", skip_reason="filter")

        site = self.site()
        ids = [
            build(
                self.tmp / "m", run="44830000001", lanes=("vm2-net",), change=missing
            ),
            build(
                self.tmp / "f", run="44840000002", lanes=("vm2-net",), change=filtered
            ),
            build(
                self.tmp / "n",
                run="44850000003",
                lanes=("vm2-net",),
                stamp="20261121T204631Z",
            ),
        ]
        done = self.publish(site, self.tmp / "m", self.tmp / "f", self.tmp / "n")
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(entry(site, ID)["superseded_by"], {})
        self.assertEqual(entry(site, ids[0])["lanes"][VM2]["status"], "missing")
        self.assertIs(entry(site, ids[1])["lanes"][VM2]["filtered"], True)
        for shard_id in ids[:2]:
            self.assertEqual(entry(site, shard_id)["superseded_by"][VM2], ID)
        other = entry(site, ids[2])
        self.assertEqual((other["superseded_by"], other["supersedes"]), ({}, []))
        self.assertEqual(sorted(entry(site, ID)["supersedes"]), sorted(ids[:2]))
        self.assertNotIn(
            "filter", {item["reason"] for item in index(site, ids[1])["attention"]}
        )

    def test_supersession_is_a_pure_function_of_the_night(self) -> None:
        def shard(run: str, tier: str, **lanes: str) -> dict[str, Any]:
            return {
                "id": f"{STAMP}-x-{run}.a1",
                "path": f"night/{STAMP}/x/{run}.a1",
                "stamp": STAMP,
                "slug": "x",
                "tier": tier,
                "bench_run_id": run,
                "run_attempt": 1,
                "matrix_sha256": "m2" if run == "9" else "m1",
                "lanes": {key: {"status": status} for key, status in lanes.items()},
                "superseded_by": {"stale": "value"},
                "supersedes": ["stale"],
            }

        entries = [
            shard("1", "nightly", a="complete", b="blocked"),
            shard("2", "nightly", a="partial", b="blocked"),
            shard("3", "weekly", a="missing"),
            shard("9", "nightly", b="blocked"),
        ]
        entries[0]["path"] = entries[0]["path"].replace("night/", "weekly/")
        publish.supersede(entries)
        ids = [item["id"] for item in entries]
        # The first is in another root, so it is another night.
        self.assertEqual(entries[0]["superseded_by"], {})
        self.assertEqual(entries[1]["superseded_by"], {"b": ids[3]})
        self.assertEqual(entries[2]["superseded_by"], {"a": ids[1]})
        # Another matrix: the latest blocked run is its own earliest.
        self.assertEqual(entries[3]["superseded_by"], {})
        self.assertEqual(entries[1]["supersedes"], [ids[2]])
        self.assertEqual(entries[3]["supersedes"], [ids[1]])
        again = copy.deepcopy(entries)
        publish.supersede(again)
        self.assertEqual(again, entries)


class Latest(Case):
    def test_a_nightly_is_the_headline_and_a_later_manual_run_is_newer(self) -> None:
        site = self.site()
        nightly = build(
            self.tmp / "nightly",
            stamp="20261119T033017Z",
            run="44700000000",
            reason="nightly",
            lanes=("vm2-net",),
        )
        self.assertEqual(self.publish(site, self.tmp / "nightly").code, 0)
        head = branch(site)
        self.assertEqual(
            (head["headline"], head["stamp"]), ("nightly", "20261119T033017Z")
        )
        self.assertEqual(head["shards"], [nightly])
        self.assertEqual(head["attention_total"], 9)
        self.assertEqual(
            head["newer"],
            {
                "stamp": STAMP,
                "shards": [ID],
                "reasons": ["dispatch"],
                "totals": index(PLAIN_SITE)["totals"],
                "failing": 20,
            },
        )
        partial = build(
            self.tmp / "partial",
            stamp="20261121T010101Z",
            run="44900000000",
            lanes=("vm2-net",),
        )
        self.assertEqual(self.publish(site, self.tmp / "partial").code, 0)
        head = branch(site)
        self.assertEqual((head["headline"], head["shards"]), ("nightly", [nightly]))
        self.assertEqual(head["newer"]["stamp"], "20261121T010101Z")
        self.assertEqual(head["newer"]["shards"], [partial])
        self.assertEqual(head["newer"]["failing"], 9)

    def test_a_partial_dispatch_is_never_the_headline(self) -> None:
        site = self.site()
        partial = build(
            self.tmp / "partial",
            stamp="20261121T010101Z",
            run="44900000000",
            lanes=("vm2-net",),
        )
        self.assertEqual(self.publish(site, self.tmp / "partial").code, 0)
        head = branch(site)
        self.assertEqual((head["headline"], head["stamp"]), ("manual", STAMP))
        self.assertEqual(head["attention_total"], 20)
        self.assertEqual(head["newer"]["shards"], [partial])
        self.assertIs(head["stale"], False)

    def test_a_full_run_that_lost_a_lane_is_the_headline(self) -> None:
        def lost(records: list[dict[str, Any]]) -> None:
            run = vm2_run(records)
            run["status"] = "missing"
            for case in of_kind(records, "case"):
                if case["run_uid"] == run["run_uid"]:
                    case["status"] = "not-run"

        site = self.site()
        later = build(
            self.tmp / "lost", stamp="20261124T010101Z", run="44900000000", change=lost
        )
        done = self.publish(site, self.tmp / "lost")
        self.assertEqual(done.code, 0, done.stderr)
        head = branch(site)
        self.assertEqual((head["headline"], head["stamp"]), ("manual", later[:16]))
        self.assertEqual(head["shards"], [later])
        self.assertIsNone(head["newer"])
        missing = [lane for lane in head["lanes"] if lane["status"] == "missing"]
        self.assertEqual([lane["lane"] for lane in missing], ["vm2-net"])
        self.assertEqual(head["totals"]["not-run"], missing[0]["n"][6])
        # The landing page shows the lane as one row that the run page has.
        landing = (site / "bench" / "index.html").read_text()
        self.assertEqual(landing.count("lane did not report:"), 1)
        self.assertNotIn(f"runs/{later}.html#case-10-1-0-vm2-net", landing)

    def test_a_branch_with_a_run_in_the_window_is_never_stale(self) -> None:
        site = self.site(published=False)
        build(
            self.tmp / "nightly",
            stamp="20261101T033017Z",
            run="44000000000",
            reason="nightly",
        )
        build(self.tmp / "hand", stamp="20261116T000000Z", run="44500000000")
        build(
            self.tmp / "main",
            stamp="20261120T000000Z",
            slug="main-amd",
            branch="main-amd",
            run="44600000000",
        )
        done = self.publish(site, *(self.tmp / n for n in ("nightly", "hand", "main")))
        self.assertEqual(done.code, 0, done.stderr)
        head = branch(site)
        self.assertEqual(
            (head["headline"], head["stamp"]), ("nightly", "20261101T033017Z")
        )
        self.assertEqual(head["newer"]["stamp"], "20261116T000000Z")
        self.assertIs(head["stale"], False)
        landing = (site / "bench" / "index.html").read_text()
        self.assertNotIn("Branches with no recent run", landing)
        # Once its newest run falls out of the window the branch is folded,
        # and the line still names that newer run.
        build(
            self.tmp / "late",
            stamp="20261205T000000Z",
            slug="main-amd",
            branch="main-amd",
            run="44700000000",
        )
        self.assertEqual(self.publish(site, self.tmp / "late").code, 0)
        self.assertIs(branch(site)["stale"], True)
        landing = (site / "bench" / "index.html").read_text()
        self.assertIn("Branches with no recent run (1)", landing)
        self.assertIn("newer manual run: ", landing)

    def test_a_branch_far_behind_the_newest_run_is_stale(self) -> None:
        site = self.site()
        build(
            self.tmp / "main",
            stamp="20261206T000000Z",
            slug="main-amd",
            branch="main-amd",
            run="45000000000",
            lanes=("vm2-net",),
        )
        self.assertEqual(self.publish(site, self.tmp / "main").code, 0)
        self.assertIs(branch(site)["stale"], True)
        self.assertIs(branch(site, "main-amd")["stale"], False)
        self.assertEqual(branch(site, "main-amd")["branch"], "main-amd")
        done = self.publish(site, stale_days=16)
        self.assertEqual((done.code, done.out["generation"]), (0, "3"))
        self.assertIs(branch(site)["stale"], False)

    def test_weekly_nights_count_only_for_a_branch_without_others(self) -> None:
        site = self.site()
        weekly = build(
            self.tmp / "weekly",
            stamp="20261122T000000Z",
            run="44950000000",
            tier="weekly",
            reason="nightly",
        )
        only = build(
            self.tmp / "only",
            stamp="20261122T000000Z",
            slug="main-amd",
            run="44950000000",
            tier="weekly",
        )
        done = self.publish(site, self.tmp / "weekly", self.tmp / "only")
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(entry(site, weekly)["path"].split("/")[0], "weekly")
        self.assertEqual(branch(site)["stamp"], STAMP)
        self.assertEqual(branch(site, "main-amd")["shards"], [only])


class Artifacts(Case):
    def two(self) -> tuple[pathlib.Path, pathlib.Path, str]:
        first = self.collected(f"bench-collected-{SLUG}")
        second = self.tmp / "bench-collected-main-amd"
        other = build(second, slug="main-amd", branch="main-amd", sha="b" * 40)
        return first, second, other

    def truncate(self, directory: pathlib.Path) -> None:
        (path,) = directory.glob("bench-*.ndjson.gz")
        path.write_bytes(path.read_bytes()[:3000])

    def test_one_bad_branch_does_not_hide_the_other(self) -> None:
        first, second, other = self.two()
        self.truncate(second)
        seen = self.inspect(first, second, stage=True)
        self.assertEqual(seen.code, 0, seen.stderr)
        (valid,) = seen.report["valid"]
        (invalid,) = seen.report["invalid"]
        self.assertEqual(valid["input"], str(first))
        self.assertEqual([shard["id"] for shard in valid["shards"]], [ID])
        self.assertEqual(invalid["input"], str(second))
        self.assertIn("truncated gzip", invalid["why"])
        site = self.site(published=False)
        done = self.publish(site, pathlib.Path(valid["input"]))
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual([s["id"] for s in manifest(site)["shards"]], [ID])
        summary = seen.summary.read_text()
        self.assertIn("| valid |", summary)
        self.assertIn("| invalid |", summary)
        rows = summary.split("|---|")[-1].split("Cases are")[0]
        self.assertRegex(rows, r"^[A-Za-z0-9._+/ :|\n-]+$")

    def test_two_branches_of_one_bench_run_are_two_shards(self) -> None:
        first, second, other = self.two()
        seen = self.inspect(first, second, stage=True)
        self.assertEqual((seen.code, seen.report["invalid"]), (0, []))
        self.assertEqual(len(seen.report["valid"]), 2)
        site = self.site(published=False)
        done = self.publish(site, first, second)
        self.assertEqual(done.code, 0, done.stderr)
        shards = manifest(site)["shards"]
        self.assertEqual([s["id"] for s in shards], [ID, other])
        self.assertEqual({s["stamp"] for s in shards}, {STAMP})
        self.assertEqual({s["superseded_by"] == {} for s in shards}, {True})
        # The uids are those of the bench run, so they repeat across shards.
        runs = [
            gzip.decompress((site / DATA / s["path"] / "runs.ndjson.gz").read_bytes())
            for s in shards
        ]
        self.assertEqual(
            [json.loads(line)["run_uid"] for line in runs[0].splitlines()],
            [json.loads(line)["run_uid"] for line in runs[1].splitlines()],
        )
        self.assertEqual(
            sorted(load_json(site / DATA / "latest.json")["branches"]),
            sorted([SLUG, "main-amd"]),
        )
        self.assertEqual(len(list((site / DATA / "matrix").iterdir())), 1)

    def test_no_valid_artifact_stops_the_inspection(self) -> None:
        first, second, _ = self.two()
        self.truncate(first)
        self.truncate(second)
        seen = self.inspect(first, second)
        self.assertEqual(seen.code, 3)
        self.assertEqual(seen.report["valid"], [])
        self.assertEqual(len(seen.report["invalid"]), 2)

    def test_inspect_stages_what_the_archive_keeps(self) -> None:
        source = self.collected()
        (source / "summary.md").write_text("derived\n")
        (source / f"bench-{ID}.ndjson.zst").write_bytes(b"derived")
        seen = self.inspect(source, stage=True)
        self.assertEqual(seen.code, 0, seen.stderr)
        (shard,) = seen.report["valid"][0]["shards"]
        self.assertEqual((shard["id"], shard["tag"]), (ID, "bench-raw-2026-11"))
        self.assertEqual((shard["logs"], shard["collect"]), (False, True))
        self.assertEqual(shard["counts"]["cases"], index(PLAIN_SITE)["totals"])
        self.assertEqual(shard["counts"]["records"]["sample"], 739)
        self.assertEqual(shard["bytes"], entry(PLAIN_SITE, ID)["bytes"])
        stage = seen.stage
        names = [f"bench-{ID}.ndjson.gz", f"bench-{ID}.collect.json", MATRIX_FILE.name]
        self.assertEqual(sorted(p.name for p in stage.iterdir()), sorted(names))
        self.assertEqual([asset["name"] for asset in shard["assets"]], names)
        for asset in shard["assets"]:
            data = pathlib.Path(asset["path"]).read_bytes()
            self.assertEqual(pathlib.Path(asset["path"]).parent, stage)
            self.assertEqual(
                (asset["bytes"], asset["sha256"]), (len(data), sha256(data))
            )
        self.assertEqual(
            (stage / names[0]).read_bytes(), (source / names[0]).read_bytes()
        )
        self.assertEqual(
            (stage / names[1]).read_bytes(), (source / "collect.json").read_bytes()
        )
        # The summary names the artifact by its last two directories, which
        # are the run id and the artifact name; the plan keeps the whole path.
        self.assertEqual(seen.report["valid"][0]["input"], str(source))
        row = f"| {self.tmp.name}/collected | {ID} | valid |"
        self.assertIn(row, seen.summary.read_text())
        self.assertNotIn(str(self.tmp), seen.summary.read_text())

    def test_the_same_shard_in_two_inputs_counts_once(self) -> None:
        first, second = self.collected("one"), self.collected("two")
        seen = self.inspect(first, second)
        self.assertEqual(seen.code, 0)
        self.assertEqual([len(item["shards"]) for item in seen.report["valid"]], [1, 0])
        site = self.site(published=False)
        self.assertEqual(self.publish(site, first, second).code, 0)
        self.assertEqual(len(manifest(site)["shards"]), 1)
        build(second, change=lambda records: records[0].update(reason="other"))
        seen = self.inspect(first, second)
        self.assertEqual([i["input"] for i in seen.report["invalid"]], [str(second)])
        clean = self.site(published=False)
        done = self.publish(clean, first, second)
        self.assertEqual(done.code, 3)
        self.assertIn("other bytes", done.stderr)
        self.assertEqual(snapshot(clean), {})


class Corrupt(Case):
    def rewrite(self, change: Callable[[list[dict[str, Any]]], None]) -> pathlib.Path:
        self.calls += 1
        directory = self.tmp / f"bad-{self.calls}"
        build(directory, run=LATER_RUN, change=change)
        return directory

    def raw(self, change: Callable[[bytes], bytes]) -> pathlib.Path:
        directory = self.rewrite(lambda records: None)
        (path,) = directory.glob("bench-*.ndjson.gz")
        path.write_bytes(change(path.read_bytes()))
        return directory

    def renamed(self, name: str) -> pathlib.Path:
        directory = self.rewrite(lambda records: None)
        (path,) = directory.glob("bench-*.ndjson.gz")
        path.rename(directory / name)
        return directory

    def other_matrix(self) -> tuple[pathlib.Path, pathlib.Path]:
        """Two artifacts of a matrix the site lacks; only the second has it."""
        data = MATRIX_FILE.read_bytes() + b"\n"
        digest = sha256(data)

        def use(records: list[dict[str, Any]]) -> None:
            for run in of_kind(records, "run"):
                run["matrix_sha256"] = digest

        self.calls += 1
        alone = self.tmp / f"alone-{self.calls}"
        sibling = self.tmp / f"sibling-{self.calls}"
        build(alone, run=LATER_RUN, change=use, matrix=False)
        build(sibling, run=LATER_RUN, slug="main-amd", change=use, matrix=False)
        (sibling / f"matrix-{digest}.json").write_bytes(data)
        return alone, sibling

    def own_matrix(self, data: bytes) -> pathlib.Path:
        """An artifact whose runs name a matrix of its own, with these bytes."""
        digest = sha256(data)

        def use(records: list[dict[str, Any]]) -> None:
            for run in of_kind(records, "run"):
                run["matrix_sha256"] = digest

        self.calls += 1
        directory = self.tmp / f"matrix-{self.calls}"
        build(directory, run=LATER_RUN, change=use, matrix=False)
        (directory / f"matrix-{digest}.json").write_bytes(data)
        return directory

    def test_a_bad_strict_input_leaves_the_tree_untouched(self) -> None:
        wide = "".join(chr(ord(c) + 0xFEE0) if c.isdigit() else c for c in STAMP)

        def invalid(records: list[dict[str, Any]]) -> None:
            of_kind(records, "case")[5]["status"] = "bogus"

        def secret(records: list[dict[str, Any]]) -> None:
            of_kind(records, "case")[5]["error_excerpt"] = f"key {AWS_KEY} seen"

        def unstable(records: list[dict[str, Any]]) -> None:
            of_kind(records, "case")[5]["cmdline"] = f"{UNSTABLE} nixlbench"

        def unstable_test(records: list[dict[str, Any]]) -> None:
            of_kind(records, "test")[0]["error_excerpt"] = UNSTABLE

        def orphan(records: list[dict[str, Any]]) -> None:
            of_kind(records, "sample")[0]["case_uid"] = "0" * 20

        def orphan_case(records: list[dict[str, Any]]) -> None:
            of_kind(records, "case")[0]["run_uid"] = "0" * 20

        def lane(records: list[dict[str, Any]]) -> None:
            vm2_run(records)["lane"] = "vm2/net"

        def rocm(records: list[dict[str, Any]]) -> None:
            vm2_run(records)["rocm_version"] = ".."

        def stamp(records: list[dict[str, Any]]) -> None:
            vm2_run(records)["stamp"] = "20261121T204631Z"

        def branch_(records: list[dict[str, Any]]) -> None:
            vm2_run(records)["branch"] = "another"

        def no_slug(records: list[dict[str, Any]]) -> None:
            for run in of_kind(records, "run"):
                run["slug"] = None

        def repo(records: list[dict[str, Any]]) -> None:
            for run in of_kind(records, "run"):
                run["repo"] = "ROCm/nixl/../x"

        def no_run(records: list[dict[str, Any]]) -> None:
            records[:] = []

        def huge(records: list[dict[str, Any]]) -> None:
            of_kind(records, "sample")[0]["bw_gbps"] = 10**400

        def twice(data: bytes) -> bytes:
            # A parser keeps the second 'cmdline'; the first would be archived.
            hidden = b'{"cmdline":"' + AWS_KEY.encode() + b'",'
            lines = gzip.decompress(data).split(b"\n")
            at = next(n for n, line in enumerate(lines) if b'"kind":"case"' in line)
            lines[at] = hidden + lines[at][1:]
            return gzip.compress(b"\n".join(lines), mtime=0)

        def named(data: bytes) -> bytes:
            buffer = io.BytesIO()
            with gzip.GzipFile(AWS_KEY, "wb", fileobj=buffer, mtime=0) as stream:
                stream.write(gzip.decompress(data))
            return buffer.getvalue()

        def secret_matrix() -> pathlib.Path:
            return self.own_matrix(
                MATRIX_FILE.read_bytes()[:-1] + f',"x":"{AWS_KEY}"}}'.encode()
            )

        def big_matrix() -> pathlib.Path:
            return self.own_matrix(
                MATRIX_FILE.read_bytes()[:-1]
                + b',"x":"'
                + b"y" * publish.MATRIX_MAX
                + b'"}'
            )

        def twice_matrix() -> pathlib.Path:
            return self.own_matrix(b'{"lanes":{},"lanes":{}}')

        def bad_matrix() -> pathlib.Path:
            directory = self.rewrite(lambda records: None)
            (directory / MATRIX_FILE.name).write_bytes(b'{"lanes": {}}')
            return directory

        def no_records() -> pathlib.Path:
            directory = self.tmp / "no-records"
            directory.mkdir()
            shutil.copy(MATRIX_FILE, directory)
            return directory

        cases: dict[str, tuple[Callable[[], pathlib.Path], str]] = {
            "a truncated gzip": (lambda: self.raw(lambda d: d[:2000]), "truncated"),
            "not a gzip": (lambda: self.raw(lambda d: b"x" + d), "not a gzip file"),
            "a line that is not JSON": (
                lambda: self.raw(
                    lambda d: gzip.compress(gzip.decompress(d) + b"{oops\n")
                ),
                ":1656: not JSON",
            ),
            "a record that is not valid": (
                lambda: self.rewrite(invalid),
                "schema: $.status",
            ),
            "a number no reader can hold": (
                lambda: self.rewrite(huge),
                "an integer beyond 64 bits",
            ),
            "a line with a key twice": (
                lambda: self.raw(twice),
                ": a key is given twice",
            ),
            "a gzip header with a file name": (
                lambda: self.raw(named),
                "gzip header with optional fields",
            ),
            "a matrix with a secret-looking string": (
                secret_matrix,
                "holds what looks like a secret",
            ),
            "a matrix that is too large": (big_matrix, "larger than 1048576 bytes"),
            "a matrix with a key twice": (twice_matrix, "not a JSON object"),
            "a secret-looking string": (
                lambda: self.rewrite(secret),
                "secret:aws-key-id",
            ),
            "an unstable cmdline": (
                lambda: self.rewrite(unstable),
                "'cmdline' is not redaction-stable",
            ),
            "an unstable test excerpt": (
                lambda: self.rewrite(unstable_test),
                "'error_excerpt' is not redaction-stable",
            ),
            "an orphan sample": (lambda: self.rewrite(orphan), "unknown case_uid"),
            "an orphan case": (lambda: self.rewrite(orphan_case), "unknown run_uid"),
            "no run record": (lambda: self.rewrite(no_run), "no run record"),
            "a matrix of another hash": (bad_matrix, "not the hash in its name"),
            "a matrix only in a sibling": (
                lambda: self.other_matrix()[0],
                "neither beside",
            ),
            "no records file": (no_records, "no records file"),
            "no such directory": (lambda: self.tmp / "nowhere", "not a directory"),
            "a slug of two dots": (
                lambda: self.renamed(f"bench-{STAMP}-..-{LATER_RUN}.a1.ndjson.gz"),
                "not a publishable records file name",
            ),
            "a stamp in full-width digits": (
                lambda: self.renamed(f"bench-{wide}-{SLUG}-{LATER_RUN}.a1.ndjson.gz"),
                "not a publishable records file name",
            ),
            "a slug other than the records'": (
                lambda: self.renamed(f"bench-{STAMP}-main-{LATER_RUN}.a1.ndjson.gz"),
                "another slug",
            ),
            "a lane with a slash": (
                lambda: self.rewrite(lane),
                "'lane' has a forbidden",
            ),
            "a ROCm version of two dots": (
                lambda: self.rewrite(rocm),
                "'rocm_version' has a forbidden",
            ),
            "a run of another stamp": (
                lambda: self.rewrite(stamp),
                "disagree on 'stamp'",
            ),
            "a run of another branch": (
                lambda: self.rewrite(branch_),
                "disagree on 'branch'",
            ),
            "a collection without a slug": (
                lambda: self.renamed_after(no_slug),
                "another slug",
            ),
            "a repository that is not OWNER/NAME": (
                lambda: self.rewrite(repo),
                "'repo'",
            ),
        }
        site = self.site()
        before = snapshot(site)
        for label, (make, text) in cases.items():
            with self.subTest(label):
                directory = make()
                done = self.publish(site, directory)
                self.assertEqual(done.code, 3, done.stderr)
                self.assertIn(text, done.stderr)
                for leak in (AWS_KEY, PLAIN):
                    self.assertNotIn(leak, done.stderr + done.stdout)
                self.assertEqual(snapshot(site), before)
                self.assertEqual(done.out["changed"], "false")
                seen = self.inspect(directory)
                self.assertEqual(seen.code, 3)
                self.assertIn(text, seen.report["invalid"][0]["why"])
                self.assertNotIn(AWS_KEY, json.dumps(seen.report))

    def renamed_after(
        self, change: Callable[[list[dict[str, Any]]], None]
    ) -> pathlib.Path:
        directory = self.rewrite(change)
        (path,) = directory.glob("bench-*.ndjson.gz")
        path.rename(directory / f"bench-{STAMP}-local-{LATER_RUN}.a1.ndjson.gz")
        return directory

    def test_one_bad_input_among_good_ones_publishes_nothing(self) -> None:
        site = self.site()
        before = snapshot(site)
        good = self.tmp / "good"
        build(good, run=LATER_RUN, lanes=("vm2-net",))
        bad = self.raw(lambda data: data[:2000])
        done = self.publish(site, good, bad)
        self.assertEqual(done.code, 3)
        self.assertEqual(snapshot(site), before)

    def test_records_of_another_repository_are_refused(self) -> None:
        def forged(records: list[dict[str, Any]]) -> None:
            for run in of_kind(records, "run"):
                run["repo"] = "evil-org/evil-repo"

        directory = self.rewrite(forged)
        seen = self.inspect(directory, repo="ROCm/nixl")
        self.assertEqual(seen.code, 3)
        self.assertIn("of another repository", seen.report["invalid"][0]["why"])
        self.assertEqual(self.inspect(COLLECTED, repo="ROCm/nixl").code, 0)
        self.assertEqual(self.inspect(COLLECTED, repo="ROCm/../nixl").code, 2)
        # A first publication cannot give the site to another repository.
        site = self.site(published=False)
        done = self.publish(site, directory, repo="ROCm/nixl")
        self.assertEqual(done.code, 3)
        self.assertIn("of another repository", done.stderr)
        self.assertEqual(snapshot(site), {})
        done = self.publish(site, heal=directory, repo="ROCm/nixl")
        self.assertEqual((done.code, snapshot(site)), (0, {}))
        self.assertIn("of another repository", done.report["skipped"][0]["why"])
        self.assertEqual(self.publish(site, COLLECTED, repo="ROCm/nixl").code, 0)
        self.assertEqual(manifest(site)["repo"], "ROCm/nixl")
        # And a site of another repository is not this workflow's to write.
        done = self.publish(self.site(), repo="ROCm/other")
        self.assertEqual(done.code, 2)
        self.assertIn("another repository", done.stderr)
        self.assertEqual(self.publish(self.site(), repo="no slash").code, 2)

    def test_an_artifact_the_tool_trips_over_does_not_hide_the_others(self) -> None:
        good = self.tmp / "good"
        build(good, run=LATER_RUN, lanes=("vm2-net",))
        bad = self.tmp / "bad"
        build(bad, run=LATER_RUN, slug="main-amd", branch="main-amd")
        render = pages.render_run

        def trip(index: dict[str, Any]) -> str:
            if index["shard"]["slug"] == "main-amd":
                raise RuntimeError(f"\n::error::{AWS_KEY}")
            return render(index)

        with mock.patch.object(pages, "render_run", trip):
            seen = self.inspect(bad, good)
            self.assertEqual(seen.code, 0, seen.stderr)
            (invalid,) = seen.report["invalid"]
            self.assertEqual(
                (invalid["input"], invalid["why"]),
                (str(bad), "internal error (RuntimeError)"),
            )
            self.assertEqual([v["input"] for v in seen.report["valid"]], [str(good)])
            self.assertNotIn(AWS_KEY, seen.stderr + seen.summary.read_text())
            self.assertEqual(self.inspect(bad).code, 3)
            # The same file coming back from the archive costs only itself.
            site = self.site()
            done = self.publish(site, good, heal=bad)
            self.assertEqual(done.code, 0, done.stderr)
            (skipped,) = done.report["skipped"]
            self.assertEqual(skipped["why"], "internal error (RuntimeError)")
            self.assertEqual(len(manifest(site)["shards"]), 2)
            # A strict input is still an error, and says so on one line each.
            done = self.publish(site, bad)
            self.assertEqual(done.code, 5)
            self.assertNotIn("\n::", done.stderr)
            self.assertTrue(
                all(
                    line.startswith("amd_bench_publish.py: ")
                    for line in done.stderr.splitlines()
                )
            )

    def test_a_page_that_cannot_be_rendered_makes_the_artifact_invalid(self) -> None:
        def fail(index: dict[str, Any]) -> str:
            raise pages.PageError("no")

        with mock.patch.object(pages, "render_run", fail):
            seen = self.inspect(COLLECTED, stage=True)
        self.assertEqual(seen.code, 3)
        self.assertIn("cannot be rendered", seen.report["invalid"][0]["why"])
        self.assertFalse(seen.stage.exists())

    def test_text_of_a_record_cannot_start_a_workflow_command(self) -> None:
        def extra(records: list[dict[str, Any]]) -> None:
            of_kind(records, "case")[0]["\n::stop-commands::x"] = 1

        directory = self.rewrite(extra)
        for done in (self.inspect(directory), self.publish(self.site(), directory)):
            self.assertEqual(done.code, 3)
            self.assertIn("unexpected key", done.stderr)
            self.assertNotIn("\n::", done.stderr)
            for line in done.stderr.splitlines():
                self.assertTrue(line.startswith("amd_bench_publish.py: "), line)
        done = self.publish(self.site(), heal=directory)
        self.assertEqual(done.code, 0)
        self.assertNotIn("\n::", done.stderr)

    def test_a_ratio_that_no_float_can_hold_is_left_out(self) -> None:
        matrix = load_json(MATRIX_FILE)
        pair = matrix["lanes"]["vm2-net"]["pairs"][0]

        def tiny(records: list[dict[str, Any]]) -> None:
            cases = {c["case_uid"]: c for c in of_kind(records, "case")}
            for sample in of_kind(records, "sample"):
                key = cases[sample["case_uid"]]["case_key"]
                if key == pair["num"]:
                    sample["bw_gbps"] = 1.5e308
                elif key == pair["den"]:
                    sample["bw_gbps"] = 5e-324

        directory = self.rewrite(tiny)
        site = self.site(published=False)
        self.assertEqual(self.publish(site, directory).code, 0)
        doc = index(site, f"{STAMP}-{SLUG}-{LATER_RUN}.a1")
        (ratio,) = [r for r in doc["ratios"] if r["key"] == pair["key"]]
        self.assertEqual((ratio["status"], ratio["points"]), ("unavailable", []))

    def test_a_matrix_is_never_taken_from_a_sibling_input(self) -> None:
        alone, sibling = self.other_matrix()
        site = self.site()
        before = snapshot(site)
        done = self.publish(site, alone, sibling)
        self.assertEqual(done.code, 3)
        self.assertEqual(snapshot(site), before)
        seen = self.inspect(alone, sibling)
        self.assertEqual(seen.code, 0)
        self.assertEqual([i["input"] for i in seen.report["invalid"]], [str(alone)])
        self.assertEqual([i["input"] for i in seen.report["valid"]], [str(sibling)])
        # Once published, the matrix on the site serves a later artifact.
        self.assertEqual(self.publish(site, sibling).code, 0)
        self.assertEqual(self.publish(site, alone).code, 0)
        self.assertEqual(len(list((site / DATA / "matrix").iterdir())), 2)

    def test_a_shard_over_the_budget_is_refused(self) -> None:
        site = self.site()
        before = snapshot(site)
        directory = self.rewrite(lambda records: None)
        size = self.inspect(directory).report["valid"][0]["shards"][0]["bytes"]
        done = self.publish(site, directory, max_bytes=size - 1)
        self.assertEqual(done.code, 3)
        self.assertIn(f"over budget: {size} bytes", done.stderr)
        self.assertEqual(snapshot(site), before)
        plan = self.tmp / "p.json"
        for limit, code in ((size - 1, 3), (size, 0)):
            seen = tool(
                "inspect", "--input", directory, "--out", plan, "--max-bytes", limit
            )
            self.assertEqual(seen.code, code)
        # What inspect measured is what the manifest then lists.
        done = self.publish(site, directory, max_bytes=size, warn_bytes=1000)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(done.report["published"][0]["bytes"], size)
        self.assertRegex(
            " ".join(done.report["warnings"]), r"[0-9]+ bytes of shard data"
        )

    def test_a_site_this_tool_cannot_work_on(self) -> None:
        directory = self.tmp / "later"
        build(directory, run=LATER_RUN, lanes=("vm2-net",))
        for label, change in (
            ("another repository", lambda doc: doc.update(repo="other/repo")),
            ("another schema", lambda doc: doc.update(schema=2)),
            ("a damaged entry", lambda doc: doc["shards"][0].update(path="../x")),
            ("no generation", lambda doc: doc.pop("generation")),
        ):
            with self.subTest(label):
                site = self.site()
                doc = manifest(site)
                change(doc)
                (site / DATA / "manifest.json").write_text(json.dumps(doc))
                before = snapshot(site)
                done = self.publish(site, directory)
                self.assertEqual(done.code, 2, done.stderr)
                self.assertEqual(snapshot(site), before)
        site = self.site()
        (site / DATA / "manifest.json").write_text("[]")
        self.assertEqual(self.publish(site, directory).code, 2)
        self.assertEqual(self.publish(self.tmp / "nowhere", directory).code, 2)
        self.assertEqual(
            self.publish(self.site(), directory, default_branch="a b").code, 2
        )

    @unittest.skipUnless(hasattr(os, "symlink"), "no symbolic links")
    def test_a_symbolic_link_in_the_site_is_not_followed(self) -> None:
        site = self.site(published=False)
        outside = self.tmp / "outside"
        outside.mkdir()
        os.symlink(outside, site / "bench")
        done = self.publish(site, COLLECTED)
        self.assertEqual(done.code, 2, done.stderr)
        self.assertEqual(list(outside.iterdir()), [])


class Conflicts(Case):
    def test_other_bytes_under_a_published_id_are_a_conflict(self) -> None:
        site = self.site()
        before = snapshot(site)
        directory = self.tmp / "again"
        build(directory, change=lambda records: records[0].update(reason="other"))
        done = self.publish(site, directory)
        self.assertEqual(done.code, 4, done.stderr)
        self.assertIn("conflict", done.stderr)
        self.assertEqual(snapshot(site), before)

    def test_an_unlisted_shard_on_the_site_is_adopted_if_identical(self) -> None:
        site = self.site()
        wanted = snapshot(site)
        (site / DATA / "manifest.json").unlink()
        (site / DATA / "latest.json").unlink()
        done = self.publish(site, COLLECTED)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(snapshot(site), wanted)
        for name in ("cases.ndjson.gz", "index.json"):
            with self.subTest(name):
                site = self.site()
                (site / DATA / "manifest.json").unlink()
                with open(site / SHARD_DIR / name, "ab") as stream:
                    stream.write(b"x")
                before = snapshot(site)
                done = self.publish(site, COLLECTED)
                self.assertEqual(done.code, 4, done.stderr)
                self.assertEqual(snapshot(site), before)
        site = self.site()
        (site / DATA / "manifest.json").unlink()
        (site / DATA / "matrix" / f"{MATRIX_SHA}.json").write_bytes(b"{}")
        self.assertEqual(self.publish(site, COLLECTED).code, 4)

    def test_a_quarantined_id_is_skipped(self) -> None:
        site = self.site()
        directory = self.tmp / "later"
        later = build(directory, run=LATER_RUN, lanes=("vm2-net",))
        doc = manifest(site)
        doc["quarantine"] = [{"id": later, "reason": "bad run", "at": NOW}]
        (site / DATA / "manifest.json").write_bytes(publish.manifest_text(doc))
        before = snapshot(site)
        done = self.publish(site, directory)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(done.report["quarantined"], [later])
        self.assertIn("quarantined", done.stderr)
        self.assertEqual(snapshot(site), before)
        self.assertEqual(done.out["changed"], "false")


class LogsAndCollect(Case):
    MEMBER = "vm2-net-10.1.0/logs/001-x/init.stderr.log"

    def with_logs(self, members: dict[str, Optional[bytes]]) -> pathlib.Path:
        self.calls += 1
        source = self.collected(f"logs-{self.calls}")
        tarball(source / f"bench-{ID}.logs.tar.gz", members)
        return source

    def test_a_clean_tarball_is_archived(self) -> None:
        source = self.with_logs({self.MEMBER: b"all good\n", "a/b.log": b"\xff\xfe"})
        seen = self.inspect(source, stage=True)
        (shard,) = seen.report["valid"][0]["shards"]
        self.assertIs(shard["logs"], True)
        staged = seen.stage
        self.assertEqual(
            (staged / f"bench-{ID}.logs.tar.gz").read_bytes(),
            (source / f"bench-{ID}.logs.tar.gz").read_bytes(),
        )
        self.assertIn(f"bench-{ID}.logs.tar.gz", [a["name"] for a in shard["assets"]])
        site = self.site(published=False)
        self.assertEqual(self.publish(site, source).code, 0)
        self.assertIs(entry(site, ID)["raw"]["logs"], True)
        self.assertTrue(
            index(site)["links"]["raw_logs"].endswith(f"/bench-{ID}.logs.tar.gz")
        )
        self.assertEqual(index(site)["warnings"], [])

    def test_a_tarball_that_fails_the_check_is_not_archived(self) -> None:
        many: dict[str, Optional[bytes]] = {f"a/{n}.log": b"x" for n in range(4)}
        cases: dict[str, tuple[dict[str, Optional[bytes]], str]] = {
            "a secret-looking member": (
                {"a/ok.log": b"fine", self.MEMBER: f"id {AWS_KEY}\n".encode()},
                f"member '{self.MEMBER}': secret:aws-key-id",
            ),
            "a member that redaction would change": (
                {self.MEMBER: f"env {UNSTABLE}\n".encode()},
                f"member '{self.MEMBER}': not redaction-stable",
            ),
            "a secret-looking member name": (
                {f"a/{AWS_KEY}.log": b"x"},
                "member 1: its name looks like a secret",
            ),
            "a symbolic link": (
                {"a/link": None},
                "member 'a/link': not a regular file",
            ),
            "an absolute path": (
                {"/abs/x.log": b"x"},
                "member '/abs/x.log': not a relative path inside the archive",
            ),
            "a path that climbs": (
                {"a/../../x.log": b"x"},
                "member 'a/../../x.log': not a relative path inside the archive",
            ),
            "too many members": (many, "more than 3 members"),
            "a member that is not a log": (
                {"a/core.bin": b"x"},
                "member 'a/core.bin': not a .log file",
            ),
            "a compressed member": (
                {self.MEMBER: gzip.compress(b"", mtime=0)},
                f"member '{self.MEMBER}': not text",
            ),
            "a line too long for the scan": (
                {self.MEMBER: b"short\n" + b"y" * 33},
                f"member '{self.MEMBER}': a line too long to check",
            ),
            "a member that is too large": (
                {"a/big.log": b"x" * 50},
                "member 'a/big.log': larger than 40 bytes",
            ),
        }
        for label, (members, text) in cases.items():
            with (
                self.subTest(label),
                mock.patch.object(publish, "LOGS_MAX_MEMBERS", 3),
                mock.patch.object(publish, "LOGS_MAX_MEMBER", 40),
                mock.patch.object(publish, "LOGS_MAX_LINE", 32),
            ):
                source = self.with_logs(members)
                seen = self.inspect(source, stage=True)
                self.assertEqual(seen.code, 0, seen.stderr)
                (shard,) = seen.report["valid"][0]["shards"]
                self.assertIs(shard["logs"], False)
                staged = seen.stage
                self.assertEqual(
                    sorted(p.name for p in staged.iterdir()),
                    sorted(asset["name"] for asset in shard["assets"]),
                )
                self.assertFalse((staged / f"bench-{ID}.logs.tar.gz").exists())
                site = self.site(published=False)
                done = self.publish(site, source)
                self.assertEqual(done.code, 0, done.stderr)
                self.assertIs(entry(site, ID)["raw"]["logs"], False)
                doc = index(site)
                self.assertIsNone(doc["links"]["raw_logs"])
                (note,) = doc["warnings"]
                self.assertEqual(note, f"logs tarball not archived: {text}")
                self.assertIn(text, " ".join(done.report["warnings"]))
                everything = done.stderr + json.dumps(doc) + json.dumps(seen.report)
                for leak in (AWS_KEY, PLAIN):
                    self.assertNotIn(leak, everything)
                self.assertEqual(doc["totals"], index(PLAIN_SITE)["totals"])

    def test_a_tarball_may_say_nothing_beside_its_members(self) -> None:
        path = self.tmp / "logs.tar.gz"
        members: dict[str, Optional[bytes]] = {"a/x.log": b"fine\n"}
        tarball(path, members)
        self.assertEqual(publish.check_logs(path), "")
        tarball(path, members, trailer=b"\0" * 4096)
        self.assertEqual(publish.check_logs(path), "")
        tarball(path, members, owner=AWS_KEY)
        self.assertEqual(
            publish.check_logs(path),
            "member 'a/x.log': its header carries more than a name",
        )
        tarball(path, members, trailer=AWS_KEY.encode())
        self.assertEqual(publish.check_logs(path), "data after the end of the archive")
        with tarfile.open(path, mode="w:gz") as tar:
            info = tarfile.TarInfo("a/x.log")
            tar.addfile(info, io.BytesIO(b""))
        self.assertEqual(publish.check_logs(path), "gzip header with optional fields")
        tarball(path, members)
        with mock.patch.object(publish, "LOGS_MAX_SECONDS", -1.0):
            self.assertEqual(publish.check_logs(path), "not checked within -1 seconds")

    def test_a_file_that_is_no_tarball_is_not_archived(self) -> None:
        source = self.collected()
        (source / f"bench-{ID}.logs.tar.gz").write_bytes(b"not a tarball")
        site = self.site(published=False)
        self.assertEqual(self.publish(site, source).code, 0)
        self.assertIn("unreadable", index(site)["warnings"][0])

    def test_the_collect_file_of_the_shard_is_preferred(self) -> None:
        source = self.collected()
        own = {"rejected": [{"file": "x"}], "warnings": ["own"] * 60, "outputs": []}
        (source / f"bench-{ID}.collect.json").write_text(json.dumps(own))
        site = self.site(published=False)
        self.assertEqual(self.publish(site, source).code, 0)
        collect = index(site)["collect"]
        self.assertEqual((collect["rejected"], collect["warnings"]), (1, ["own"] * 50))
        self.assertIs(entry(site, ID)["raw"]["collect"], True)
        seen = self.inspect(source, stage=True)
        staged = seen.stage
        self.assertEqual(
            json.loads((staged / f"bench-{ID}.collect.json").read_text()), own
        )

    def test_a_collect_file_of_another_shard_is_ignored(self) -> None:
        for label, text in (
            ("another shard", json.dumps({"outputs": ["bench-other.ndjson.gz"]})),
            ("no outputs", json.dumps({"warnings": ["x"]})),
            ("not an object", "[]"),
            ("not JSON", "{"),
        ):
            with self.subTest(label):
                self.calls += 1
                source = self.collected(f"collect-{self.calls}")
                (source / "collect.json").write_text(text)
                site = self.site(published=False)
                self.assertEqual(self.publish(site, source).code, 0)
                self.assertIsNone(index(site)["collect"])
                self.assertIs(entry(site, ID)["raw"]["collect"], False)
                self.assertIsNone(index(site)["links"]["raw_collect"])
                seen = self.inspect(source)
                self.assertIs(seen.report["valid"][0]["shards"][0]["collect"], False)

    def test_a_collect_file_with_a_key_twice_is_not_archived(self) -> None:
        source = self.collected()
        text = (source / "collect.json").read_text()
        hidden = '{"schema":"' + AWS_KEY + '",' + text[1:]
        (source / "collect.json").unlink()
        (source / f"bench-{ID}.collect.json").write_text(hidden)
        seen = self.inspect(source, stage=True)
        self.assertEqual(seen.code, 0, seen.stderr)
        (shard,) = seen.report["valid"][0]["shards"]
        self.assertIs(shard["collect"], False)
        self.assertFalse((seen.stage / f"bench-{ID}.collect.json").exists())
        self.assertIn("each key once", shard["warnings"][0])

    def test_a_collect_file_with_a_secret_is_not_archived(self) -> None:
        source = self.collected()
        doc = load_json(source / "collect.json")
        doc["warnings"] = [f"saw {AWS_KEY}"]
        (source / "collect.json").write_text(json.dumps(doc))
        site = self.site(published=False)
        done = self.publish(site, source)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertIsNone(index(site)["collect"])
        self.assertIs(entry(site, ID)["raw"]["collect"], False)
        self.assertIn("collect file not archived", index(site)["warnings"][0])
        self.assertNotIn(AWS_KEY, json.dumps(snapshot(site), default=repr))
        seen = self.inspect(source, stage=True)
        staged = seen.stage
        self.assertFalse((staged / f"bench-{ID}.collect.json").exists())


class Heal(Case):
    def heal_dir(self) -> tuple[pathlib.Path, str]:
        directory = self.tmp / "heal"
        later = build(directory, run=LATER_RUN, lanes=("vm2-net",))
        return directory, later

    def test_a_healed_shard_links_to_what_the_archive_has(self) -> None:
        directory, later = self.heal_dir()
        tarball(directory / f"bench-{later}.logs.tar.gz", {"a/x.log": b"x"})
        site = self.site()
        done = self.publish(site, heal=directory)
        self.assertEqual(done.code, 0, done.stderr)
        raw = entry(site, later)["raw"]
        self.assertEqual((raw["logs"], raw["collect"]), (False, False))
        self.assertIsNone(index(site, later)["collect"])
        self.assertIsNone(index(site, later)["links"]["raw_logs"])
        self.assertEqual(index(site, later)["warnings"], [])
        self.assertEqual(done.report["published"][0]["id"], later)

        site = self.site()
        assets = self.tmp / "assets.jsonl"
        names = [f"bench-{later}.logs.tar.gz", f"bench-{later}.collect.json"]
        lines = [{"tag": "bench-raw-2026-11", "name": name} for name in names]
        assets.write_text("".join(json.dumps(line) + "\n" for line in lines) + "oops\n")
        done = self.publish(site, heal=directory, assets=assets)
        self.assertEqual(done.code, 0, done.stderr)
        raw = entry(site, later)["raw"]
        self.assertEqual((raw["logs"], raw["collect"]), (True, True))
        self.assertIsNotNone(index(site, later)["links"]["raw_logs"])
        self.assertIn("--assets", " ".join(done.report["warnings"]))
        # The published fixture is of the same tag, but only --rerender asks.
        self.assertIs(entry(site, ID)["raw"]["collect"], True)

        site = self.site()
        assets.write_text(json.dumps({"tag": "bench-raw-2026-10", "name": "x"}) + "\n")
        self.assertEqual(self.publish(site, heal=directory, assets=assets).code, 0)
        self.assertIs(entry(site, later)["raw"]["logs"], False)

    def test_an_invalid_heal_input_is_skipped_with_a_warning(self) -> None:
        directory, later = self.heal_dir()
        bad = build(directory, run="44830000000", lanes=("vm2-net",))
        path = directory / f"bench-{bad}.ndjson.gz"
        path.write_bytes(path.read_bytes()[:2000])
        site = self.site()
        done = self.publish(site, heal=[directory, self.tmp / "nowhere", self.tmp])
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual([s["id"] for s in manifest(site)["shards"]], [later, ID])
        (skipped,) = done.report["skipped"]
        self.assertIn(path.name, skipped["what"])
        self.assertIn("truncated", skipped["why"])
        self.assertIn("warning: skipped", done.stderr)

    def test_a_heal_copy_never_stops_a_publish(self) -> None:
        directory, later = self.heal_dir()
        build(directory / "again", change=lambda records: records[0].update(reason="x"))
        over = self.tmp / "over"
        build(over, run="44830000000")
        size = self.inspect(over).report["valid"][0]["shards"][0]["bytes"]
        site = self.site()
        done = self.publish(
            site,
            directory,
            heal=[directory, directory / "again", over],
            max_bytes=size - 1,
        )
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual([s["id"] for s in manifest(site)["shards"]], [later, ID])
        self.assertEqual(
            sorted(item["what"].split("/")[-2] for item in done.report["skipped"]),
            ["again", "over"],
        )
        reasons = " ".join(item["why"] for item in done.report["skipped"])
        self.assertIn("other bytes are published", reasons)
        self.assertIn("over budget", reasons)
        self.assertEqual([item["id"] for item in done.report["published"]], [later])

    def test_a_healed_shard_that_conflicts_with_the_site_is_skipped(self) -> None:
        directory, later = self.heal_dir()
        site = self.site()
        planted = site / DATA / "night" / STAMP / SLUG / f"{LATER_RUN}.a1"
        planted.mkdir(parents=True)
        (planted / "runs.ndjson.gz").write_bytes(b"other bytes")
        good = self.tmp / "good"
        new = build(good, run="44830000000", lanes=("vm2-net",))
        done = self.publish(site, good, heal=directory)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual([s["id"] for s in manifest(site)["shards"]], [new, ID])
        (skipped,) = done.report["skipped"]
        self.assertIn("exists with other content", skipped["why"])
        self.assertEqual((planted / "runs.ndjson.gz").read_bytes(), b"other bytes")
        self.assertFalse((site / pages.run_page_path(later)).exists())
        self.assertEqual(sorted(p.name for p in planted.iterdir()), ["runs.ndjson.gz"])
        # As a strict input the same shard is a conflict, and nothing is written.
        before = snapshot(site)
        done = self.publish(site, directory)
        self.assertEqual((done.code, snapshot(site)), (4, before))

    def test_a_heal_copy_of_a_published_shard_changes_nothing(self) -> None:
        site = self.site()
        before = snapshot(site)
        done = self.publish(site, heal=COLLECTED)
        self.assertEqual((done.code, done.out["changed"]), (0, "false"))
        self.assertEqual(done.report["noop"], [ID])
        self.assertEqual(snapshot(site), before)

    def test_rerender_fills_in_a_collect_summary_healed_later(self) -> None:
        directory, later = self.heal_dir()
        site = self.site()
        self.assertEqual(self.publish(site, heal=directory).code, 0)
        self.assertIsNone(index(site, later)["collect"])
        found = self.tmp / "found"
        found.mkdir()
        (found / f"bench-{later}.collect.json").write_text(
            json.dumps(
                {"rejected": [], "warnings": ["late"], "required_failures": ["k"]}
            )
        )
        done = self.publish(site, rerender=True, heal=found)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(
            index(site, later)["collect"],
            {"rejected": 0, "warnings": ["late"], "required_failures": ["k"]},
        )
        self.assertIs(entry(site, later)["raw"]["collect"], True)
        self.assertTrue(index(site, later)["links"]["raw_collect"])
        self.assertEqual(done.report["rebuilt"], [later])
        self.assertEqual(index(site), index(PLAIN_SITE))
        # Without the directory the summary is carried over.
        done = self.publish(site, rerender=True)
        self.assertEqual((done.code, done.out["changed"]), (0, "false"))
        self.assertEqual(index(site, later)["collect"]["warnings"], ["late"])

    def test_rerender_takes_the_raw_flags_from_the_asset_listing(self) -> None:
        site = self.site()
        assets = self.tmp / "assets.jsonl"
        line = {"tag": "bench-raw-2026-11", "name": f"bench-{ID}.logs.tar.gz"}
        assets.write_text(json.dumps(line) + "\n")
        done = self.publish(site, rerender=True, assets=assets)
        self.assertEqual(done.code, 0, done.stderr)
        raw = entry(site, ID)["raw"]
        self.assertEqual((raw["logs"], raw["collect"]), (True, False))
        links = index(site)["links"]
        self.assertTrue(links["raw_logs"])
        self.assertIsNone(links["raw_collect"])
        # The summary itself is derived data and stays.
        self.assertEqual(index(site)["collect"], index(PLAIN_SITE)["collect"])


@unittest.skipUnless(GIT, "git is not installed")
class Repair(Case):
    def test_a_damaged_index_is_left_then_rebuilt_by_rerender(self) -> None:
        site = self.git_site(published=True)
        target = site / SHARD_DIR / "index.json"
        good = target.read_bytes()
        target.write_text("{}\n")
        self.stage(site)
        git(site, "commit", "-q", "-m", "damage")

        done = self.publish(site, COLLECTED)
        self.assertEqual(done.code, 0, done.stderr)
        self.assertIn("--rerender is due", done.stderr)
        self.assertIn("--rerender is due", " ".join(done.report["warnings"]))
        self.assertEqual(git(site, "status", "--porcelain"), "")
        self.assertEqual(done.out["changed"], "false")

        # The collector summary is in no shard file: without the artifact at
        # hand it is carried over, and the damaged index had none to carry.
        other = self.tmp / "without-the-artifact"
        shutil.copytree(site, other)
        done = self.publish(other, rerender=True)
        self.assertEqual(done.code, 0, done.stderr)
        rebuilt = load_json(other / SHARD_DIR / "index.json")
        self.assertIsNone(rebuilt["collect"])
        self.assertEqual(
            dict(rebuilt, collect=json.loads(good)["collect"]), json.loads(good)
        )

        done = self.publish(site, COLLECTED, rerender=True, now="2026-11-23T00:00:00Z")
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(target.read_bytes(), good)
        self.stage(site)
        status = git(site, "status", "--porcelain").splitlines()
        self.assertEqual(
            sorted(status),
            sorted(
                [
                    f"M  {SHARD_DIR}/index.json",
                    f"M  {DATA}/manifest.json",
                    f"M  {DATA}/latest.json",
                ]
            ),
        )
        self.assertEqual(self.check(site).code, 1)
        self.assertEqual(self.check(site, "--rerender").code, 0)
        self.assertEqual(
            (done.out["generation"], done.out["generation_moved"]), ("2", "true")
        )
        doc = manifest(site)
        self.assertEqual(
            (doc["generation"], doc["updated_at"]), (2, "2026-11-23T00:00:00Z")
        )
        shard = entry(site, ID)
        self.assertEqual((shard["generation"], shard["published_at"]), (1, NOW))
        self.assertEqual(done.report["rebuilt"], [ID])
        self.assertEqual(done.out["shards"], "")

    def test_rerender_updates_the_file_entry_of_the_index(self) -> None:
        site = self.git_site(published=True)
        before = entry(site, ID)
        done = self.publish(site, rerender=True, default_branch="main")
        self.assertEqual(done.code, 0, done.stderr)
        after = entry(site, ID)
        data = (site / SHARD_DIR / "index.json").read_bytes()
        files = {item["name"]: item for item in after["files"]}
        self.assertEqual(
            (files["index.json"]["bytes"], files["index.json"]["sha256"]),
            (len(data), sha256(data)),
        )
        self.assertNotEqual(files["index.json"], before["files"][-1])
        self.assertEqual(after["bytes"], sum(item["bytes"] for item in after["files"]))
        self.assertEqual(after["bytes"], before["bytes"] - len("-amd"))
        self.assertEqual(after["files"][:5], before["files"][:5])
        self.assertTrue(
            index(site)["links"]["docs"].endswith("/blob/main/.ci/docs/amd-bench.md")
        )
        self.assertEqual(manifest(site)["links"]["docs"], index(site)["links"]["docs"])
        self.stage(site)
        status = git(site, "status", "--porcelain").splitlines()
        self.assertEqual({line[:2] for line in status}, {"M "})
        self.assertEqual(len(status), 5)
        self.assertEqual(self.check(site, "--rerender").code, 0)
        self.assertEqual(
            {item["path"] for item in done.report["verify"]},
            {"bench/index.html", f"{DATA}/latest.json", f"bench/runs/{ID}.html"},
        )

    def test_rerender_refuses_a_shard_whose_data_changed(self) -> None:
        site = self.git_site(published=True)
        for name in ("cases.ndjson.gz", "tests.ndjson.gz"):
            with self.subTest(name):
                target = site / SHARD_DIR / name
                good = target.read_bytes()
                target.write_bytes(pack(of_kind(fixture_records(), "run")))
                before = snapshot(site)
                done = self.publish(site, rerender=True)
                self.assertEqual(done.code, 4, done.stderr)
                self.assertEqual(snapshot(site), before)
                target.unlink()
                self.assertEqual(self.publish(site, rerender=True).code, 4)
                target.write_bytes(good)
        self.assertEqual(git(site, "status", "--porcelain"), "")

    def test_a_missing_run_page_is_reported_and_rerender_writes_it(self) -> None:
        site = self.git_site(published=True)
        page = site / f"bench/runs/{ID}.html"
        good = page.read_bytes()
        page.unlink()
        done = self.publish(site, COLLECTED)
        self.assertEqual(done.code, 0)
        self.assertIn("run page is missing", done.stderr)
        self.assertFalse(page.exists())
        done = self.publish(site, rerender=True)
        self.assertEqual(page.read_bytes(), good)
        self.assertEqual(done.out["generation_moved"], "false")
        self.assertEqual(done.out["changed"], "true")
        self.assertEqual(done.out["verify"], f"bench/runs/{ID}.html={sha256(good)}")
        self.assertEqual(git(site, "status", "--porcelain"), "")


class Fields(Case):
    def test_links_are_null_when_a_field_has_no_safe_form(self) -> None:
        for label, ident, null in (
            ("no sha", {"sha": None}, "commit"),
            ("a short sha", {"sha": "5a84605"}, "commit"),
            ("a path as source run", {"nightly_run_id": "1/../x"}, "source_run"),
            ("no source run", {"nightly_run_id": None}, "source_run"),
        ):
            with self.subTest(label):
                self.calls += 1
                directory = self.tmp / f"field-{self.calls}"
                later = build(directory, run=LATER_RUN, lanes=("vm2-net",), **ident)
                site = self.site(published=False)
                self.assertEqual(self.publish(site, directory).code, 0)
                links = index(site, later)["links"]
                self.assertIsNone(links[null])
                self.assertEqual(
                    sum(value is None for value in links.values()), 3, links
                )
                page = (site / pages.run_page_path(later)).read_text()
                self.assertNotIn('1/../x"', page)

    def test_a_duplicate_lane_keeps_the_later_attempt(self) -> None:
        def carry(records: list[dict[str, Any]]) -> None:
            old = vm2_run(records)
            new = dict(old, run_attempt=2, status="partial")
            new["run_uid"] = lib.run_uid(old["repo"], LATER_RUN, 2, "vm2-net", "10.1.0")
            records.append(new)
            test = dict(of_kind(records, "test")[0], run_uid=new["run_uid"])
            planted.append(test["test_id"])
            records.append(dict(test, status="fail", error_excerpt="probe died"))

        planted: list[str] = []

        directory = self.tmp / "carried"
        build(directory, run=LATER_RUN, lanes=("vm2-net",), change=carry)
        (path,) = directory.glob("bench-*.ndjson.gz")
        shard_id = f"{STAMP}-{SLUG}-{LATER_RUN}.a2"
        path.rename(directory / f"bench-{shard_id}.ndjson.gz")
        site = self.site(published=False)
        done = self.publish(site, directory)
        self.assertEqual(done.code, 0, done.stderr)
        shard = entry(site, shard_id)
        self.assertEqual(shard["lanes"][VM2], {"status": "partial", "n": [0] * 7})
        self.assertEqual(shard["tests"], [0, 1, 0, 0])
        rows = gzip.decompress(
            (site / DATA / shard["path"] / "runs.ndjson.gz").read_bytes()
        )
        self.assertEqual(len(rows.splitlines()), 5)
        self.assertEqual(
            shard["rows"], sum(item["rows"] or 0 for item in shard["files"])
        )
        doc = index(site, shard_id)
        (note,) = doc["warnings"]
        self.assertIn(f"more than one run of {VM2}", note)
        self.assertIn("attempt 2", note)
        self.assertIn(note, " ".join(done.report["warnings"]))
        (lane,) = [item for item in doc["lanes"] if item["lane"] == "vm2-net"]
        self.assertEqual((lane["status"], lane["tests"]), ("partial", [0, 1, 0, 0]))
        self.assertEqual(
            [
                (t["test_id"], t["status"], t["error_excerpt"])
                for t in doc["tests_attention"]
            ],
            [(planted[0], "fail", "probe died")],
        )
        self.assertEqual(doc["attention"], [])
        head = branch(site)
        self.assertEqual(head["tests_attention_total"], 1)
        self.assertEqual(
            sorted(head["tests_attention"][0]),
            sorted(["shard", "lane", "rocm_version", "suite", "test_id", "status"]),
        )
        self.assertEqual(head["tests"]["fail"], 1)

    def test_a_planted_failing_probe_is_listed(self) -> None:
        def plant(records: list[dict[str, Any]]) -> None:
            uid = vm2_run(records)["run_uid"]
            tests = [t for t in of_kind(records, "test") if t["run_uid"] == uid]
            tests[3].update(status="fail", error_excerpt="no device")
            tests[1].update(status="timeout")
            tests[0].update(status="skip")

        directory = self.tmp / "probe"
        later = build(directory, run=LATER_RUN, lanes=("vm2-net",), change=plant)
        site = self.site(published=False)
        self.assertEqual(self.publish(site, directory).code, 0)
        doc = index(site, later)
        self.assertEqual(
            sorted((t["status"], t["error_excerpt"]) for t in doc["tests_attention"]),
            [("fail", "no device"), ("timeout", "")],
        )
        self.assertEqual(
            [t["test_id"] for t in doc["tests_attention"]],
            sorted(t["test_id"] for t in doc["tests_attention"]),
        )
        (lane,) = [item for item in doc["lanes"] if item["lane"] == "vm2-net"]
        self.assertEqual(lane["tests"], [79, 1, 1, 1])
        self.assertEqual(doc["tests"], dict(zip(lib.TEST_STATUSES, [79, 1, 1, 1])))
        self.assertEqual(entry(site, later)["tests"], [79, 1, 1, 1])

    def test_a_run_of_another_matrix_is_published_with_a_warning(self) -> None:
        def other(records: list[dict[str, Any]]) -> None:
            vm2_run(records)["matrix_sha256"] = "0" * 64

        directory = self.tmp / "mixed"
        later = build(directory, run=LATER_RUN, lanes=("vm2-net",), change=other)
        site = self.site(published=False)
        self.assertEqual(self.publish(site, directory).code, 0)
        self.assertEqual(entry(site, later)["matrix_sha256"], MATRIX_SHA)
        (note,) = index(site, later)["warnings"]
        self.assertIn(f"{VM2} ran another matrix", note)


class Pending(Case):
    def listing(self, names: dict[str, list[str]]) -> pathlib.Path:
        path = self.tmp / "assets.jsonl"
        path.write_text(
            "".join(
                json.dumps({"tag": tag, "name": name}) + "\n"
                for tag, items in names.items()
                for name in items
            )
        )
        return path

    def pending(self, *args: Any, stdin: str = "") -> list[str]:
        done = tool("pending", *args, "--now", NOW, stdin=stdin)
        self.assertEqual(done.code, 0, done.stderr)
        return done.stdout.splitlines()

    def test_what_the_manifest_lacks_is_named_with_its_companions(self) -> None:
        new = f"{STAMP}-{SLUG}-{LATER_RUN}.a1"
        older = f"20261115T000000Z-main-amd-{LATER_RUN}.a1"
        old = f"20261101T000000Z-main-amd-{LATER_RUN}.a1"
        last = f"20261031T000000Z-main-amd-{LATER_RUN}.a1"
        tag, before = "bench-raw-2026-11", "bench-raw-2026-10"
        assets = self.listing(
            {
                tag: [
                    f"bench-{ID}.ndjson.gz",
                    f"bench-{ID}.collect.json",
                    f"bench-{older}.ndjson.gz",
                    f"bench-{new}.ndjson.gz",
                    f"bench-{new}.collect.json",
                    f"bench-{new}.logs.tar.gz",
                    f"bench-{old}.ndjson.gz",
                    f"bench-{last}.ndjson.gz",
                    f"bench-{STAMP}-..-1.a1.ndjson.gz",
                    f"matrix-{MATRIX_SHA}.json",
                    f"matrix-{'0' * 64}.json",
                    "matrix-short.json",
                    "notes.txt",
                ],
                before: [f"bench-{last}.ndjson.gz", f"matrix-{'1' * 64}.json"],
            }
        )
        site_manifest = PLAIN_SITE / DATA / "manifest.json"
        matrices = [
            f"{tag}\tmatrix-{'0' * 64}.json",
            f"{tag}\tmatrix-{MATRIX_SHA}.json",
        ]
        wanted = [
            f"{tag}\tbench-{new}.ndjson.gz",
            f"{tag}\tbench-{new}.collect.json",
            f"{tag}\tbench-{older}.ndjson.gz",
            *matrices,
        ]
        common = ("--assets", assets)
        self.assertEqual(self.pending("--manifest", site_manifest, *common), wanted)
        self.assertEqual(
            self.pending("--manifest", "-", *common, stdin=site_manifest.read_text()),
            wanted,
        )
        self.assertEqual(
            self.pending("--manifest", site_manifest, *common, "--limit", "1"),
            wanted[:2] + matrices,
        )
        self.assertEqual(
            self.pending("--manifest", site_manifest, *common, "--days", "3"),
            wanted[:2] + matrices,
        )
        # No manifest: everything in the window, the published run included.
        everything = [
            f"{tag}\tbench-{ID}.ndjson.gz",
            f"{tag}\tbench-{ID}.collect.json",
            *wanted,
        ]
        for missing in (self.tmp / "nowhere.json", "-"):
            self.assertEqual(self.pending("--manifest", missing, *common), everything)
        # A wider window reaches into the release of the month before.
        wide = self.pending("--manifest", site_manifest, *common, "--days", "30")
        self.assertEqual(
            wide[3:],
            [
                f"{tag}\tbench-{old}.ndjson.gz",
                f"{before}\tbench-{last}.ndjson.gz",
                *matrices,
                f"{before}\tmatrix-{'1' * 64}.json",
            ],
        )
        doc = manifest(PLAIN_SITE)
        doc["quarantine"] = [{"id": new, "reason": "x", "at": NOW}]
        self.assertEqual(
            self.pending("--manifest", "-", *common, stdin=json.dumps(doc)), wanted[2:]
        )
        doc["quarantine"].append({"id": older, "reason": "x", "at": NOW})
        self.assertEqual(
            self.pending("--manifest", "-", *common, stdin=json.dumps(doc)), []
        )

    def test_bad_usage(self) -> None:
        assets = self.listing({})
        for args, stdin in (
            (("--manifest", "-", "--assets", assets), "{oops"),
            (("--manifest", "-", "--assets", assets), "[]"),
            (("--manifest", "-", "--assets", self.tmp / "nowhere"), ""),
            (("--manifest", "-", "--assets", assets, "--now", "today"), ""),
        ):
            self.assertEqual(tool("pending", *args, stdin=stdin).code, 2)


class Served(Case):
    def served_site(self) -> pathlib.Path:
        site = self.site()
        for name, data in OTHERS.items():
            (site / name).parent.mkdir(parents=True, exist_ok=True)
            (site / name).write_bytes(data)
        return site

    def served(self, site: pathlib.Path, generation: int, *more: Any) -> Result:
        return tool(
            "served",
            "--base-url",
            site.as_uri(),
            "--generation",
            generation,
            "--timeout",
            "0",
            "--interval",
            "0",
            *more,
        )

    def expect(self, site: pathlib.Path, name: str) -> str:
        return f"{name}={sha256((site / name).read_bytes())}"

    def test_a_published_site_is_served(self) -> None:
        site = self.served_site()
        done = self.served(
            site,
            1,
            "--expect",
            self.expect(site, "bench/index.html"),
            "--expect",
            self.expect(site, f"bench/runs/{ID}.html"),
        )
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(self.served(site, 0).code, 0)

    def test_what_is_not_served_is_named(self) -> None:
        site = self.served_site()
        late = self.served(site, 2)
        self.assertEqual(late.code, 6)
        self.assertIn("manifest.json: generation 1", late.stderr)
        self.assertEqual(late.stderr.count("not served"), 1)

        wrong = self.served(site, 1, "--expect", f"bench/index.html={'0' * 64}")
        self.assertEqual(wrong.code, 6)
        self.assertIn("bench/index.html: other content", wrong.stderr)

        (site / "wheels" / "index.html").unlink()
        (site / "data" / "nightly" / "index.json").write_text("{oops")
        (site / DATA / "latest.json").unlink()
        broken = self.served(site, 1)
        self.assertEqual(broken.code, 6)
        self.assertEqual(broken.stderr.count("not served"), 3)
        self.assertIn("data/nightly/index.json: not JSON", broken.stderr)
        self.assertIn("wheels/index.html: ", broken.stderr)
        self.assertIn("latest.json: ", broken.stderr)

    def test_a_later_publication_does_not_fail_an_earlier_check(self) -> None:
        site = self.served_site()
        first = [
            self.expect(site, name)
            for name in ("bench/index.html", f"{DATA}/latest.json")
        ]
        page = self.expect(site, f"bench/runs/{ID}.html")
        build(self.tmp / "next", stamp="20261121T010101Z", run="44900000000")
        self.assertEqual(self.publish(site, self.tmp / "next").code, 0)
        self.assertEqual(manifest(site)["generation"], 2)
        args = [arg for item in (*first, page) for arg in ("--expect", item)]
        self.assertEqual(self.served(site, 1, *args).code, 0)
        # The files nothing rewrites keep their test, whatever is served.
        other = f"bench/runs/{ID}.html={'0' * 64}"
        late = self.served(site, 1, *args, "--expect", other)
        self.assertEqual(late.code, 6)
        self.assertEqual(late.stderr.count("not served"), 1)
        # At its own generation a publication must be served as it was pushed.
        own = self.served(site, 2, *args)
        self.assertEqual(own.code, 6)
        self.assertEqual(own.stderr.count("other content"), 2)

    def test_it_waits_until_the_site_catches_up(self) -> None:
        site = self.served_site()
        target = site / DATA / "manifest.json"
        good = target.read_bytes()
        target.write_text("{oops")
        sleeps: list[float] = []

        def sleep(seconds: float) -> None:
            sleeps.append(seconds)
            target.write_bytes(good)

        with mock.patch.object(publish.time, "sleep", sleep):
            done = tool(
                "served",
                "--base-url",
                site.as_uri() + "/",
                "--generation",
                "1",
                "--timeout",
                "60",
                "--interval",
                "2",
            )
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(sleeps, [2])

    def test_urls_carry_a_cache_buster_except_for_files(self) -> None:
        seen: list[str] = []

        def fetch(url: str) -> tuple[str, Optional[bytes]]:
            seen.append(url)
            return "200", b'{"generation": 7}'

        with mock.patch.object(publish, "fetch", fetch):
            done = tool(
                "served",
                "--base-url",
                "https://example.invalid/nixl",
                "--generation",
                "7",
                "--timeout",
                "0",
            )
        self.assertEqual(done.code, 0, done.stderr)
        self.assertEqual(len(seen), 6)
        self.assertRegex(seen[0], r"/nixl/data/bench/v1/manifest\.json\?g=7&t=[0-9]+$")
        self.assertRegex(seen[1], r"^https://example\.invalid/nixl/\?g=7&t=[0-9]+$")
        self.assertIn("/nixl/wheels/index.html?g=7", seen[3])

    def test_bad_usage(self) -> None:
        for args in (
            ("--base-url", "ftp://example.invalid/", "--generation", "1"),
            ("--base-url", "https://example.invalid/", "--generation", "x"),
            (
                "--base-url",
                "https://example.invalid/",
                "--generation",
                "1",
                "--expect",
                "index.html=" + "0" * 64,
            ),
            (
                "--base-url",
                "https://example.invalid/",
                "--generation",
                "1",
                "--expect",
                "bench/index.html=abc",
            ),
        ):
            self.assertEqual(tool("served", *args, "--timeout", "0").code, 2, args)


if __name__ == "__main__":
    unittest.main()
