# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of amd_bench_lib.py and of .ci/bench/schema/v1.json.

    python3 -m unittest discover -s .ci/scripts/tests -v
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_lib as lib  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
FIXTURE_MATRIX = HERE / "fixtures" / "matrix"
REAL_MATRIX = HERE.parents[1] / "bench"
HEADER = "# test\n"
PROFILE_S = """
check_consistency = true
warmup_iter = 16
num_iter = 64
large_blk_iter_ftr = 4
total_buffer_size = 33554432
start_block_size = 65536
max_block_size = 65536
start_batch_size = 4
max_batch_size = 4
[amd]
name = "S"
rows = 1
"""
LANE_HEAD = """
schema = 1
lane = "one"
lane_id = "T-ONE"
env_class = "gha-container"
timing_trust = "container"
topology = "single"
"""
CASE = """
[[case]]
key = "one/posix/WRITE/S"
workload = "posix/WRITE/S"
profile = "S"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
"""
# Golden values of fixtures/matrix; they change only when it or the id
# derivations do, and either is a break in every published series.
MATRIX_SHA = "650d0ecb52a6e462d1a9a44b75a41aea19bfb008699780f61478559d0beb23a8"
RUN_UID = "7f4df5a3339a63b08e00"
CASE_UID = "4d6ca5e45c2ea6a470ad"
PARAMS_SHA = "a402a39ea760e692c25455dbe27d80e9d3b2180659339d93addf6046f087a4e7"
ENV_FP = "58c1046abd18ad6c288f"


def fake(*parts: str) -> str:
    """A secret-looking string, assembled so that no scanner sees it in here."""
    return "".join(parts)


SECRETS = {
    "aws-key-id": fake("AKIA", "ABCDEFGHIJKLMNOP"),
    "github-token": fake("gh", "p_", "a1B2" * 9),
    "private-key": fake("-----BEGIN RSA ", "PRIVATE KEY-----"),
    "azure-account-key": fake("Account", "Key=", "q1w2e3r4t5" * 3, "=="),
    "bearer": fake("Authorization: ", "Bearer ", "abc.def.ghi"),
    "secret-flag": fake("--obj_secret_key", "=", "hunter2"),
    "secret-env": fake("AWS_SECRET_ACCESS_KEY", "=", "hunter2"),
    "url-password": fake("redis://", "user:", "hunter2", "@127.0.0.1:6379"),
}


