# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of amd_bench_collect.py.

    python3 -m unittest discover -s .ci/scripts/tests -v
"""

from __future__ import annotations

import contextlib
import gzip
import hashlib
import io
import json
import os
import pathlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
from typing import Any, Optional
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_collect as collect  # noqa: E402
import amd_bench_lib as lib  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE_MATRIX = HERE / "fixtures" / "matrix"
RECORDS = HERE / "fixtures" / "records"
NAME = "bench-20261119T010203Z-feat-rocm-wheels-777.a1"
BOX_UID = "7f4df5a3339a63b08e00"
REQUIRED = "box/posix/AIO/buffered/WRITE/S"
OPTIONAL = "box/posix/URING/direct/READ/S"


def fake(*parts: str) -> str:
    """A secret-looking string, assembled so that no scanner sees it in here."""
    return "".join(parts)


AWS_KEY = fake("AKIA", "ABCDEFGHIJKLMNOP")
GH_TOKEN = fake("gh", "p_", "a1B2" * 9)
PLAIN = fake("hunter", "2-value")


def dump(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":"))


def load(path: pathlib.Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines()]


class Result:
    def __init__(self, code: int, out: pathlib.Path, stdout: str, stderr: str) -> None:
        self.code = code
        self.out = out
        self.stdout = stdout
        self.stderr = stderr
        report = out / "collect.json"
        self.report: dict[str, Any] = (
            json.loads(report.read_text()) if report.is_file() else {}
        )
        self.records: list[dict[str, Any]] = []
        archive = out / f"{NAME}.ndjson.gz"
        for path in [archive] if archive.is_file() else sorted(out.glob("*.ndjson.gz")):
            text = gzip.decompress(path.read_bytes()).decode("ascii")
            self.records += [json.loads(line) for line in text.splitlines()]

    def kind(self, kind: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record["kind"] == kind]

    def run(self, lane: str, rocm_version: str = "10.1.0") -> dict[str, Any]:
        (run,) = [
            record
            for record in self.kind("run")
            if (record["lane"], record["rocm_version"]) == (lane, rocm_version)
        ]
        return run

    def cases(self, lane: str, rocm_version: str = "10.1.0") -> list[dict[str, Any]]:
        uid = self.run(lane, rocm_version)["run_uid"]
        return [case for case in self.kind("case") if case["run_uid"] == uid]

    def case(self, key: str, rocm_version: str = "10.1.0") -> dict[str, Any]:
        (case,) = [
            case
            for case in self.cases(key.split("/", 1)[0], rocm_version)
            if (case["case_key"], case["variant"], case["round"]) == (key, "head", 0)
        ]
        return case

    def counts(self, lane: str, rocm_version: str = "10.1.0") -> dict[str, int]:
        (entry,) = [
            run
            for run in self.report["runs"]
            if (run["lane"], run["rocm_version"]) == (lane, rocm_version)
        ]
        return {status: n for status, n in entry["counts"].items() if n}

    def reasons(self) -> list[tuple[int, str, str]]:
        return [
            (item["line"], item["kind"], item["reason"])
            for item in self.report["rejected"]
        ]

    def every_byte(self) -> bytes:
        """What every output holds, archives unpacked."""
        data = [self.stdout.encode(), self.stderr.encode()]
        for path in sorted(self.out.iterdir()):
            raw = path.read_bytes()
            data.append(path.name.encode())
            if path.name.endswith(".tar.gz"):
                with tarfile.open(path) as tar:
                    for member in tar.getmembers():
                        data.append(member.name.encode())
                        stream = tar.extractfile(member)
                        data.append(stream.read() if stream else b"")
            elif path.suffix == ".gz":
                data.append(gzip.decompress(raw))
            else:
                data.append(raw)
        return b"\n".join(data)


class CollectCase(unittest.TestCase):
    matrix: lib.Matrix
    schema: dict[str, Any]
    matrix_sha: str

    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = lib.load_matrix(FIXTURE_MATRIX)
        cls.schema = lib.load_schema()
        cls.matrix_sha = lib.matrix_sha256(lib.matrix_manifest(cls.matrix))

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        self.inputs = self.root / "in"
        self.inputs.mkdir()
        self.outs = 0

    def box(self) -> list[dict[str, Any]]:
        return load(RECORDS / "box-10.1.0.ndjson")

    def net(
        self,
        rocm_version: str = "10.1.0",
        tier: str = "nightly",
        status: str = "complete",
        attempt: int = 1,
    ) -> list[dict[str, Any]]:
        """A net lane that passed every case of the tier, as the runner writes it."""
        lane = self.matrix.lanes["net"]
        run = self.box()[-1]
        run.update(
            run_uid=lib.run_uid("ROCm/nixl", "777", attempt, "net", rocm_version),
            run_attempt=attempt,
            status=status,
            tier=tier,
            lane="net",
            env_class=lane.env_class,
            timing_trust=lane.timing_trust,
            rocm_version=rocm_version,
            guest_kernel="6.8.0-90-generic",
            guest_vcpus=4,
        )
        run["env_fp"] = lib.env_fingerprint(run)
        records = [run]
        for order, case in enumerate(lib.select_cases(lane, tier)):
            profile = self.matrix.profiles[case.profile]
            record = lib.case_record(case, profile, run["run_uid"])
            record.update(
                order=order,
                status="pass",
                consistency="clean",
                exit_init=0,
                exit_tgt=0,
                transport_obs=case.transport,
                rows_seen=record["rows_expected"],
                log_ref=f"logs/{order:03d}-x",
            )
            records.append(record)
            # Written backwards: the collector sorts samples and counters.
            for block, batch in reversed(lib.sweep(lib.effective_flags(case, profile))):
                sample = lib.blank_record("sample")
                sample.update(
                    case_uid=record["case_uid"],
                    block_size=block,
                    batch_size=batch,
                    bw_gbps=block / 1e6,
                )
                records.append(sample)
            for name in ("proc_wall_s.tgt", "proc_wall_s.init", "pair_skew_s"):
                counter = lib.blank_record("counter")
                counter.update(
                    case_uid=record["case_uid"], name=name, value=1.25, unit="s"
                )
                records.append(counter)
        return records

    def artifact(
        self,
        name: str,
        records: list[Any],
        logs: Optional[dict[str, str]] = None,
    ) -> pathlib.Path:
        """A lane artifact; a record that is a str is written as that raw line."""
        directory = self.inputs / name
        directory.mkdir(parents=True)
        (directory / "records.ndjson").write_text(
            "".join(
                (record if isinstance(record, str) else dump(record)) + "\n"
                for record in records
            )
        )
        for path, text in (logs or {}).items():
            (directory / "logs" / path).parent.mkdir(parents=True, exist_ok=True)
            (directory / "logs" / path).write_text(text)
        return directory

    def collect(self, *args: str, inputs: bool = True) -> Result:
        self.outs += 1
        out = self.root / f"out{self.outs}"
        source = self.inputs if inputs else self.root / "nothing"
        source.mkdir(exist_ok=True)
        argv = ["--matrix", str(FIXTURE_MATRIX), "--out", str(out), *args]
        argv += ["--input", str(source)]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = collect.main(argv)
        out.mkdir(exist_ok=True)
        return Result(code, out, stdout.getvalue(), stderr.getvalue())

    def meta(self, **values: Any) -> str:
        path = self.root / "meta.json"
        path.write_text(json.dumps(values))
        return str(path)


class FixtureTest(CollectCase):
    def test_golden_records_are_what_the_library_derives(self) -> None:
        records = self.box()
        for record in records:
            self.assertEqual(lib.validate_record(record, self.schema), [])
        run = records[-1]
        self.assertEqual(run["run_uid"], BOX_UID)
        self.assertEqual(
            run["run_uid"], lib.run_uid("ROCm/nixl", "777", 1, "box", "10.1.0")
        )
        self.assertEqual(run["matrix_sha256"], self.matrix_sha)
        self.assertEqual(run["env_fp"], lib.env_fingerprint(run))
        by_key = {case.case_key: case for case in self.matrix.lanes["box"].cases}
        for record in records:
            if record["kind"] != "case":
                continue
            case = by_key[record["case_key"]]
            derived = lib.case_record(case, self.matrix.profiles[case.profile], BOX_UID)
            for name in ("case_uid", "params_sha", "flags", "rows_expected", "role"):
                self.assertEqual(record[name], derived[name], name)


class MergeTest(CollectCase):
    def test_two_lanes_merge_the_same_way_every_time(self) -> None:
        self.artifact("bench-records-box-10.1.0", self.box(), {"000-a/init.log": "a\n"})
        self.artifact("bench-records-net-10.1.0", self.net(), {"000-b/tgt.log": "b\n"})
        first = self.collect("--zstd", "never")
        second = self.collect("--zstd", "never")
        self.assertEqual((first.code, second.code), (0, 0), first.stderr)
        names = sorted(path.name for path in first.out.iterdir())
        self.assertEqual(
            names,
            sorted(
                [
                    f"{NAME}.ndjson.gz",
                    f"{NAME}.logs.tar.gz",
                    f"matrix-{self.matrix_sha}.json",
                    "collect.json",
                    "summary.md",
                ]
            ),
        )
        for name in names:
            self.assertEqual(
                (first.out / name).read_bytes(), (second.out / name).read_bytes(), name
            )
            if name.endswith(".gz"):
                # No timestamp in the gzip header, or a later run would differ.
                self.assertEqual((first.out / name).read_bytes()[4:8], bytes(4), name)
        self.assertEqual(first.report["outputs"], sorted(set(names) - {"collect.json"}))
        self.assertEqual(first.report["schema"], 1)
        self.assertEqual(first.report["matrix_sha256"], self.matrix_sha)
        self.assertEqual(first.report["rejected"], [])
        self.assertEqual(first.report["required_failures"], [])
        self.assertEqual(first.report["warnings"], [])
        self.assertIn(f"{NAME}.ndjson.gz: 84 record(s) of 3 run(s)", first.stdout)

    def test_every_record_is_valid_and_in_the_documented_order(self) -> None:
        self.artifact("net", self.net())
        self.artifact("box", self.box())
        result = self.collect()
        self.assertEqual(result.code, 0, result.stderr)
        for record in result.records:
            self.assertEqual(lib.validate_record(record, self.schema), [])
            self.assertEqual(tuple(record), lib.FIELDS[record["kind"]])
        runs = [(run["lane"], run["rocm_version"]) for run in result.kind("run")]
        self.assertEqual(runs, [("box", "10.1.0"), ("fab", "none"), ("net", "10.1.0")])
        kinds = [record["kind"] for record in result.records]
        box_end = kinds.index("run", 1)
        self.assertEqual(
            kinds[:box_end],
            ["run"] + ["case", "sample", "counter"] * 4 + ["case"] * 4 + ["test"] * 2,
        )
        orders = [case["order"] for case in result.cases("box")]
        self.assertEqual(orders, [0, 1, 2, 3, None, None, None, None])
        skipped = [case["case_key"] for case in result.cases("box")[4:]]
        self.assertEqual(skipped, sorted(skipped))
        sweep = result.cases("net")[-1]
        following = result.records[result.records.index(sweep) + 1 :]
        blocks = [row["block_size"] for row in following[:11]]
        self.assertEqual(blocks, [4096 << n for n in range(11)])
        self.assertEqual(
            [row["name"] for row in following[11:14]],
            ["pair_skew_s", "proc_wall_s.init", "proc_wall_s.tgt"],
        )
        self.assertEqual(len(result.kind("sample")), 4 + 6 + 2 * 11)

    def test_published_matrix_file_is_named_by_its_own_sha256(self) -> None:
        self.artifact("box", self.box())
        result = self.collect()
        data = (result.out / f"matrix-{self.matrix_sha}.json").read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), self.matrix_sha)
        self.assertFalse(data.endswith(b"\n"))
        manifest = json.loads(data)
        self.assertEqual(manifest, lib.matrix_manifest(self.matrix))
        self.assertEqual(len(manifest["tiers"]["pr"]["box"]), 8)

    def test_last_run_record_wins(self) -> None:
        records = self.box()
        self.assertEqual(
            (records[0]["status"], records[-1]["status"]), ("partial", "complete")
        )
        self.artifact("box", records)
        result = self.collect()
        run = result.run("box")
        self.assertEqual((run["status"], run["calib_gbps"]), ("complete", 9.25))
        self.assertEqual(len(result.kind("run")), 2)

    def test_run_that_never_finished_stays_partial(self) -> None:
        self.artifact("box", self.box()[:-1])
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(result.run("box")["status"], "partial")
        self.assertIsNone(result.run("box")["finished_at"])

    def test_same_run_in_two_artifacts_warns_and_keeps_the_last(self) -> None:
        self.artifact("a", self.box()[:5])
        self.artifact("b", self.box())
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(result.run("box")["status"], "complete")
        self.assertIn(
            f"run {BOX_UID} is in more than one artifact; kept in/b/records.ndjson",
            result.report["warnings"],
        )

    def test_two_runs_of_one_lane_and_version_warn(self) -> None:
        self.artifact("net-1", self.net())
        self.artifact("net-2", self.net(attempt=2))
        result = self.collect()
        self.assertEqual(
            result.report["warnings"],
            [
                "net ROCm 10.1.0 belongs to another bench run or attempt",
                "more than one run of net ROCm 10.1.0",
            ],
        )
        self.assertEqual(len(result.kind("run")), 3)

    def test_duplicate_case_record(self) -> None:
        records = self.box()
        case = next(r for r in records if r.get("case_key") == REQUIRED)
        self.artifact("box", records + [case])
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(len(result.cases("box")), 8)
        (warning,) = result.report["warnings"]
        self.assertEqual(
            warning,
            f"duplicate case record for {REQUIRED} ({case['case_uid']}) at "
            "in/box/records.ndjson:21; last kept",
        )
        # The rows written before the repeated record went with the first one.
        uid = case["case_uid"]
        self.assertEqual([r for r in result.kind("sample") if r["case_uid"] == uid], [])

    def test_conflicting_case_record_keeps_the_last_and_its_rows(self) -> None:
        records = self.box()
        index = next(i for i, r in enumerate(records) if r.get("case_key") == REQUIRED)
        first, sample = records[index], records[index + 1]
        again = {**first, "status": "fail", "error_class": "rows", "rows_seen": 0}
        late = {**sample, "bw_gbps": 0.5}
        self.artifact("box", records + [again, late])
        result = self.collect()
        self.assertEqual(result.code, 1)
        self.assertEqual(result.case(REQUIRED)["status"], "fail")
        self.assertIn("conflicting case record for", result.report["warnings"][0])
        rows = [r for r in result.records if r.get("case_uid") == first["case_uid"]]
        self.assertEqual([r["kind"] for r in rows], ["case", "sample"])
        self.assertEqual(rows[1]["bw_gbps"], 0.5)
        self.assertEqual(result.report["required_failures"], [REQUIRED])

    def test_rounds_and_variants_are_kept_beside_the_head_record(self) -> None:
        records = self.net()
        case = self.matrix.lanes["net"].cases[6]
        uid = records[0]["run_uid"]
        for variant, round_ in (("aa", 0), ("head", 1), ("head", 2)):
            extra = lib.case_record(
                case, self.matrix.profiles["N"], uid, variant=variant, round_=round_
            )
            extra.update(status="pass", order=20 + round_)
            records.append(extra)
        self.artifact("net", records)
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(result.counts("net"), {"pass": 11})
        same = [c for c in result.cases("net") if c["case_key"] == case.case_key]
        self.assertEqual(
            [(c["variant"], c["round"]) for c in same],
            [("head", 0), ("aa", 0), ("head", 1), ("head", 2)],
        )

    def test_a_repeat_round_does_not_stand_in_for_the_head_record(self) -> None:
        records = [r for r in self.net() if r["kind"] in ("run", "case")]
        case = records[1]
        case.update(
            round=1, case_uid=lib.case_uid(case["run_uid"], case["case_key"], "head", 1)
        )
        self.artifact("net", records)
        result = self.collect()
        self.assertEqual(result.code, 1)
        self.assertEqual(result.case(case["case_key"])["status"], "not-run")


class SynthesisTest(CollectCase):
    def drop(self, records: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
        uid = next(r["case_uid"] for r in records if r.get("case_key") == key)
        return [r for r in records if r.get("case_uid") != uid]

    def test_missing_case_is_not_run(self) -> None:
        self.artifact("box", self.drop(self.box(), OPTIONAL))
        result = self.collect()
        self.assertEqual(result.code, 0, result.stderr)
        case = result.case(OPTIONAL)
        lane_case = self.matrix.lanes["box"].cases[7]
        self.assertEqual(
            case, lib.case_record(lane_case, self.matrix.profiles["S"], BOX_UID)
        )
        self.assertEqual(
            (case["status"], case["order"], case["log_ref"]), ("not-run", None, None)
        )
        self.assertEqual(result.counts("box"), {"pass": 4, "skip": 3, "not-run": 1})
        # It has no order, so it sorts by key among the skipped ones.
        self.assertEqual(result.cases("box")[-2]["case_key"], OPTIONAL)

    def test_missing_required_case_fails_the_collection(self) -> None:
        self.artifact("box", self.drop(self.box(), REQUIRED))
        result = self.collect()
        self.assertEqual(result.code, 1)
        self.assertEqual(result.report["required_failures"], [REQUIRED])
        self.assertIn("1 required case(s) did not pass", result.stderr)

    def test_tier_of_the_run_decides_what_is_expected(self) -> None:
        self.artifact("box", self.box())
        self.assertEqual(len(self.collect().cases("box")), 8)
        records = self.box()
        for record in records:
            if record["kind"] == "run":
                record["tier"] = "nightly"
        self.inputs = self.root / "in2"
        self.inputs.mkdir()
        self.artifact("box", records)
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(
            result.counts("box"), {"pass": 4, "skip": 4, "not-run": 2, "blocked": 1}
        )
        blocked = result.case("box/libfabric/tcp/DD/WRITE/S")
        self.assertEqual(
            (blocked["status"], blocked["blocked_by"]), ("blocked", ["E8"])
        )
        self.assertEqual(result.case("box/obj/std/WRITE/S")["status"], "not-run")

    def test_expected_lane_that_sent_nothing_is_missing(self) -> None:
        self.artifact("box", self.box())
        result = self.collect(
            "--expect",
            "net=10.1.0,10.2.0",
            "--expect",
            "box=10.1.0",
            "--run-meta",
            self.meta(tier="nightly"),
        )
        self.assertEqual(result.code, 1)
        self.assertEqual(
            [(r["lane"], r["rocm_version"], r["status"]) for r in result.kind("run")],
            [
                ("box", "10.1.0", "complete"),
                ("fab", "none", "blocked"),
                ("net", "10.1.0", "missing"),
                ("net", "10.2.0", "missing"),
            ],
        )
        box, net = result.run("box"), result.run("net", "10.2.0")
        self.assertEqual(
            net["run_uid"], lib.run_uid("ROCm/nixl", "777", 1, "net", "10.2.0")
        )
        self.assertEqual(net["env_fp"], lib.env_fingerprint(net))
        self.assertEqual(
            (net["env_class"], net["timing_trust"]), ("gha-kvm-vm2", "emulated")
        )
        self.assertEqual(
            (net["matrix_sha256"], net["tier"]), (self.matrix_sha, "nightly")
        )
        for name in collect.IDENTITY_KEYS:
            if name != "tier":
                self.assertEqual(net[name], box[name], name)
        # What the lane's own environment would have told is not invented.
        for name in ("runner_image", "ucx_version", "calib_gbps", "started_at"):
            self.assertIsNone(net[name], name)
        self.assertEqual(result.counts("net", "10.2.0"), {"not-run": 8})
        self.assertEqual(result.report["required_failures"], ["net/ucx/tcp/DD/WRITE/S"])
        self.assertEqual(result.report["warnings"], [])

    def test_missing_lane_takes_the_tier_of_the_first_run_without_meta(self) -> None:
        self.artifact("box", self.box())
        result = self.collect("--expect", "net=10.1.0")
        self.assertEqual(result.code, 0)
        self.assertEqual(result.run("net")["tier"], "pr")
        self.assertEqual(result.cases("net"), [])

    def test_blocked_lane_without_a_run(self) -> None:
        self.artifact("net", self.net())
        result = self.collect()
        self.assertEqual(result.code, 0)
        run = result.run("fab", "none")
        self.assertEqual((run["status"], run["tier"]), ("blocked", "nightly"))
        self.assertEqual(run["env_class"], "gha-kvm-vm1-nvmeof")
        self.assertEqual(
            run["run_uid"], lib.run_uid("ROCm/nixl", "777", 1, "fab", "none")
        )
        cases = result.cases("fab", "none")
        self.assertEqual([case["status"] for case in cases], ["blocked"] * 3)
        self.assertEqual(
            {case["case_key"]: case["blocked_by"] for case in cases},
            {
                "fab/kv/WRITE/S": ["W9", "W13"],
                "fab/posix/AIO/direct/READ/S": ["W9"],
                "fab/posix/AIO/direct/WRITE/S": ["W9"],
            },
        )
        self.assertEqual(result.counts("fab", "none"), {"blocked": 3})

    def test_blocked_lane_stands_for_all_its_cases_in_any_tier(self) -> None:
        # The cases of fab are nightly ones; the box run is a PR one.
        self.artifact("box", self.box())
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(result.run("fab", "none")["tier"], "pr")
        self.assertEqual(result.counts("fab", "none"), {"blocked": 3})
        tiers = {case["tier"] for case in result.cases("fab", "none")}
        self.assertEqual(tiers, {"nightly"})

    def test_expected_blocked_lane_is_blocked_not_missing(self) -> None:
        self.artifact("net", self.net())
        result = self.collect("--expect", "fab=10.1.0")
        self.assertEqual(result.code, 0)
        self.assertEqual(result.run("fab", "10.1.0")["status"], "blocked")
        self.assertEqual(len(result.kind("run")), 2)

    def test_blocked_lane_that_did_run_is_not_synthesised(self) -> None:
        run = self.net()[0]
        run.update(
            run_uid="b" * 20,
            lane="fab",
            env_class="gha-kvm-vm1-nvmeof",
            status="partial",
        )
        self.artifact("fab", [run])
        result = self.collect()
        self.assertEqual(result.run("fab")["status"], "partial")
        self.assertEqual(result.counts("fab"), {"blocked": 3})
        self.assertEqual(len(result.kind("run")), 1)

    def test_active_lanes_nobody_expected_are_only_named(self) -> None:
        self.artifact("box", self.box())
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual([run["lane"] for run in result.report["runs"]], ["box", "fab"])
        summary = (result.out / "summary.md").read_text()
        self.assertIn("Lanes neither reported nor expected: net", summary)

    def test_meta_alone_describes_a_night_where_nothing_reported(self) -> None:
        meta = self.meta(
            stamp="20261120T000000Z",
            bench_run_id="800",
            run_attempt=3,
            repo="ROCm/nixl",
            slug="main/amd",
            tier="nightly",
            sha="abc",
            rocm_version="10.1.0",
        )
        result = self.collect(
            "--run-meta", meta, "--expect", "box=10.1.0", inputs=False
        )
        self.assertEqual(result.code, 1)
        name = "bench-20261120T000000Z-main_amd-800.a3"
        self.assertTrue((result.out / f"{name}.ndjson.gz").is_file())
        self.assertTrue((result.out / f"{name}.logs.tar.gz").is_file())
        with tarfile.open(result.out / f"{name}.logs.tar.gz") as tar:
            self.assertEqual(tar.getnames(), [])
        run = result.run("box")
        self.assertEqual(
            (run["status"], run["sha"], run["run_attempt"]), ("missing", "abc", 3)
        )
        self.assertEqual(
            run["run_uid"], lib.run_uid("ROCm/nixl", "800", 3, "box", "10.1.0")
        )
        self.assertEqual(result.counts("box"), {"not-run": 10, "blocked": 1})
        self.assertEqual(result.counts("fab", "none"), {"blocked": 3})
        for record in result.records:
            self.assertEqual(lib.validate_record(record, self.schema), [])


class RejectionTest(CollectCase):
    def test_bad_lines_are_rejected_and_the_rest_is_kept(self) -> None:
        bad = (RECORDS / "rejects.ndjson").read_text().splitlines()
        self.artifact("box", self.box() + bad)
        result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertEqual(
            sorted(result.reasons()),
            [
                (21, "", "json"),
                (22, "", "json"),
                (23, "", "schema:$.kind: unknown record kind"),
                (24, "sample", "orphan"),
                (25, "test", "orphan"),
                (26, "", "json"),
                (27, "counter", "schema:$: unexpected key 'colour'"),
                (28, "case", "orphan"),
                (29, "counter", "schema:$.value: not a finite number"),
            ],
        )
        self.assertEqual(
            {item["file"] for item in result.report["rejected"]},
            {"in/box/records.ndjson"},
        )
        self.assertEqual(result.counts("box"), {"pass": 4, "skip": 4})
        self.assertEqual(len(result.records), 23)
        self.assertIn("rejected in/box/records.ndjson:24: orphan", result.stderr)
        self.assertNotIn(
            "net/ucx/tcp/DD/WRITE/S", [c["case_key"] for c in result.kind("case")]
        )

    def test_rejection_outranks_a_required_failure(self) -> None:
        records = self.box()
        next(r for r in records if r.get("case_key") == REQUIRED)["status"] = "crash"
        self.artifact("box", records + ["{"])
        result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertEqual(result.report["required_failures"], [REQUIRED])
        self.assertTrue((result.out / f"{NAME}.ndjson.gz").is_file())

    def test_rows_of_a_rejected_case_are_orphans(self) -> None:
        records = self.box()
        index = next(i for i, r in enumerate(records) if r.get("case_key") == REQUIRED)
        records[index]["threads"] = 0
        self.artifact("box", records)
        result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertEqual(
            sorted(result.reasons()),
            [
                (index + 1, "case", "schema:$.threads: must be at least 1"),
                (index + 2, "sample", "orphan"),
                (index + 3, "counter", "orphan"),
            ],
        )
        self.assertEqual(result.case(REQUIRED)["status"], "not-run")

    def test_deeply_nested_line_is_rejected_not_fatal(self) -> None:
        nested = [
            '{"kind":"sample","x":' + "[" * depth + "]" * depth + "}"
            for depth in (990, 2000)
        ]
        self.artifact("box", self.box() + nested)
        result = self.collect()
        self.assertEqual(result.code, 3, result.stderr)
        self.assertEqual([r[2] for r in result.reasons()], ["json", "json"])
        self.assertEqual(result.counts("box")["pass"], 4)

    def test_excerpt_cut_inside_a_mask_is_kept(self) -> None:
        # What an older runner wrote: 500 characters ending inside a mask,
        # which masking again would make longer than the schema allows.
        records = self.box()
        case = next(r for r in records if r.get("case_key") == REQUIRED)
        case["error_excerpt"] = "x" * 480 + " --obj_secret_key=**"
        self.assertEqual(len(case["error_excerpt"]), 500)
        self.artifact("box", records)
        result = self.collect()
        self.assertEqual(result.report["rejected"], [])
        kept = result.case(REQUIRED)["error_excerpt"]
        self.assertEqual(kept, "x" * 480 + " --obj_secret_key=")
        self.assertEqual(lib.find_secrets(kept), [])

    def test_internal_error_is_exit_3_not_1(self) -> None:
        self.artifact("box", self.box())
        with mock.patch.object(collect, "link", side_effect=RuntimeError("boom")):
            result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertIn("internal error", result.stderr)

    def test_secret_is_rejected_and_never_written(self) -> None:
        records = self.box()
        leaky = dict(next(r for r in records if r.get("case_key") == OPTIONAL))
        leaky["error_excerpt"] = f"denied for {AWS_KEY}"
        token = lib.blank_record("test")
        token.update(
            run_uid=BOX_UID,
            suite="probe",
            test_id=f"gh/{GH_TOKEN}",
            status="fail",
        )
        nested = {**records[-1], "features": [f"x@{AWS_KEY}"]}
        self.artifact("box", records + [leaky, token, nested])
        result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertEqual(
            result.reasons(),
            [
                (21, "case", "secret:aws-key-id"),
                (22, "test", "secret:github-token"),
                (23, "run", "secret:aws-key-id"),
            ],
        )
        everything = result.every_byte()
        self.assertNotIn(AWS_KEY.encode(), everything)
        self.assertNotIn(GH_TOKEN.encode(), everything)
        # The earlier, clean record of the same case is the one that is kept.
        self.assertEqual(result.case(OPTIONAL)["error_excerpt"], "needs io_uring")
        self.assertEqual(result.run("box")["features"], [])

    def test_unredacted_cmdline_is_redacted_and_kept(self) -> None:
        records = self.box()
        case = next(r for r in records if r.get("case_key") == REQUIRED)
        case["cmdline"] = (
            f"S3_SECRET_KEY={PLAIN} nixlbench --obj_secret_key={PLAIN} --op_type=WRITE"
        )
        case["error_excerpt"] = (
            "OBJ S3 access key (--obj_access_key=key)" + " " * 20 + f": {PLAIN}"
        )
        self.artifact("box", records)
        result = self.collect()
        self.assertEqual(result.code, 0, result.stderr)
        kept = result.case(REQUIRED)
        self.assertEqual(
            kept["cmdline"],
            "S3_SECRET_KEY=*** nixlbench --obj_secret_key=*** --op_type=WRITE",
        )
        self.assertTrue(kept["error_excerpt"].endswith(": ***"))
        self.assertEqual(
            result.report["warnings"],
            ["2 field(s) in in/box/records.ndjson needed redaction"],
        )
        self.assertNotIn(PLAIN.encode(), result.every_byte())

    def test_secret_outside_the_redacted_fields_is_rejected(self) -> None:
        records = self.box()
        case = next(r for r in records if r.get("case_key") == OPTIONAL)
        case["flags"] = f"obj_bucket_name=b AWS_SECRET_ACCESS_KEY={PLAIN}"
        self.artifact("box", records)
        result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertEqual(result.reasons()[0][1:], ("case", "secret:secret-env"))
        self.assertNotIn(PLAIN.encode(), result.every_byte())
        self.assertEqual(result.case(OPTIONAL)["status"], "not-run")

    def test_orphans_when_the_run_record_is_missing(self) -> None:
        self.artifact("box", self.box()[1:-1])
        result = self.collect(
            "--run-meta", self.meta(stamp="20261119T010203Z", bench_run_id="777")
        )
        self.assertEqual(result.code, 3)
        self.assertEqual({reason for _, _, reason in result.reasons()}, {"orphan"})
        self.assertEqual(len(result.reasons()), 18)
        self.assertEqual([run["lane"] for run in result.kind("run")], ["fab"])


class LogsTest(CollectCase):
    def members(self, result: Result) -> dict[str, bytes]:
        found = {}
        with tarfile.open(result.out / f"{NAME}.logs.tar.gz") as tar:
            for member in tar.getmembers():
                self.assertTrue(member.isfile())
                self.assertEqual((member.mtime, member.uid, member.mode), (0, 0, 0o644))
                stream = tar.extractfile(member)
                assert stream is not None
                found[member.name] = stream.read()
        return found

    def test_log_ref_points_into_the_tarball(self) -> None:
        self.artifact(
            "bench-records-box",
            self.box(),
            {
                "000-box__posix__AIO__buffered__WRITE__S/init.stdout.log": "table\n",
                "000-box__posix__AIO__buffered__WRITE__S/init.stderr.log": "",
                "runner.log": "started\n",
            },
        )
        self.artifact("bench-records-net", self.net(), {"000-x/tgt.ucx.log": "ucx\n"})
        result = self.collect()
        self.assertEqual(result.code, 0, result.stderr)
        members = self.members(result)
        self.assertEqual(
            sorted(members),
            [
                "box-10.1.0/logs/000-box__posix__AIO__buffered__WRITE__S/init.stderr.log",
                "box-10.1.0/logs/000-box__posix__AIO__buffered__WRITE__S/init.stdout.log",
                "box-10.1.0/logs/runner.log",
                "net-10.1.0/logs/000-x/tgt.ucx.log",
            ],
        )
        ref = result.case(REQUIRED)["log_ref"]
        self.assertEqual(ref, "box-10.1.0/logs/000-box__posix__AIO__buffered__WRITE__S")
        self.assertEqual(members[f"{ref}/init.stdout.log"], b"table\n")
        self.assertEqual(
            result.case("net/ucx/tcp/DD/WRITE/S")["log_ref"], "net-10.1.0/logs/000-x"
        )
        self.assertIsNone(result.case(OPTIONAL)["log_ref"])

    def test_log_with_a_secret_is_left_out(self) -> None:
        directory = self.artifact(
            "box",
            self.box(),
            {
                "000-a/init.stdout.log": "fine\n",
                "000-a/init.stderr.log": f"export AWS_ACCESS_KEY_ID={AWS_KEY}\n",
                "001-b/init.stdout.log": f"nixlbench --obj_secret_key={PLAIN}\n",
                "001-b/init.stderr.log": "nixlbench --obj_secret_key=*** A_TOKEN=***\n",
            },
        )
        (directory / "logs" / "link.log").symlink_to(directory / "records.ndjson")
        result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertEqual(
            sorted(self.members(result)),
            [
                "box-10.1.0/logs/000-a/init.stdout.log",
                "box-10.1.0/logs/001-b/init.stderr.log",
            ],
        )
        self.assertEqual(
            result.report["rejected"],
            [
                {
                    "file": "box-10.1.0/logs/000-a/init.stderr.log",
                    "line": 0,
                    "kind": "log",
                    "reason": "secret:aws-key-id",
                },
                {
                    "file": "box-10.1.0/logs/001-b/init.stdout.log",
                    "line": 0,
                    "kind": "log",
                    "reason": "secret:secret-flag",
                },
            ],
        )
        everything = result.every_byte()
        self.assertNotIn(AWS_KEY.encode(), everything)
        self.assertNotIn(PLAIN.encode(), everything)
        self.assertEqual(result.counts("box"), {"pass": 4, "skip": 4})

    def test_binary_log_and_odd_version_string(self) -> None:
        records = self.box()
        for record in records:
            if record["kind"] == "run":
                record["rocm_version"] = "10.1.0 rc/1"
        directory = self.artifact("box", records, {"a.log": "x"})
        (directory / "logs" / "core.bin").write_bytes(bytes(range(256)))
        result = self.collect()
        self.assertEqual(result.code, 0, result.stderr)
        members = self.members(result)
        self.assertEqual(
            sorted(members),
            ["box-10.1.0_rc_1/logs/a.log", "box-10.1.0_rc_1/logs/core.bin"],
        )
        self.assertEqual(members["box-10.1.0_rc_1/logs/core.bin"], bytes(range(256)))
        self.assertTrue(
            result.case(REQUIRED, "10.1.0 rc/1")["log_ref"].startswith(
                "box-10.1.0_rc_1/logs/"
            )
        )


class OutcomeTest(CollectCase):
    def with_case(self, key: str, **change: Any) -> Result:
        records = self.box()
        next(r for r in records if r.get("case_key") == key).update(change)
        self.inputs = self.root / f"in-{self.outs}"
        self.inputs.mkdir()
        self.artifact("box", records)
        return self.collect()

    def test_required_case_that_did_not_pass_exits_1(self) -> None:
        for change in (
            {"status": "fail", "error_class": "rows"},
            {"status": "timeout", "error_class": "timeout"},
            {"status": "crash", "error_class": "signal"},
            {"status": "skip", "skip_reason": "budget"},
            {"status": "skip", "skip_reason": "device-suspect"},
            {"status": "skip", "skip_reason": "env"},
            {"status": "skip", "skip_reason": "aborted"},
        ):
            with self.subTest(change=change):
                result = self.with_case(REQUIRED, **change)
                self.assertEqual(result.code, 1)
                self.assertEqual(result.report["required_failures"], [REQUIRED])
                self.assertEqual(result.report["rejected"], [])

    def test_what_does_not_fail_the_collection(self) -> None:
        self.assertEqual(
            self.with_case(REQUIRED, status="skip", skip_reason="filter").code, 0
        )
        result = self.with_case(
            OPTIONAL, status="fail", skip_reason=None, error_class="rows"
        )
        self.assertEqual(result.code, 0)
        self.assertEqual(result.report["required_failures"], [])
        self.assertEqual(result.counts("box"), {"pass": 4, "skip": 3, "fail": 1})

    def test_matrix_mismatch_is_kept_and_exits_3(self) -> None:
        records = self.box()
        for record in records:
            if record["kind"] == "run":
                record["matrix_sha256"] = "f" * 64
        self.artifact("box", records)
        result = self.collect()
        self.assertEqual(result.code, 3)
        self.assertEqual(
            result.report["warnings"],
            [f"matrix-mismatch: box ROCm 10.1.0 ran matrix {'f' * 64}"],
        )
        self.assertEqual(result.report["rejected"], [])
        self.assertEqual(result.run("box")["matrix_sha256"], "f" * 64)
        self.assertEqual(result.counts("box"), {"pass": 4, "skip": 4})
        self.assertEqual(result.run("fab", "none")["matrix_sha256"], self.matrix_sha)

    def test_unexpected_case_and_lane_are_kept_and_reported(self) -> None:
        records = self.box()
        stray = dict(next(r for r in records if r.get("case_key") == OPTIONAL))
        stray.update(
            case_key="box/retired/WRITE/S",
            case_uid=lib.case_uid(BOX_UID, "box/retired/WRITE/S", "head", 0),
        )
        other = self.net()[0]
        other.update(run_uid="c" * 20, lane="lab")
        self.artifact("box", records + [stray, other])
        result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(
            result.report["warnings"],
            [
                "unexpected: box/retired/WRITE/S in box ROCm 10.1.0",
                "unexpected: lane of lab ROCm 10.1.0 is not in the matrix",
            ],
        )
        self.assertEqual(result.counts("box"), {"pass": 4, "skip": 5})
        self.assertEqual(result.cases("lab"), [])

    def test_summary(self) -> None:
        records = self.box()
        case = next(r for r in records if r.get("case_key") == REQUIRED)
        case.update(
            status="fail",
            error_class="consistency",
            error_excerpt="first | second\nthird " + "x" * 200,
        )
        records = [r for r in records if r.get("case_key") != OPTIONAL]
        self.artifact("box", records + ["{"])
        target = self.root / "step-summary.md"
        target.write_text("earlier step\n")
        result = self.collect("--summary-out", str(target), "--expect", "net=10.2.0")
        self.assertEqual(result.code, 3)
        text = (result.out / "summary.md").read_text()
        self.assertEqual(target.read_text(), "earlier step\n" + text)
        lines = text.splitlines()
        self.assertEqual(lines[0], f"## nixlbench collection {NAME}")
        self.assertIn(
            "| Lane | ROCm | Run | pass | fail | timeout | crash | skip | blocked "
            "| not-run |",
            lines,
        )
        self.assertIn("| box | 10.1.0 | complete | 3 | 1 | 0 | 0 | 3 | 0 | 1 |", lines)
        self.assertIn("| fab | none | blocked | 0 | 0 | 0 | 0 | 0 | 3 | 0 |", lines)
        self.assertIn("| net | 10.2.0 | missing | 0 | 0 | 0 | 0 | 0 | 0 | 0 |", lines)
        excerpt = ("first \\| second third " + "x" * 200)[:121]
        self.assertIn(
            f"| {REQUIRED} | 10.1.0 | fail (required) | consistency | {excerpt} |",
            lines,
        )
        self.assertIn(f"| {OPTIONAL} | 10.1.0 | not-run | no record |  |", lines)
        self.assertIn(
            "| box/posix/URING/direct/WRITE/S | 10.1.0 | skip | env | needs io_uring |",
            lines,
        )
        self.assertEqual(sum("| 10.1.0 | pass" in line for line in lines), 0)
        self.assertIn("Rejected records and logs: 1", lines)
        self.assertIn("- `in/box/records.ndjson:20` json", lines)
        self.assertEqual(lines[-1], f"Matrix: `{self.matrix_sha}`")
        self.assertTrue(text.endswith("\n"))
        self.assertNotIn("Warnings:", lines)

    def test_summary_lists_warnings_and_caps_the_table(self) -> None:
        records = self.box()
        for record in records:
            if record["kind"] == "run":
                record["matrix_sha256"] = "f" * 64
        self.artifact("box", records)
        with mock.patch.object(collect, "SUMMARY_CASES", 2):
            result = self.collect()
        lines = (result.out / "summary.md").read_text().splitlines()
        self.assertIn("Warnings:", lines)
        self.assertIn(
            f"- matrix-mismatch: box ROCm 10.1.0 ran matrix {'f' * 64}", lines
        )
        self.assertIn("2 more are in the records.", lines)
        self.assertEqual(
            sum(line.endswith("| skip | env | needs io_uring |") for line in lines), 2
        )


class ZstdTest(CollectCase):
    def tool(self, body: str) -> dict[str, str]:
        directory = self.root / "bin"
        directory.mkdir(exist_ok=True)
        path = directory / "zstd"
        path.write_text(f"#!/bin/sh\n{body}\n")
        path.chmod(0o755)
        return {"PATH": str(directory)}

    def test_never(self) -> None:
        self.artifact("box", self.box())
        with mock.patch.dict(os.environ, self.tool("exec /bin/cat")):
            result = self.collect("--zstd", "never")
        self.assertEqual(result.code, 0)
        self.assertEqual(list(result.out.glob("*.zst")), [])

    def test_auto_without_zstd_on_the_path(self) -> None:
        self.artifact("box", self.box())
        empty = self.root / "empty"
        empty.mkdir()
        with mock.patch.dict(os.environ, {"PATH": str(empty)}):
            result = self.collect("--zstd", "auto")
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual(list(result.out.glob("*.zst")), [])
        self.assertEqual(result.report["warnings"], [])
        self.assertEqual(len(result.records), 23)

    def test_auto_runs_zstd_on_the_same_bytes(self) -> None:
        self.artifact("box", self.box())
        # Stands in for zstd: records its arguments and passes stdin through.
        env = self.tool(f'echo "$@" > {self.root}/args; exec /bin/cat')
        with mock.patch.dict(os.environ, env):
            result = self.collect()
        self.assertEqual(result.code, 0, result.stderr)
        self.assertEqual((self.root / "args").read_text(), "-19 -q -c\n")
        self.assertIn(f"{NAME}.ndjson.zst", result.report["outputs"])
        self.assertEqual(
            (result.out / f"{NAME}.ndjson.zst").read_bytes(),
            gzip.decompress((result.out / f"{NAME}.ndjson.gz").read_bytes()),
        )

    def test_failing_zstd_is_a_warning(self) -> None:
        self.artifact("box", self.box())
        with mock.patch.dict(os.environ, self.tool("exit 7")):
            result = self.collect()
        self.assertEqual(result.code, 0)
        self.assertEqual(result.report["warnings"], ["zstd failed with status 7"])
        self.assertEqual(list(result.out.glob("*.zst")), [])


class UsageTest(CollectCase):
    def assert_refused(self, result: Result, message: str) -> None:
        self.assertEqual(result.code, 2)
        self.assertIn(message, result.stderr)
        self.assertEqual(list(result.out.iterdir()), [])

    def test_nothing_to_collect(self) -> None:
        self.assert_refused(
            self.collect(), "no run record was read and there is no --run-meta"
        )
        (self.inputs / "empty").mkdir()
        self.assert_refused(self.collect(), "no run record was read")

    def test_only_rejected_records_and_no_meta(self) -> None:
        self.artifact("box", ["{", "[]"])
        result = self.collect()
        self.assert_refused(result, "no run record was read")
        self.assertIn("rejected in/box/records.ndjson:1: json", result.stderr)

    def test_missing_input_directory_is_a_warning(self) -> None:
        self.artifact("box", self.box())
        result = self.collect("--input", str(self.root / "nowhere"))
        self.assertEqual(result.code, 0)
        self.assertEqual(
            result.report["warnings"],
            [f"input {self.root / 'nowhere'} is not a directory"],
        )

    def test_bad_matrix(self) -> None:
        self.artifact("box", self.box())
        out = self.root / "out"
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr):
            code = collect.main(
                [
                    "--matrix",
                    str(self.root),
                    "--input",
                    str(self.inputs),
                    "--out",
                    str(out),
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("no lane files", stderr.getvalue())
        self.assertFalse(out.exists())

    def test_bad_meta(self) -> None:
        self.artifact("box", self.box())
        path = self.root / "meta.json"
        for text, message in (
            ("{", "cannot read --run-meta"),
            ("[]", "is not a JSON object"),
            ('{"stamp": "20261119T010203Z", "colour": "red"}', "unknown key(s) colour"),
            (json.dumps({"reason": f"token {GH_TOKEN}"}), "looks like a secret"),
        ):
            with self.subTest(message=message):
                path.write_text(text)
                self.assert_refused(self.collect("--run-meta", str(path)), message)
        self.assertNotIn(GH_TOKEN, self.collect("--run-meta", str(path)).stderr)
        self.assert_refused(
            self.collect("--run-meta", str(self.root / "none.json")), "cannot read"
        )

    def test_meta_keys_are_the_runner_s(self) -> None:
        self.artifact("box", self.box())
        meta = {"rocm_version": "10.1.0", "ib_mtu": 4096, "tier": "pr"}
        self.assertLessEqual(set(meta), set(lib.META_KEYS))
        result = self.collect("--run-meta", self.meta(**meta))
        self.assertEqual(result.code, 0, result.stderr)

    def test_meta_that_cannot_make_a_run_record(self) -> None:
        for meta, message in (
            ({"tier": "nightly"}, "--run-meta must give 'stamp' and 'bench_run_id'"),
            (
                {"stamp": "today", "bench_run_id": "1"},
                "invalid run record: $.stamp: does not match",
            ),
            (
                {"stamp": "20261119T010203Z", "bench_run_id": 5},
                "invalid run record: $.bench_run_id: expected string, got integer",
            ),
        ):
            with self.subTest(meta=meta):
                result = self.collect("--run-meta", self.meta(**meta), inputs=False)
                self.assert_refused(result, message)

    def test_bad_expect(self) -> None:
        self.artifact("box", self.box())
        for value in ("lab=10.1.0", "box", "box=", "=10.1.0"):
            with self.subTest(value=value):
                self.assert_refused(
                    self.collect("--expect", value), f"--expect {value}"
                )

    def test_usage_errors_exit_2(self) -> None:
        for argv in (
            [],
            ["--input", "x"],
            ["--input", "x", "--out", "y", "--zstd", "x"],
        ):
            with self.subTest(argv=argv), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as caught:
                    collect.main(argv)
                self.assertEqual(caught.exception.code, 2)

    def test_runs_as_a_script_from_anywhere(self) -> None:
        script = HERE.parent / "amd_bench_collect.py"
        self.assertTrue(os.access(script, os.X_OK))
        done = subprocess.run(
            [sys.executable, str(script), "--help"],
            cwd=self.root,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("--expect LANE=ROCM[,ROCM...]", done.stdout)

    def test_default_matrix_is_the_one_beside_the_scripts(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            args = collect.parse_args(["--input", "x", "--out", "y"])
        self.assertEqual(args.matrix, HERE.parents[1] / "bench")
        self.assertEqual((args.zstd, args.expect, args.run_meta), ("auto", [], None))


if __name__ == "__main__":
    unittest.main()