def write_matrix(
    root: pathlib.Path,
    lanes: dict[str, str],
    profiles: dict[str, str] | None = None,
) -> pathlib.Path:
    for directory, files in (
        ("profiles", {"S": PROFILE_S} if profiles is None else profiles),
        ("lanes", lanes),
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
        for name, text in files.items():
            (root / directory / f"{name}.toml").write_text(HEADER + text)
    return root


def valid_record(kind: str) -> dict[str, Any]:
    """blank_record plus the least that makes it a valid record."""
    record = lib.blank_record(kind)
    uid = "0123456789abcdef0123"
    for key in ("run_uid", "case_uid", "env_fp"):
        if key in record:
            record[key] = uid
    if kind == "run":
        record.update(
            status="complete",
            stamp="20261119T010203Z",
            bench_run_id="42",
            run_attempt=1,
            tier="nightly",
            repo="ROCm/nixl",
            lane="box",
            env_class="gha-container",
            timing_trust="container",
            rocm_version="10.1.0",
            matrix_sha256="0" * 64,
        )
    elif kind == "case":
        record.update(
            case_key="box/x",
            workload_key="x",
            params_sha="0" * 64,
            profile="S",
            role="info",
            tier="pr",
            variant="head",
            backend="POSIX",
            transport_req="none",
            init_mem="DRAM",
            tgt_mem="FILE",
            op="WRITE",
            scheme="pairwise",
            runtime="NONE",
            threads=1,
            attach="none",
            status="pass",
        )
    elif kind == "sample":
        record.update(block_size=4096, batch_size=1)
    elif kind == "counter":
        record.update(name="proc_wall_s.init", unit="s")
    else:
        record.update(suite="probe", test_id="ibv/start", status="pass")
    return record


class MatrixTest(unittest.TestCase):
    matrix: lib.Matrix

    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = lib.load_matrix(FIXTURE_MATRIX)

    def case(self, key: str) -> lib.Case:
        lane = self.matrix.lanes[key.split("/", 1)[0]]
        return next(case for case in lane.cases if case.case_key == key)

    def test_axes_expand_in_listed_order_last_axis_fastest(self) -> None:
        keys = [case.case_key for case in self.matrix.lanes["box"].cases[:8]]
        self.assertEqual(
            keys,
            [
                f"box/posix/{api}/{io}/{op}/S"
                for api in ("AIO", "URING")
                for io in ("buffered", "direct")
                for op in ("WRITE", "READ")
            ],
        )
        cases = self.matrix.lanes["box"].cases
        self.assertEqual([case.index for case in cases], list(range(len(cases))))
        self.assertEqual(sorted(self.matrix.lanes), ["box", "fab", "net"])

    def test_axis_sets_the_field_it_is_named_after(self) -> None:
        case = self.case("box/posix/URING/direct/READ/S")
        self.assertEqual((case.op, case.engine), ("READ", "URING"))
        self.assertEqual(
            case.flags, {"posix_api_type": "URING", "filepath": "{posix_dir}"}
        )
        self.assertEqual(case.workload_key, "posix/URING/direct/READ/S")
        self.assertEqual(self.case("net/ucx/rc/DD/WRITE/S").transport, "rc")

    def test_integer_axis_keeps_its_type(self) -> None:
        case = self.case("box/posix/AIO/buffered/WRITE/S/t2")
        self.assertEqual(case.threads, 2)
        self.assertEqual(case.flags["num_files"], 2)
        self.assertIsInstance(case.flags["num_files"], int)

    def test_mem_axis_sets_both_memories(self) -> None:
        case = self.case("net/ucx/tcp/DV/WRITE/S")
        self.assertEqual((case.init_mem, case.tgt_mem), ("DRAM", "VRAM"))
        case = self.case("net/ucx/rc/VV/WRITE/S")
        self.assertEqual((case.init_mem, case.tgt_mem), ("VRAM", "VRAM"))

    def test_when_applies_in_order_and_replaces_fields(self) -> None:
        direct = self.case("box/posix/AIO/direct/WRITE/S")
        self.assertTrue(direct.direct)
        self.assertEqual((direct.role, direct.required), ("gate", True))
        uring = self.case("box/posix/URING/direct/WRITE/S")
        self.assertTrue(uring.direct)
        self.assertEqual((uring.role, uring.required), ("info", False))
        self.assertEqual(uring.needs, ("io_uring",))
        # A list in match is any-of; a match may name a field an axis set.
        self.assertEqual(self.case("net/ucx/rc/DD/WRITE/S").attach, "net-rdma")
        self.assertEqual(self.case("net/ucx/tcp/DD/WRITE/S").attach, "net-tcp")
        self.assertTrue(self.case("net/ucx/tcp/DD/WRITE/S").required)
        self.assertFalse(self.case("net/ucx/tcp/DV/WRITE/S").required)

    def test_defaults_and_built_in_defaults(self) -> None:
        case = self.case("box/posix/AIO/buffered/WRITE/S")
        self.assertEqual((case.tier, case.timeout_s, case.tgt_mem), ("pr", 120, "FILE"))
        self.assertEqual(
            (case.runtime, case.scheme, case.threads), ("NONE", "pairwise", 1)
        )
        self.assertEqual(
            (case.transport, case.attach, case.status), ("none", "none", "active")
        )
        case = self.case("net/ucx/tcp/DD/WRITE/N")
        self.assertEqual(
            (case.tier, case.timeout_s, case.tgt_mem), ("nightly", 300, "DRAM")
        )
        self.assertEqual(
            (case.runtime, case.role, case.sentinel), ("ASIO", "track", False)
        )

    def test_needs_are_authored_plus_derived(self) -> None:
        self.assertEqual(self.case("net/ucx/tcp/DD/WRITE/S").needs, ("two_hosts",))
        self.assertEqual(
            self.case("net/ucx/rc/DV/WRITE/S").needs, ("gpu", "rdma", "two_hosts")
        )
        self.assertEqual(self.case("box/obj/std/WRITE/S").needs, ("s3",))
        # A pair in a single-host lane does not need two hosts.
        self.assertEqual(
            self.case("box/libfabric/tcp/DD/WRITE/S").needs, ("libfabric",)
        )

    def test_pairs_are_joined_sorted_by_pair_key(self) -> None:
        den = self.case("net/ucx/tcp/DD/WRITE/S")
        self.assertEqual(
            den.pair_key, "net/rc-tcp/DD/WRITE/S,net/vram-dram/tcp/WRITE/S"
        )
        self.assertEqual(den.pair_role, "den,den")
        num = self.case("net/ucx/tcp/VV/WRITE/S")
        self.assertEqual(
            (num.pair_key, num.pair_role), ("net/vram-dram/tcp/WRITE/S", "num")
        )
        self.assertEqual(self.case("net/ucx/tcp/DV/WRITE/S").pair_key, "")
        self.assertEqual(len(self.matrix.lanes["net"].pairs), 3)

    def test_blocked_case_and_blocked_lane(self) -> None:
        case = self.case("box/libfabric/tcp/DD/WRITE/S")
        self.assertEqual((case.status, case.blocked_by), ("blocked", ("E8",)))
        lane = self.matrix.lanes["fab"]
        self.assertEqual((lane.status, lane.blocked_by), ("blocked", ("W9",)))
        self.assertEqual({case.status for case in lane.cases}, {"blocked"})
        self.assertEqual(self.case("fab/posix/AIO/direct/READ/S").blocked_by, ("W9",))
        self.assertEqual(self.case("fab/kv/WRITE/S").blocked_by, ("W9", "W13"))

    def test_select_cases_is_cumulative_and_keeps_blocked(self) -> None:
        box = self.matrix.lanes["box"]
        self.assertEqual(len(lib.select_cases(box, "pr")), 8)
        nightly = lib.select_cases(box, "nightly")
        self.assertEqual(len(nightly), 11)
        self.assertIn("box/libfabric/tcp/DD/WRITE/S", [c.case_key for c in nightly])
        self.assertEqual(lib.select_cases(box, "weekly"), list(box.cases))
        self.assertEqual([c.index for c in nightly], sorted(c.index for c in nightly))
        with self.assertRaises(lib.BenchError):
            lib.select_cases(box, "hourly")

    def test_real_matrix_loads_and_yields_valid_records(self) -> None:
        if not any((REAL_MATRIX / "lanes").glob("*.toml")):
            self.skipTest(f"no lane files under {REAL_MATRIX} yet")
        matrix = lib.load_matrix(REAL_MATRIX)
        schema = lib.load_schema()
        manifest = lib.matrix_manifest(matrix)
        self.assertEqual(
            lib.matrix_sha256(manifest),
            lib.matrix_sha256(lib.matrix_manifest(lib.load_matrix(REAL_MATRIX))),
        )
        for lane in matrix.lanes.values():
            for case in lane.cases:
                record = lib.case_record(case, matrix.profiles[case.profile], "0" * 20)
                self.assertEqual(lib.validate_record(record, schema), [], case.case_key)
                self.assertEqual(lib.find_secrets(record), [], case.case_key)


class StructureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)

    def problems(
        self, lanes: dict[str, str], profiles: dict[str, str] | None = None
    ) -> list[str]:
        try:
            lib.load_matrix(write_matrix(self.root, lanes, profiles))
        except lib.MatrixError as exc:
            self.assertEqual(str(exc), "\n".join(exc.problems))
            return exc.problems
        return []

    def assert_problem(self, problems: list[str], *parts: str) -> None:
        self.assertTrue(
            any(all(part in problem for part in parts) for problem in problems),
            f"no problem with {parts} in {problems}",
        )

    def test_minimal_matrix_loads(self) -> None:
        self.assertEqual(self.problems({"one": LANE_HEAD + CASE}), [])

    def test_empty_directory(self) -> None:
        with self.assertRaises(lib.MatrixError) as caught:
            lib.load_matrix(self.root)
        self.assertEqual(len(caught.exception.problems), 2)

    def test_every_problem_is_reported_at_once(self) -> None:
        lane = (
            LANE_HEAD.replace('lane = "one"', 'lane = "two"')
            + 'colour = "red"\n'
            + CASE
            + CASE.replace("posix/WRITE/S", "posix/READ/S").replace(
                'backend = "POSIX"', 'backend = "TAPE"'
            )
            + '\n[[pair]]\nkey = "p"\nkind = "vram-dram"\n'
            'num = "one/posix/WRITE/S"\nden = "one/posix/WRITE/S"\n'
        )
        problems = self.problems({"one": lane})
        self.assert_problem(problems, "lanes/one.toml: -: ", "lane must be 'one'")
        self.assert_problem(problems, "lanes/one.toml: -: ", "unknown key 'colour'")
        self.assert_problem(problems, "one/posix/READ/S: ", "'backend' must be one of")
        self.assert_problem(problems, "lanes/one.toml: p: ", "must differ")
        self.assertEqual(len(problems), 4, problems)

    def test_toml_error_is_a_problem_not_a_crash(self) -> None:
        problems = self.problems({"one": LANE_HEAD + CASE, "bad": "lane = = 3"})
        self.assert_problem(problems, "lanes/bad.toml: -: cannot read")
        self.assertEqual(len(problems), 1)

    def test_lane_checks(self) -> None:
        for change, expected in (
            (("schema = 1", "schema = 2"), "schema must be 1"),
            (('topology = "single"', 'topology = "ring"'), "'topology' must be one of"),
            (('timing_trust = "container"', ""), "'timing_trust' must be one of"),
            (('lane_id = "T-ONE"', "lane_id = 7"), "'lane_id' must be a non-empty"),
            (("schema = 1", 'schema = 1\nstatus = "blocked"'), "if and only if"),
            (("schema = 1", 'schema = 1\nblocked_by = ["W1"]'), "if and only if"),
            (("schema = 1", 'schema = 1\naa_case = "one/nope"'), "aa_case 'one/nope'"),
            (("schema = 1", "schema = 1\n[defaults]\nkey = 'x'"), "unknown key 'key'"),
        ):
            with self.subTest(expected=expected):
                problems = self.problems({"one": LANE_HEAD.replace(*change) + CASE})
                self.assert_problem(problems, "lanes/one.toml: -: ", expected)

    def test_aa_case_must_be_active_and_run_nightly(self) -> None:
        head = LANE_HEAD + 'aa_case = "one/posix/WRITE/S"\n'
        self.assertEqual(self.problems({"one": head + CASE}), [])
        for extra in ('tier = "weekly"\n', 'status = "blocked"\nblocked_by = ["W1"]\n'):
            with self.subTest(extra=extra):
                problems = self.problems({"one": head + CASE + extra})
                self.assert_problem(problems, "aa_case must be an active case")

    def test_case_checks(self) -> None:
        for change, expected in (
            (('profile = "S"', ""), "'profile' is required"),
            (('profile = "S"', 'profile = "XL"'), "profile 'XL' does not exist"),
            (('op = "WRITE"', 'op = "ERASE"'), "'op' must be one of"),
            (('tgt_mem = "FILE"', ""), "'tgt_mem' is required for backend POSIX"),
            (('tgt_mem = "FILE"', 'tgt_mem = "FILE"\nthreads = 0'), "at least 1"),
            (('tgt_mem = "FILE"', 'tgt_mem = "FILE"\nthreads = true'), "an integer"),
            (('tgt_mem = "FILE"', 'tgt_mem = "FILE"\ntimeout_s = 481'), "between 1"),
            (('tgt_mem = "FILE"', 'tgt_mem = "FILE"\ntimeout_s = 0'), "between 1"),
            (('tgt_mem = "FILE"', 'tgt_mem = "FILE"\ndirect = "yes"'), "a boolean"),
            (
                ('tgt_mem = "FILE"', 'tgt_mem = "FILE"\nneeds = ["luck"]'),
                "unknown need",
            ),
            (
                ('tgt_mem = "FILE"', 'tgt_mem = "FILE"\nruntime = "ASIO"'),
                "runtime NONE",
            ),
            (
                ('tgt_mem = "FILE"', 'tgt_mem = "FILE"\nstatus = "blocked"'),
                "if and only",
            ),
            (
                ('tgt_mem = "FILE"', 'tgt_mem = "FILE"\nblocked_by = ["W1"]'),
                "if and only",
            ),
            (
                ('tgt_mem = "FILE"', 'tgt_mem = "FILE"\nenv = { A = 1 }'),
                "names to strings",
            ),
            (
                ('tgt_mem = "FILE"', 'tgt_mem = "FILE"\ncolour = 1'),
                "unknown key 'colour'",
            ),
            (('"one/posix/WRITE/S"', '"two/posix/WRITE/S"'), "key must match ^one/"),
            (('"one/posix/WRITE/S"', '"one/posix/{op}/S"'), "key must match ^one/"),
            (('"posix/WRITE/S"', '"one/posix/WRITE/S"'), "must not start with a lane"),
            (
                ('backend = "POSIX"', 'backend = "UCX"\nruntime = "NONE"'),
                "ASIO or ETCD",
            ),
        ):
            with self.subTest(expected=expected):
                problems = self.problems({"one": LANE_HEAD + CASE.replace(*change)})
                self.assert_problem(problems, "lanes/one.toml: ", expected)
                self.assertEqual(len(problems), 1, problems)

    def test_flag_checks(self) -> None:
        for flags, expected in (
            ("{ num_threads = 2 }", "flag 'num_threads' is set by a case dimension"),
            ('{ config_file = "x" }', "flag 'config_file' is set by a case dimension"),
            ("{ block_size = 1 }", "unknown flag 'block_size'"),
            ('{ num_iter = "64" }', "must be an integer or one placeholder"),
            ('{ num_iter = "{a}{b}" }', "must be an integer or one placeholder"),
            ("{ num_iter = true }", "must be an integer or one placeholder"),
            ("{ check_consistency = 1 }", "must be a boolean"),
            ("{ filepath = 3 }", "must be a string"),
            ("{ start_block_size = 0 }", "start_block_size must be a positive"),
            ('{ max_batch_size = "{n}" }', "max_batch_size must be a positive"),
        ):
            with self.subTest(flags=flags):
                problems = self.problems(
                    {"one": LANE_HEAD + CASE + f"flags = {flags}\n"}
                )
                self.assert_problem(problems, "one/posix/WRITE/S: ", expected)
        good = 'flags = { num_files = "{files}", filepath = "{self.dir}/x" }\n'
        self.assertEqual(self.problems({"one": LANE_HEAD + CASE + good}), [])

    def test_case_keys_are_unique_across_lanes(self) -> None:
        problems = self.problems({"one": LANE_HEAD + CASE + CASE})
        self.assert_problem(problems, "one/posix/WRITE/S: case key is not unique")
        self.assertEqual(len(problems), 1)

    def test_case_key_of_another_lane(self) -> None:
        other = LANE_HEAD.replace('"one"', '"two"') + CASE
        problems = self.problems({"one": LANE_HEAD + CASE, "two": other})
        self.assertEqual(
            problems,
            [
                "lanes/two.toml: one/posix/WRITE/S: case key is not unique "
                "(also in lanes/one.toml)"
            ],
        )

    def test_axes_and_when_checks(self) -> None:
        for extra, expected in (
            ("[case.axes]\nOp = ['a']\n", "axis name 'Op' must match"),
            ("[case.axes]\nflags = ['a']\n", "cannot be named like that case field"),
            ("[case.axes]\nx = []\n", "non-empty list"),
            ("[case.axes]\nx = [true]\n", "non-empty list"),
            ("[case.axes]\nmem = ['DX']\n", "two letters of D and V"),
            (
                "[[case.when]]\nmatch = { x = 1 }\nset = { tier = 'pr' }\n",
                "matches 'x'",
            ),
            ("[[case.when]]\nmatch = { op = 'READ' }\n", "needs a set table"),
            ("[[case.when]]\nset = { tier = 'pr' }\n", "needs a match table"),
            (
                "[[case.when]]\nmatch = { op = 'WRITE' }\nset = { key = 'k' }\n",
                "unknown key 'key' in when[0] set",
            ),
        ):
            with self.subTest(expected=expected):
                problems = self.problems({"one": LANE_HEAD + CASE + extra})
                self.assert_problem(problems, "one/posix/WRITE/S: ", expected)

    def test_defaults_merge_flags_and_env_key_wise(self) -> None:
        lane = (
            LANE_HEAD
            + '[defaults]\ntier = "pr"\nflags = { num_files = 1, filepath = "/a" }\n'
            + 'env = { A = "1" }\n'
            + CASE
            + 'flags = { filepath = "/b" }\nenv = { B = "2" }\n'
            + "[[case.when]]\nmatch = { op = 'WRITE' }\n"
            + 'set = { flags = { enable_pt = true }, env = { A = "3" }, tier = "weekly" }\n'
        )
        case = (
            lib.load_matrix(write_matrix(self.root, {"one": lane}))
            .lanes["one"]
            .cases[0]
        )
        self.assertEqual(
            case.flags, {"num_files": 1, "filepath": "/b", "enable_pt": True}
        )
        self.assertEqual(case.env, {"A": "3", "B": "2"})
        self.assertEqual(case.tier, "weekly")

    def test_pair_checks(self) -> None:
        pair = '\n[[pair]]\nkey = "p"\nkind = "{kind}"\nnum = "{num}"\nden = "{den}"\n'
        two = CASE + CASE.replace("WRITE", "READ")
        for values, expected in (
            (("warm-cold", "one/posix/WRITE/S", "one/posix/READ/S"), "pair kind"),
            (("rc-tcp", "one/nope", "one/posix/READ/S"), "pair num 'one/nope'"),
            (("rc-tcp", "one/posix/READ/S", "one/nope"), "pair den 'one/nope'"),
        ):
            with self.subTest(expected=expected):
                text = pair.format(kind=values[0], num=values[1], den=values[2])
                problems = self.problems({"one": LANE_HEAD + two + text})
                self.assert_problem(problems, "lanes/one.toml: p: ", expected)
        text = pair.format(
            kind="rc-tcp", num="one/posix/WRITE/S", den="one/posix/READ/S"
        )
        problems = self.problems({"one": LANE_HEAD + two + text + "weight = 2\n"})
        self.assert_problem(problems, "lanes/one.toml: p: unknown key 'weight' in pair")
        self.assertEqual(self.problems({"one": LANE_HEAD + two + text}), [])
        problems = self.problems({"one": LANE_HEAD + two + text + text})
        self.assert_problem(problems, "pair key is not unique")

    def test_profile_checks(self) -> None:
        for change, expected in (
            (('name = "S"', 'name = "T"'), "name must be 'S'"),
            (("rows = 1", "rows = 2"), "rows is 2 but the sweep is 1"),
            (("rows = 1", "rows = 1\nsize = 3"), "unknown key 'size' in [amd]"),
            (("num_iter = 64", 'num_iter = "many"'), "flag 'num_iter' must be"),
            (("num_iter = 64", "num_iter = 64\nnum_threads = 2"), "case dimension"),
            (
                ("rows = 1", "rows = 1\n[amd.when_vram]\nmax_block_size = 131072"),
                "rows is 1 but the sweep with when_vram is 2",
            ),
        ):
            with self.subTest(expected=expected):
                problems = self.problems(
                    {"one": LANE_HEAD + CASE}, {"S": PROFILE_S.replace(*change)}
                )
                self.assert_problem(problems, "profiles/S.toml: -: ", expected)


class FlagsTest(unittest.TestCase):
    matrix: lib.Matrix

    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = lib.load_matrix(FIXTURE_MATRIX)

    def flags(self, key: str) -> dict[str, lib.FlagValue]:
        lane = self.matrix.lanes[key.split("/", 1)[0]]
        case = next(case for case in lane.cases if case.case_key == key)
        return lib.effective_flags(case, self.matrix.profiles[case.profile])

    def test_storage_case(self) -> None:
        flags = self.flags("box/posix/URING/direct/WRITE/S")
        self.assertEqual(
            list(flags.items()),
            [
                ("backend", "POSIX"),
                ("op_type", "WRITE"),
                ("initiator_seg_type", "DRAM"),
                ("scheme", "pairwise"),
                ("num_threads", 1),
                ("storage_enable_direct", True),
                ("check_consistency", True),
                ("filepath", "{posix_dir}"),
                ("large_blk_iter_ftr", 4),
                ("max_batch_size", 4),
                ("max_block_size", 65536),
                ("num_iter", 64),
                ("posix_api_type", "URING"),
                ("start_batch_size", 4),
                ("start_block_size", 65536),
                ("total_buffer_size", 33554432),
                ("warmup_iter", 16),
            ],
        )

    def test_pair_case(self) -> None:
        flags = self.flags("net/ucx/rc/VV/WRITE/S")
        self.assertEqual(
            list(flags)[:9],
            [
                "backend",
                "runtime_type",
                "asio_address",
                "asio_port",
                "op_type",
                "initiator_seg_type",
                "target_seg_type",
                "scheme",
                "num_threads",
            ],
        )
        self.assertEqual(flags["asio_address"], "{asio_address}")
        self.assertEqual(flags["asio_port"], "{asio_port}")
        self.assertEqual(flags["target_seg_type"], "VRAM")
        self.assertNotIn("storage_enable_direct", flags)

    def test_etcd_runtime(self) -> None:
        case = self.matrix.lanes["net"].cases[0]
        case = dataclasses.replace(case, runtime="ETCD", scheme="tp")
        flags = lib.effective_flags(case, self.matrix.profiles["S"])
        self.assertEqual(flags["runtime_type"], "ETCD")
        self.assertEqual(flags["etcd_endpoints"], "{etcd_endpoints}")
        self.assertEqual(flags["benchmark_group"], "{benchmark_group}")
        self.assertNotIn("asio_address", flags)

    def test_precedence_profile_when_vram_case_dimension(self) -> None:
        self.assertEqual(
            self.flags("net/ucx/tcp/DD/WRITE/N")["total_buffer_size"], 268435456
        )
        self.assertEqual(
            self.flags("net/ucx/tcp/VV/WRITE/N")["total_buffer_size"], 67108864
        )
        case = self.matrix.lanes["net"].cases[-1]
        profile = self.matrix.profiles["N"]
        case = dataclasses.replace(
            case, flags={"total_buffer_size": 1 << 24, "num_iter": 32}
        )
        flags = lib.effective_flags(case, profile)
        self.assertEqual((flags["total_buffer_size"], flags["num_iter"]), (1 << 24, 32))
        self.assertEqual(flags["warmup_iter"], 16)

    def test_flag_order(self) -> None:
        names = ["warmup_iter", "num_threads", "filepath", "backend", "asio_port"]
        self.assertEqual(
            lib.flag_order(dict.fromkeys(names, 1)),
            ["backend", "asio_port", "num_threads", "filepath", "warmup_iter"],
        )

    def test_reserved_flags_are_never_authorable(self) -> None:
        self.assertEqual(set(lib.RESERVED_FLAGS) & set(lib.NIXLBENCH_FLAGS), set())
        self.assertLessEqual(set(lib.DIMENSION_FLAGS), lib.RESERVED_FLAGS)
        self.assertLessEqual(
            set(lib.SECRET_FLAGS) - set(lib.NIXLBENCH_FLAGS),
            {"azure_blob_connection_string"},
        )

    def test_sweep(self) -> None:
        self.assertEqual(
            lib.sweep(self.flags("box/posix/AIO/direct/READ/S")), [(65536, 4)]
        )
        rows = lib.sweep(self.flags("net/ucx/tcp/DD/WRITE/N"))
        self.assertEqual(len(rows), 11)
        self.assertEqual((rows[0], rows[-1]), ((4096, 4), (4194304, 4)))
        self.assertEqual(
            lib.sweep(
                {
                    "start_block_size": 4096,
                    "max_block_size": 10000,
                    "start_batch_size": 1,
                    "max_batch_size": 5,
                }
            ),
            [(4096, 1), (4096, 2), (4096, 4), (8192, 1), (8192, 2), (8192, 4)],
        )
        # nixlbench's own defaults: 4 KiB to 64 MiB, batch 1.
        self.assertEqual(len(lib.sweep({})), 15)
        self.assertEqual(lib.sweep({"start_block_size": 8, "max_block_size": 4}), [])
        for bad in (0, -4, True, "{x}"):
            with self.assertRaises(lib.BenchError):
                lib.sweep({"start_batch_size": bad})


class IdsTest(unittest.TestCase):
    matrix: lib.Matrix
    case: lib.Case
    profile: lib.Profile

    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = lib.load_matrix(FIXTURE_MATRIX)
        cls.case = cls.matrix.lanes["box"].cases[0]
        cls.profile = cls.matrix.profiles["S"]

    def test_canonical_json(self) -> None:
        self.assertEqual(
            lib.canonical_json({"b": [1, True, None], "a": "é"}),
            '{"a":"\\u00e9","b":[1,true,null]}',
        )
        with self.assertRaises(ValueError):
            lib.canonical_json(float("nan"))

    def test_golden_ids(self) -> None:
        uid = lib.run_uid("ROCm/nixl", "777", 1, "box", "10.1.0")
        self.assertEqual(uid, RUN_UID)
        self.assertEqual(
            lib.case_uid(uid, "box/posix/AIO/buffered/WRITE/S", "head", 0), CASE_UID
        )
        self.assertEqual(lib.params_sha(self.case, self.profile), PARAMS_SHA)
        self.assertEqual(
            lib.env_fingerprint(
                {"env_class": "gha-container", "rocm_version": "10.1.0"}
            ),
            ENV_FP,
        )

    def test_ids_separate_what_must_not_merge(self) -> None:
        base = ("ROCm/nixl", "777", 1, "box", "10.1.0")
        uids = {lib.run_uid(*base)}
        for position, other in enumerate(("x/y", "778", 2, "net", "10.2.0")):
            changed = list(base)
            changed[position] = other
            uids.add(lib.run_uid(*changed))  # type: ignore[arg-type]
        self.assertEqual(len(uids), 6)
        cases = {
            lib.case_uid(RUN_UID, key, variant, round_)
            for key in ("a", "b")
            for variant in lib.VARIANTS
            for round_ in (0, 1)
        }
        self.assertEqual(len(cases), 12)
        self.assertTrue(all(len(uid) == 20 for uid in uids | cases))

    def test_params_sha_ignores_the_environment_it_runs_in(self) -> None:
        with mock.patch.dict(os.environ, {"posix_dir": "/elsewhere", "UCX_TLS": "rc"}):
            self.assertEqual(lib.params_sha(self.case, self.profile), PARAMS_SHA)
        # It is a function of the templates, so it moves when one does.
        for change in (
            {"flags": {**self.case.flags, "filepath": "{other_dir}"}},
            {"env": {"UCX_RNDV_THRESH": "0"}},
            {"threads": 2},
            {"direct": True},
        ):
            other = dataclasses.replace(self.case, **change)
            self.assertNotEqual(lib.params_sha(other, self.profile), PARAMS_SHA)
        # Fields that do not reach the command line or the environment do not.
        other = dataclasses.replace(self.case, role="info", tier="weekly", timeout_s=9)
        self.assertEqual(lib.params_sha(other, self.profile), PARAMS_SHA)

    def test_env_fingerprint_reads_only_its_keys(self) -> None:
        run = {"env_class": "gha-container", "rocm_version": "10.1.0"}
        self.assertEqual(lib.env_fingerprint({**run, "stamp": "x", "sha": "y"}), ENV_FP)
        self.assertEqual(lib.env_fingerprint({**run, "ib_mtu": None}), ENV_FP)
        for key in lib.ENV_FP_KEYS:
            self.assertNotEqual(lib.env_fingerprint({**run, key: "changed"}), ENV_FP)


class ManifestTest(unittest.TestCase):
    def test_sha_is_the_sha256_of_the_published_bytes(self) -> None:
        manifest = lib.matrix_manifest(lib.load_matrix(FIXTURE_MATRIX))
        data = lib.canonical_json(manifest).encode("ascii")
        self.assertEqual(lib.matrix_sha256(manifest), hashlib.sha256(data).hexdigest())
        self.assertEqual(lib.matrix_sha256(manifest), MATRIX_SHA)
        self.assertEqual(json.loads(data), manifest)

    def test_shape(self) -> None:
        manifest = lib.matrix_manifest(lib.load_matrix(FIXTURE_MATRIX))
        self.assertEqual(sorted(manifest), ["lanes", "profiles", "schema", "tiers"])
        self.assertEqual(
            manifest["profiles"]["N"],
            {
                "flags": {
                    "check_consistency": True,
                    "warmup_iter": 16,
                    "num_iter": 128,
                    "large_blk_iter_ftr": 4,
                    "total_buffer_size": 268435456,
                    "start_block_size": 4096,
                    "max_block_size": 4194304,
                    "start_batch_size": 4,
                    "max_batch_size": 4,
                },
                "when_vram": {"total_buffer_size": 67108864},
                "rows": 11,
            },
        )
        net = manifest["lanes"]["net"]
        self.assertEqual(
            sorted(net),
            [
                "aa_case",
                "blocked_by",
                "cases",
                "env_class",
                "lane_id",
                "pairs",
                "status",
                "timing_trust",
                "topology",
            ],
        )
        self.assertEqual(
            net["cases"][0],
            {
                "case_key": "net/ucx/tcp/DD/WRITE/S",
                "workload_key": "ucx/tcp/DD/WRITE/S",
                "profile": "S",
                "tier": "nightly",
                "role": "gate",
                "required": True,
                "sentinel": False,
                "status": "active",
                "blocked_by": [],
                "params_sha": net["cases"][0]["params_sha"],
                "rows_expected": 1,
            },
        )
        self.assertRegex(net["cases"][0]["params_sha"], "^[0-9a-f]{64}$")
        self.assertEqual(net["cases"][-1]["rows_expected"], 11)
        self.assertEqual(
            net["pairs"][0],
            {
                "key": "net/vram-dram/tcp/WRITE/S",
                "kind": "vram-dram",
                "num": "net/ucx/tcp/VV/WRITE/S",
                "den": "net/ucx/tcp/DD/WRITE/S",
            },
        )
        self.assertEqual(manifest["lanes"]["fab"]["blocked_by"], ["W9"])
        self.assertEqual(list(manifest["tiers"]), list(lib.TIERS))
        self.assertEqual(manifest["tiers"]["pr"]["net"], [])
        self.assertEqual(len(manifest["tiers"]["pr"]["box"]), 8)
        self.assertEqual(len(manifest["tiers"]["nightly"]["box"]), 11)
        self.assertEqual(len(manifest["tiers"]["weekly"]["box"]), 13)
        nightly = manifest["tiers"]["nightly"]["box"]
        self.assertEqual(nightly, sorted(nightly))
        self.assertIn("box/libfabric/tcp/DD/WRITE/S", nightly)

    def test_sha_is_stable_and_moves_with_the_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp, "matrix")
            shutil.copytree(FIXTURE_MATRIX, root)
            self.assertEqual(
                lib.matrix_sha256(lib.matrix_manifest(lib.load_matrix(root))),
                MATRIX_SHA,
            )
            lane = root / "lanes" / "net.toml"
            text = lane.read_text()
            lane.write_text(text.replace('description = "Two guests"', ""))
            self.assertEqual(
                lib.matrix_sha256(lib.matrix_manifest(lib.load_matrix(root))),
                MATRIX_SHA,
                "a description is not part of what a run is expected to produce",
            )
            lane.write_text(text.replace('"DD", "VV", "DV"', '"DD", "VV", "DV", "VD"'))
            added = lib.matrix_manifest(lib.load_matrix(root))
            self.assertNotEqual(lib.matrix_sha256(added), MATRIX_SHA)
            self.assertIn("net/ucx/rc/VD/WRITE/S", added["tiers"]["nightly"]["net"])
            lane.write_text(text)
            profile = root / "profiles" / "S.toml"
            profile.write_text(
                profile.read_text().replace("num_iter = 64", "num_iter = 128")
            )
            self.assertNotEqual(
                lib.matrix_sha256(lib.matrix_manifest(lib.load_matrix(root))),
                MATRIX_SHA,
            )


class RecordTest(unittest.TestCase):
    matrix: lib.Matrix
    schema: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = lib.load_matrix(FIXTURE_MATRIX)
        cls.schema = lib.load_schema()

    def record(self, key: str, **kwargs: Any) -> dict[str, Any]:
        lane = self.matrix.lanes[key.split("/", 1)[0]]
        case = next(case for case in lane.cases if case.case_key == key)
        return lib.case_record(
            case, self.matrix.profiles[case.profile], RUN_UID, **kwargs
        )

    def test_blank_record_has_every_field_in_order(self) -> None:
        for kind, fields in lib.FIELDS.items():
            blank = lib.blank_record(kind)
            self.assertEqual(tuple(blank), fields)
            self.assertEqual(blank["kind"], kind)
        first = lib.blank_record("case")
        first["blocked_by"].append("W1")
        self.assertEqual(lib.blank_record("case")["blocked_by"], [])
        with self.assertRaises(lib.BenchError):
            lib.blank_record("row")

    def test_case_record_of_an_active_case(self) -> None:
        record = self.record("box/posix/URING/direct/WRITE/S")
        self.assertEqual(lib.validate_record(record, self.schema), [])
        expected = lib.blank_record("case")
        expected.update(
            run_uid=RUN_UID,
            case_uid=lib.case_uid(RUN_UID, "box/posix/URING/direct/WRITE/S", "head", 0),
            case_key="box/posix/URING/direct/WRITE/S",
            workload_key="posix/URING/direct/WRITE/S",
            params_sha=record["params_sha"],
            profile="S",
            role="info",
            tier="pr",
            variant="head",
            backend="POSIX",
            engine="URING",
            transport_req="none",
            init_mem="DRAM",
            tgt_mem="FILE",
            op="WRITE",
            scheme="pairwise",
            runtime="NONE",
            threads=1,
            direct=True,
            flags="filepath={posix_dir} posix_api_type=URING",
            total_buffer=33554432,
            attach="none",
            status="not-run",
            rows_expected=1,
        )
        self.assertEqual(record, expected)

    def test_case_record_variants_pairs_and_blocked(self) -> None:
        record = self.record("net/ucx/tcp/DD/WRITE/N", variant="aa", round_=2)
        self.assertEqual((record["variant"], record["round"]), ("aa", 2))
        self.assertEqual(
            record["case_uid"], lib.case_uid(RUN_UID, record["case_key"], "aa", 2)
        )
        self.assertEqual(
            (record["pair_key"], record["pair_role"]),
            ("net/vram-dram/tcp/WRITE/N", "den"),
        )
        self.assertEqual(
            (record["rows_expected"], record["total_buffer"]), (11, 268435456)
        )
        self.assertEqual(record["transport_req"], "tcp")
        self.assertEqual(
            self.record("net/ucx/tcp/VV/WRITE/N")["total_buffer"], 67108864
        )
        blocked = self.record("fab/kv/WRITE/S")
        self.assertEqual(
            (blocked["status"], blocked["blocked_by"]), ("blocked", ["W9", "W13"])
        )
        self.assertEqual(lib.validate_record(blocked, self.schema), [])

    def test_flags_text_masks_secrets_and_keeps_templates(self) -> None:
        record = self.record("box/obj/std/WRITE/S")
        self.assertEqual(
            record["flags"],
            "AWS_EC2_METADATA_DISABLED=true S3_SESSION_TOKEN=*** "
            "obj_access_key=*** obj_bucket_name={s3_bucket} obj_secret_key=*** "
            "obj_use_virtual_addressing=false",
        )
        self.assertEqual(lib.find_secrets(record), [])
        self.assertEqual(lib.validate_record(record, self.schema), [])

    def test_every_fixture_case_gives_a_valid_record(self) -> None:
        for lane in self.matrix.lanes.values():
            for case in lane.cases:
                record = self.record(case.case_key)
                self.assertEqual(lib.validate_record(record, self.schema), [])


class RedactionTest(unittest.TestCase):
    TEXTS = (
        ("nixlbench --obj_access_key=AK1 --obj_bucket_name=b", "AK1"),
        ("nixlbench --obj_secret_key SK2 --op_type=WRITE", "SK2"),
        ("nixlbench -obj_session_token=ST3", "ST3"),
        ("x --azure_blob_connection_string=Endpoint=sb://h;Key=CS4 y", "CS4"),
        ("export AWS_SECRET_ACCESS_KEY=SK5 REDIS_PASSWORD=PW6", "SK5"),
        ("GITHUB_TOKEN=T7 S3_ACCESS_KEY=AK8 REGISTRY_AUTH=A9 nixlbench", "AK8"),
        ("OBJ S3 access key (--obj_access_key=key)" + " " * 20 + ": AK10", "AK10"),
        ("OBJ S3 secret key (--obj_secret_key=key)" + " " * 20 + ": SK11", "SK11"),
        (
            "OBJ S3 session token (--obj_session_token=token)" + " " * 12 + ": ST12",
            "ST12",
        ),
        (
            "Azure Blob Storage connection string "
            "(--azure_blob_connection_string=connection-string): a b CS13",
            "CS13",
        ),
        # A quoted value is one value, blank and all: bash prints the command
        # of a job that a signal ended, quoted as it was given.
        ("timeout nixlbench --obj_secret_key='SK14 tail-SK14' --op_type=READ", "SK14"),
        ("AWS_SECRET_ACCESS_KEY='SK15 xSK15' aws s3 ls", "SK15"),
        ('X_TOKEN="T16 moreT16" cmd', "T16"),
        ("--obj_access_key='a'\"'\"'AK17 b' next", "AK17"),
        ("--obj_secret_key='SK18 never closed", "SK18"),
        ("aws --endpoint-url http://user:PW19@minio:9000 s3 ls", "PW19"),
        ("connecting to redis://:PW20@127.0.0.1:6379/0", "PW20"),
        (
            "[host:12] OBJ S3 secret key (--obj_secret_key=key)" + " " * 9 + ": SK21",
            "SK21",
        ),
    )

    def test_each_pattern_is_masked_and_idempotent(self) -> None:
        for text, secret in self.TEXTS:
            with self.subTest(text=text):
                once = lib.redact_text(text)
                self.assertNotIn(secret, once)
                self.assertIn("***", once)
                self.assertEqual(lib.redact_text(once), once)
                self.assertEqual(lib.find_secrets(once), [])

    def test_exact_forms(self) -> None:
        self.assertEqual(
            lib.redact_text("a --obj_access_key=AK --obj_secret_key SK b"),
            "a --obj_access_key=*** --obj_secret_key *** b",
        )
        self.assertEqual(lib.redact_text("A_TOKEN=x B=y"), "A_TOKEN=*** B=y")
        self.assertEqual(
            lib.redact_text("OBJ S3 secret key (--obj_secret_key=key)   : s3cr3t"),
            "OBJ S3 secret key (--obj_secret_key=***)   : ***",
        )
        # nixlbench pads a description to 60 columns; this one is longer, so
        # nothing but ": " stands between it and the value.
        self.assertEqual(
            lib.redact_text(
                "Azure Blob Storage connection string "
                "(--azure_blob_connection_string=connection-string): "
                "DefaultEndpointsProtocol=https;AccountName=a;Key=k=="
            ),
            "Azure Blob Storage connection string "
            "(--azure_blob_connection_string=***): ***",
        )
        self.assertEqual(
            lib.redact_text("OBJ S3 session token (--obj_session_token=token)   : "),
            "OBJ S3 session token (--obj_session_token=***)   : ",
        )
        self.assertEqual(
            lib.redact_text("--obj_secret_key=***x --obj_access_key ***y"),
            "--obj_secret_key=*** --obj_access_key ***",
        )

    def test_other_text_is_untouched(self) -> None:
        text = (
            "OBJ S3 bucket name (--obj_bucket_name=nixlbench-bucket)   : b\n"
            "UCX_TLS=rc,rocm UCX_PROTO_INFO=y nixlbench --backend=UCX\r\n"
            "OBJ S3 access key (--obj_access_key=***)   : ***\n"
            "token bucket secret\n"
        )
        self.assertEqual(lib.redact_text(text), text)
        self.assertEqual(lib.redact_text(""), "")
        url = "http://127.0.0.1:9000/bucket ssh://git@host:22/x mail a@b.c"
        self.assertEqual(lib.redact_text(url), url)

    def test_clip_never_cuts_a_mask(self) -> None:
        self.assertEqual(lib.clip("short"), "short")
        self.assertEqual(lib.clip("x" * 600), "x" * 500)
        for name in ("AWS_SESSION_TOKEN=", "--obj_secret_key=", "--obj_secret_key "):
            for pad in range(496 - len(name), 501 - len(name)):
                with self.subTest(name=name, pad=pad):
                    text = lib.redact_text("y" * (pad - 1) + f" {name}hunter2 tail")
                    cut = lib.clip(text)
                    self.assertLessEqual(len(cut), 500)
                    self.assertEqual(lib.redact_text(cut), cut)
                    self.assertEqual(lib.find_secrets(cut), [])
                    self.assertEqual(lib.clip(cut), cut)
        self.assertEqual(lib.clip("abcdef", 3), "abc")

    def test_lines_are_redacted_one_by_one(self) -> None:
        text = "\n".join(text for text, _ in self.TEXTS) + "\r\nlast X_SECRET=s\r\n"
        once = lib.redact_text(text)
        self.assertEqual(once.count("\n"), text.count("\n"))
        self.assertTrue(once.endswith("last X_SECRET=***\r\n"))
        for _, secret in self.TEXTS:
            self.assertNotIn(secret, once)
        self.assertEqual(lib.redact_text(once), once)

    def test_argv(self) -> None:
        argv = [
            "nixlbench",
            "--backend=OBJ",
            "--obj_access_key=AK1",
            "--obj_secret_key",
            "SK2",
            "-obj_session_token",
            "ST3",
            "--obj_bucket_name",
            "bucket",
            "AWS_SESSION_TOKEN=T4",
        ]
        once = lib.redact_argv(argv)
        self.assertEqual(
            once,
            [
                "nixlbench",
                "--backend=OBJ",
                "--obj_access_key=***",
                "--obj_secret_key",
                "***",
                "-obj_session_token",
                "***",
                "--obj_bucket_name",
                "bucket",
                "AWS_SESSION_TOKEN=***",
            ],
        )
        self.assertEqual(lib.redact_argv(once), once)
        self.assertEqual(lib.find_secrets(once), [])
        self.assertEqual(lib.redact_argv(["--obj_secret_key"]), ["--obj_secret_key"])


class FindSecretsTest(unittest.TestCase):
    def test_each_pattern_is_found_and_never_returned(self) -> None:
        self.assertEqual(
            sorted(SECRETS), sorted(name for name, _ in lib._SECRET_PATTERNS)
        )
        for name, secret in SECRETS.items():
            with self.subTest(pattern=name):
                record = {"kind": "case", "cmdline": f"run {secret} now", "n": 3}
                found = lib.find_secrets(record)
                self.assertIn(("$.cmdline", name), found)
                self.assertNotIn(secret, json.dumps(found))
                self.assertEqual(lib.find_secrets(f"x {secret}")[0], ("$", name))

    def test_paths(self) -> None:
        secret = SECRETS["aws-key-id"]
        found = lib.find_secrets({"a": [1, {"b": secret}], "c": None, "d": 4.5})
        self.assertEqual(found, [("$.a[1].b", "aws-key-id")])
        found = lib.find_secrets({"ok": 1, secret: "value"})
        self.assertEqual(found, [("$.<key 1>", "aws-key-id")])
        self.assertNotIn(secret, json.dumps(found))

    def test_clean_values(self) -> None:
        for value in (
            None,
            7,
            "nixlbench --obj_secret_key=*** --obj_access_key ***",
            "AWS_SECRET_ACCESS_KEY=*** A_TOKEN=***",
            "token=abc secret key AKIA1234",
            {"flags": "obj_secret_key=*** S3_SESSION_TOKEN=***"},
            ["UCX_TLS=tcp", "OBJ S3 secret key (--obj_secret_key=***)   : ***"],
        ):
            self.assertEqual(lib.find_secrets(value), [], value)

    def test_what_printconfig_prints_is_a_hit_until_redacted(self) -> None:
        line = "OBJ S3 secret key (--obj_secret_key=key)" + " " * 20 + ": ci"
        self.assertEqual(lib.find_secrets(line), [("$", "secret-flag")])
        self.assertEqual(lib.find_secrets(lib.redact_text(line)), [])


class SchemaTest(unittest.TestCase):
    schema: dict[str, Any]

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = lib.load_schema()

    def errors(self, record: dict[str, Any]) -> list[str]:
        return lib.validate_record(record, self.schema)

    def test_schema_file_matches_the_field_tables(self) -> None:
        self.assertEqual(self.schema["x-schema-version"], lib.SCHEMA)
        self.assertEqual(self.schema["x-schema-minor"], lib.SCHEMA_MINOR)
        self.assertEqual(list(self.schema["$defs"]), list(lib.FIELDS))
        self.assertEqual(
            self.schema["oneOf"], [{"$ref": f"#/$defs/{kind}"} for kind in lib.FIELDS]
        )
        for kind, fields in lib.FIELDS.items():
            node = self.schema["$defs"][kind]
            self.assertEqual(tuple(node["required"]), fields, kind)
            self.assertEqual(tuple(node["properties"]), fields, kind)
            self.assertIs(node["additionalProperties"], False)
            self.assertEqual(node["type"], "object")
            self.assertEqual(node["properties"]["kind"], {"const": kind})

    def test_schema_enums_are_the_library_constants(self) -> None:
        for kind, name, values in (
            ("run", "status", lib.RUN_STATUSES),
            ("run", "tier", lib.TIERS),
            ("run", "timing_trust", lib.TIMING_TRUST),
            ("case", "role", lib.ROLES),
            ("case", "tier", lib.TIERS),
            ("case", "variant", lib.VARIANTS),
            ("case", "backend", lib.BACKENDS),
            ("case", "transport_req", lib.TRANSPORTS),
            ("case", "init_mem", lib.INIT_MEMS),
            ("case", "tgt_mem", lib.TGT_MEMS),
            ("case", "op", lib.OPS),
            ("case", "scheme", lib.SCHEMES),
            ("case", "runtime", lib.RUNTIMES),
            ("case", "attach", lib.ATTACH),
            ("case", "status", lib.CASE_STATUSES),
            ("case", "error_class", lib.ERROR_CLASSES),
            ("test", "status", lib.TEST_STATUSES),
        ):
            enum = self.schema["$defs"][kind]["properties"][name]["enum"]
            self.assertEqual(tuple(enum), values, f"{kind}.{name}")
        properties = self.schema["$defs"]["case"]["properties"]
        self.assertEqual(properties["consistency"]["enum"], [*lib.CONSISTENCY, None])
        self.assertEqual(properties["skip_reason"]["enum"], [*lib.SKIP_REASONS, None])

    def test_blank_defaults_follow_the_schema(self) -> None:
        for kind in lib.FIELDS:
            properties = self.schema["$defs"][kind]["properties"]
            for name, value in lib.blank_record(kind).items():
                types = properties[name].get("type", [])
                types = [types] if isinstance(types, str) else types
                if "null" in types:
                    self.assertIsNone(value, f"{kind}.{name}")
                elif "const" in properties[name]:
                    self.assertEqual(value, properties[name]["const"])
                else:
                    self.assertIn(lib._json_type(value), types + ["integer"], name)

    def test_valid_records(self) -> None:
        for kind in lib.FIELDS:
            with self.subTest(kind=kind):
                self.assertEqual(self.errors(valid_record(kind)), [])
        self.assertNotEqual(self.errors(lib.blank_record("run")), [])

    def test_full_records(self) -> None:
        case = valid_record("case")
        case.update(
            order=7,
            transport_obs="rc+rocm_copy",
            consistency="clean",
            skip_reason=None,
            exit_init=0,
            exit_tgt=-2,
            duration_s=3,
            rows_seen=1,
            load1_before=0.25,
            steal_pct=0.0,
            cmdline="nixlbench --backend=POSIX",
            log_ref="logs/007-x",
            started_at="2026-11-19T01:02:03Z",
            error_excerpt="x" * 500,
            blocked_by=[],
        )
        self.assertEqual(self.errors(case), [])
        run = valid_record("run")
        run.update(features=["a@b"], degraded=False, calib_gbps=4, ib_mtu=1024)
        self.assertEqual(self.errors(run), [])
        sample = valid_record("sample")
        sample.update(bw_gbps=4.429712, lat_avg_us=3.7, agg_bw_gbps=None)
        self.assertEqual(self.errors(sample), [])

    def test_rejections_for_every_kind(self) -> None:
        for kind in lib.FIELDS:
            good = valid_record(kind)
            last = lib.FIELDS[kind][-1]
            with self.subTest(kind=kind, why="missing key"):
                record = {k: v for k, v in good.items() if k != last}
                self.assertEqual(self.errors(record), [f"$: missing key '{last}'"])
            with self.subTest(kind=kind, why="extra key"):
                self.assertEqual(
                    self.errors({**good, "extra": 1}), ["$: unexpected key 'extra'"]
                )
            with self.subTest(kind=kind, why="wrong kind const"):
                self.assertEqual(
                    self.errors({**good, "kind": "nope"}),
                    ["$.kind: unknown record kind"],
                )

    def test_rejections_by_cause(self) -> None:
        for kind, change, expected in (
            ("case", {"round": True}, "$.round: expected integer, got boolean"),
            ("case", {"threads": 1.0}, "$.threads: expected integer, got number"),
            ("case", {"required": 1}, "$.required: expected boolean, got integer"),
            ("case", {"duration_s": True}, "$.duration_s: expected number or null"),
            ("case", {"status": "passed"}, "$.status: must be one of"),
            ("case", {"skip_reason": "tired"}, "$.skip_reason: must be one of"),
            ("case", {"status": None}, "$.status: expected string, got null"),
            ("case", {"blocked_by": None}, "$.blocked_by: expected array, got null"),
            ("case", {"blocked_by": ["W1", 2]}, "$.blocked_by[1]: expected string"),
            ("case", {"threads": 0}, "$.threads: must be at least 1"),
            ("case", {"round": -1}, "$.round: must be at least 0"),
            ("case", {"error_excerpt": "x" * 501}, "$.error_excerpt: longer than 500"),
            ("case", {"case_key": ""}, "$.case_key: shorter than 1"),
            ("case", {"case_uid": "xyz"}, "$.case_uid: does not match"),
            ("case", {"params_sha": "0" * 63}, "$.params_sha: does not match"),
            ("case", {"started_at": "yesterday"}, "$.started_at: does not match"),
            ("case", {"flags": {"a": 1}}, "$.flags: expected string, got object"),
            ("run", {"schema": 2}, "$.schema: must be 1"),
            ("run", {"schema": True}, "$.schema: must be 1"),
            ("run", {"stamp": "2026-11-19"}, "$.stamp: does not match"),
            ("run", {"stamp": "20261119T010203Z\n"}, "$.stamp: does not match"),
            ("run", {"stamp": "٢٠٢٦1119T010203Z"}, "$.stamp: does not match"),
            ("run", {"run_uid": "0" * 20 + "\n"}, "$.run_uid: does not match"),
            ("run", {"matrix_sha256": "0" * 64 + "\n"}, "$.matrix_sha256: does not"),
            ("run", {"run_attempt": 0}, "$.run_attempt: must be at least 1"),
            ("run", {"features": "a"}, "$.features: expected array, got string"),
            ("run", {"lane": None}, "$.lane: expected string, got null"),
            ("run", {"degraded": 0}, "$.degraded: expected boolean or null"),
            ("run", {"calib_gbps": float("inf")}, "$.calib_gbps: not a finite number"),
            (
                "sample",
                {"block_size": None},
                "$.block_size: expected integer, got null",
            ),
            ("sample", {"bw_gbps": "fast"}, "$.bw_gbps: expected number or null"),
            ("counter", {"value": None}, "$.value: expected number, got null"),
            ("counter", {"deterministic": "no"}, "$.deterministic: expected boolean"),
            ("test", {"status": "blocked"}, "$.status: must be one of"),
        ):
            with self.subTest(kind=kind, change=change):
                errors = self.errors({**valid_record(kind), **change})
                self.assertEqual(len(errors), 1, errors)
                self.assertTrue(errors[0].startswith(expected), errors[0])

    def test_errors_never_quote_the_value(self) -> None:
        secret = "s3cr3t-value"
        record = {**valid_record("case"), "status": secret, "case_uid": secret}
        record["blocked_by"] = [7, secret * 2]
        errors = self.errors(record)
        self.assertEqual(len(errors), 3)
        self.assertNotIn(secret, "\n".join(errors))

    def test_not_a_record(self) -> None:
        self.assertEqual(self.errors({}), ["$.kind: unknown record kind"])
        self.assertEqual(self.errors({"kind": 3}), ["$.kind: unknown record kind"])
        self.assertEqual(lib.validate_record([], self.schema), ["$: expected object"])  # type: ignore[arg-type]

    def test_ref_and_remaining_keywords(self) -> None:
        schema = {
            "$defs": {
                "test": {
                    "type": "object",
                    "properties": {
                        "kind": {"const": "test"},
                        "n": {"$ref": "#/$defs/small"},
                        "tags": {
                            "type": "array",
                            "minItems": 1,
                            "items": {"type": "string"},
                        },
                    },
                },
                "small": {"type": "number", "minimum": 0, "maximum": 9},
            }
        }
        check = lib.validate_record
        self.assertEqual(check({"kind": "test", "n": 9, "tags": ["a"]}, schema), [])
        self.assertEqual(check({"kind": "test", "anything": 1}, schema), [])
        self.assertEqual(
            check({"kind": "test", "n": 9.5, "tags": []}, schema),
            ["$.n: must be at most 9", "$.tags: fewer than 1 items"],
        )
        self.assertEqual(
            check({"kind": "small"}, schema), ["$.kind: unknown record kind"]
        )

    def test_unsupported_schema_is_refused_at_load(self) -> None:
        good = json.loads(json.dumps(self.schema))
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp, "schema.json")

            def load(change: Any) -> dict[str, Any]:
                schema = json.loads(json.dumps(good))
                change(schema)
                path.write_text(json.dumps(schema))
                return lib.load_schema(path)

            self.assertEqual(load(lambda s: None), good)
            for change, expected in (
                (
                    lambda s: s.update(definitions={}),
                    "unsupported keyword 'definitions'",
                ),
                (
                    lambda s: s["$defs"]["run"].update(anyOf=[]),
                    "unsupported keyword 'anyOf'",
                ),
                (
                    lambda s: s["$defs"]["case"]["properties"]["order"].update(
                        exclusiveMinimum=0
                    ),
                    "unsupported keyword 'exclusiveMinimum' at case.order",
                ),
                (
                    lambda s: s["$defs"]["run"]["properties"]["features"][
                        "items"
                    ].update(format="uri"),
                    "unsupported keyword 'format' at run.features[]",
                ),
                (
                    lambda s: s["$defs"]["run"].update(additionalProperties={}),
                    "additionalProperties at run must be a boolean",
                ),
                (
                    lambda s: s["$defs"]["test"]["properties"]["suite"].update(
                        type="text"
                    ),
                    "unknown type 'text'",
                ),
                (
                    lambda s: s["$defs"]["test"]["properties"].update(
                        suite={"$ref": "#/definitions/x"}
                    ),
                    "$ref at test.suite must be #/$defs/<name>",
                ),
                (
                    lambda s: s["$defs"]["test"]["properties"].update(
                        suite={"$ref": "#/$defs/nope"}
                    ),
                    "names no definition",
                ),
                (
                    lambda s: s["$defs"]["test"]["properties"]["suite"].update(
                        pattern="("
                    ),
                    "bad pattern at test.suite",
                ),
                (lambda s: s.update({"x-schema-version": 2}), "is not version 1"),
                (lambda s: s.pop("$defs"), "has no $defs"),
            ):
                with self.subTest(expected=expected):
                    with self.assertRaises(lib.BenchError) as caught:
                        load(change)
                    self.assertIn(expected, str(caught.exception))
            path.write_text("{")
            with self.assertRaises(lib.BenchError):
                lib.load_schema(path)
            with self.assertRaises(lib.BenchError):
                lib.load_schema(pathlib.Path(tmp, "missing.json"))


class NdjsonTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = pathlib.Path(self.tmp.name, "records.ndjson")

    def read(self) -> list[dict[str, Any]]:
        records = []
        for _, record, error in lib.read_ndjson(self.path):
            self.assertEqual(error, "")
            assert record is not None
            records.append(record)
        return records

    def test_file_is_valid_after_every_write(self) -> None:
        writer = lib.NdjsonWriter(self.path)
        self.assertEqual(self.read(), [])
        written = []
        for kind in lib.FIELDS:
            record = valid_record(kind)
            writer.write(record)
            written.append(record)
            self.assertEqual(self.read(), written)
            self.assertTrue(self.path.read_bytes().endswith(b"}\n"))
        with mock.patch.object(lib.os, "fsync") as fsync:
            writer.write(written[0])
            fsync.assert_called_once()
        writer.close()
        writer.close()
        with self.assertRaises(lib.BenchError):
            writer.write(written[0])
        lines = self.path.read_text().splitlines()
        self.assertEqual(list(json.loads(lines[0])), list(lib.FIELDS["run"]))
        self.assertNotIn(" ", lines[3])

    def test_append_and_torn_last_line(self) -> None:
        with lib.NdjsonWriter(self.path) as writer:
            writer.write({"kind": "test", "n": 1})
        with open(self.path, "ab") as stream:
            stream.write(b'{"kind":"test","n":')
        with lib.NdjsonWriter(self.path) as writer:
            writer.write({"kind": "test", "n": 3})
        rows = list(lib.read_ndjson(self.path))
        self.assertEqual([number for number, _, _ in rows], [1, 2, 3])
        self.assertEqual(rows[0][1], {"kind": "test", "n": 1})
        self.assertIsNone(rows[1][1])
        self.assertTrue(rows[1][2].startswith("not JSON"))
        self.assertEqual(rows[2][1], {"kind": "test", "n": 3})

    def test_writer_refuses_what_is_not_json(self) -> None:
        with lib.NdjsonWriter(self.path) as writer:
            with self.assertRaises(ValueError):
                writer.write({"kind": "sample", "bw_gbps": float("nan")})
            writer.write({"kind": "sample", "text": "é"})
        self.assertEqual(
            self.path.read_bytes(), b'{"kind":"sample","text":"\\u00e9"}\n'
        )

    def test_reader_reports_bad_lines_and_goes_on(self) -> None:
        self.path.write_text(
            '{"a":1}\n\n   \nnot json\n[1,2]\n"text"\n{"v":NaN}\n{"v":Infinity}\n'
            '{"b":2}\r\n{"c":3}'
        )
        rows = list(lib.read_ndjson(self.path))
        self.assertEqual([number for number, _, _ in rows], [1, 4, 5, 6, 7, 8, 9, 10])
        self.assertEqual(
            [record for _, record, _ in rows],
            [{"a": 1}, None, None, None, None, None, {"b": 2}, {"c": 3}],
        )
        self.assertEqual(rows[2][2], "not a JSON object")
        self.assertIn("NaN", rows[4][2])
        # Deep enough to parse, and to exhaust whatever walks it recursively.
        for nest in (
            '{"x":' * 990 + "1" + "}" * 990,
            '{"x":' + "[" * 9 + "]" * 9 + "}",
        ):
            self.path.write_text(nest + "\n" + '{"x":[["flat"]]}\n')
            rows = list(lib.read_ndjson(self.path))
            self.assertEqual((rows[0][1], rows[0][2][:8]), (None, "not JSON"))
            self.assertEqual(rows[1][1], {"x": [["flat"]]})
        deep = "[" * 100000
        self.path.write_text(deep + "\n" + '{"ok":true}\n')
        rows = list(lib.read_ndjson(self.path))
        self.assertIsNone(rows[0][1])
        self.assertEqual(rows[1][1], {"ok": True})


if __name__ == "__main__":
    unittest.main()
