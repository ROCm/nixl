# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The committed bench matrix under .ci/bench: it loads, every case passes
the runner's rules, and it holds the cases the programme plan counts."""

from __future__ import annotations

import collections
import contextlib
import io
import pathlib
import re
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_lib as lib  # noqa: E402
import amd_bench_run as run  # noqa: E402

BENCH = pathlib.Path(__file__).resolve().parents[2] / "bench"
CONTAINER_ENV = BENCH / "envs" / "gha-container.toml"

# Plan section 2.6: cases per lane and profile, N-r counted with N.
PLAN_COUNTS = {
    "bench-container": {"S": 26, "N": 12},
    "vm1-local": {"S": 29, "N": 6},
    "vm2-net": {"S": 26, "N": 4, "B": 1},
    "vm1-loop": {"S": 16, "N": 2},
}
ACTIVE_LANES = ("bench-container", "vm1-local", "vm2-net")
PR_LANES = ("bench-container", "vm1-local")
# The LBA-STD and KV-STD sets of plan section 2.5.
LBA_STD = {"S": 21, "N": 4}
KV_STD = 11
RUNNER_PLACEHOLDERS = frozenset(
    ("asio_address", "asio_port", "benchmark_group", "case_dir")
)
ENV_VARS = frozenset(
    (
        "posix_dir",
        "ais_dir",
        "gusli_file",
        "gusli_blkdev",
        "redis_host",
        "redis_port",
        "s3_endpoint",
        "s3_bucket",
        "s3_access_key",
        "s3_secret_key",
        "etcd_endpoints",
    )
)
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")
HOST_VAR_RE = re.compile(r"(self|init|tgt)\.[A-Za-z0-9_]+")


def profile_family(name: str) -> str:
    return "N" if name == "N-r" else name


def run_cli(*argv: str) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        try:
            code = run.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue()


class MatrixTest(unittest.TestCase):
    matrix: lib.Matrix

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.matrix = lib.load_matrix(BENCH)
        except lib.MatrixError as exc:
            raise AssertionError("\n".join(exc.problems)) from exc

    def cases(self) -> list[lib.Case]:
        return [case for lane in self.matrix.lanes.values() for case in lane.cases]

    def flags(self, case: lib.Case) -> dict[str, lib.FlagValue]:
        return lib.effective_flags(case, self.matrix.profiles[case.profile])

    def number(self, case: lib.Case, name: str) -> int:
        value = self.flags(case)[name]
        assert isinstance(value, int) and not isinstance(value, bool), name
        return value

    def test_every_case_passes_the_runner_rules(self) -> None:
        problems = []
        for lane in self.matrix.lanes.values():
            for case in lane.cases:
                profile = self.matrix.profiles[case.profile]
                problems += [
                    f"{case.case_key}: {text}"
                    for text in run.check_case(case, profile, lane)
                ]
        self.assertEqual(problems, [])

    def test_validate_command_accepts_the_matrix(self) -> None:
        code, text = run_cli("validate", "--matrix", str(BENCH))
        self.assertEqual(code, 0, text)

    def test_nixlbench_would_not_adjust_or_refuse_a_case(self) -> None:
        # nixlbench's own rules, restated here so that the matrix is held to
        # them whatever the runner checks: utils.cpp L1044-1103 on main.
        for case in self.cases():
            with self.subTest(case=case.case_key):
                total = self.number(case, "total_buffer_size")
                largest = self.number(case, "max_block_size") * self.number(
                    case, "max_batch_size"
                )
                partition = case.threads * self.number(case, "large_blk_iter_ftr")
                self.assertLessEqual(largest, total // case.threads)
                self.assertEqual(total % case.threads, 0)
                self.assertEqual(self.number(case, "num_iter") % partition, 0)
                self.assertEqual(self.number(case, "warmup_iter") % partition, 0)
                self.assertIs(self.flags(case)["check_consistency"], True)

    def test_runner_caps(self) -> None:
        for lane in self.matrix.lanes.values():
            for case in lane.cases:
                with self.subTest(case=case.case_key):
                    if lane.env_class.startswith("gha-kvm"):
                        self.assertLessEqual(case.threads, 2)
                    if "VRAM" in (case.init_mem, case.tgt_mem):
                        total = self.number(case, "total_buffer_size")
                        self.assertLessEqual(total, 64 << 20)
                    if case.transport in ("rc", "ud", "verbs"):
                        self.assertLess(self.number(case, "max_block_size"), 16 << 20)

    def test_counts_match_the_plan(self) -> None:
        for name, want in PLAN_COUNTS.items():
            got = collections.Counter(
                profile_family(case.profile) for case in self.matrix.lanes[name].cases
            )
            self.assertEqual(dict(got), want, name)

    def test_storage_sets_are_the_same_on_every_attachment(self) -> None:
        def storage(lane: str) -> dict[str, str]:
            return {
                case.workload_key: lib.params_sha(
                    case, self.matrix.profiles[case.profile]
                )
                for case in self.matrix.lanes[lane].cases
                if case.backend in ("POSIX", "AIS", "GUSLI")
            }

        local = storage("vm1-local")
        families = collections.Counter(
            profile_family(key.rsplit("/", 1)[1]) for key in local
        )
        self.assertEqual(dict(families), LBA_STD)
        for name in ("vm1-fabric", "vm1-pcie"):
            self.assertEqual(storage(name), local, name)
            kv = [c for c in self.matrix.lanes[name].cases if c.backend == "NVME_KV"]
            self.assertEqual(len(kv), KV_STD, name)

    def test_blocked_if_and_only_if_blocked_by(self) -> None:
        for lane in self.matrix.lanes.values():
            self.assertEqual(lane.status == "blocked", bool(lane.blocked_by), lane.name)
            for case in lane.cases:
                with self.subTest(case=case.case_key):
                    self.assertEqual(case.status == "blocked", bool(case.blocked_by))
                    if lane.status == "blocked":
                        self.assertEqual(case.status, "blocked")

    def test_only_the_three_lanes_are_active(self) -> None:
        active = [
            lane.name for lane in self.matrix.lanes.values() if lane.status == "active"
        ]
        self.assertEqual(sorted(active), sorted(ACTIVE_LANES))
        for case in self.cases():
            if case.backend in ("LIBFABRIC", "NVME_KV"):
                self.assertEqual(case.status, "blocked", case.case_key)

    def test_pr_tier_is_the_s_cases_of_two_lanes(self) -> None:
        for lane in self.matrix.lanes.values():
            for case in lane.cases:
                with self.subTest(case=case.case_key):
                    self.assertIn(case.tier, ("pr", "nightly"))
                    want_pr = lane.name in PR_LANES and case.profile == "S"
                    self.assertEqual(case.tier == "pr", want_pr)

    def test_nothing_is_required_yet(self) -> None:
        self.assertEqual([c.case_key for c in self.cases() if c.required], [])

    def test_roles(self) -> None:
        for case in self.cases():
            with self.subTest(case=case.case_key):
                if case.profile != "S" and case.backend != "NVME_KV":
                    self.assertEqual(case.role, "track")
                if case.role == "gate":
                    self.assertEqual(case.profile, "S")
                    self.assertNotEqual(case.op, "QUERY")
                    self.assertNotIn(case.transport, ("rc", "ud", "verbs"))
                    if case.backend in lib.PAIR_BACKENDS:
                        self.assertEqual((case.init_mem, case.tgt_mem), ("DRAM",) * 2)

    def test_aa_case_is_a_nightly_write_sweep(self) -> None:
        for lane in self.matrix.lanes.values():
            with self.subTest(lane=lane.name):
                if lane.status == "blocked":
                    self.assertEqual(lane.aa_case, "")
                    continue
                (case,) = [c for c in lane.cases if c.case_key == lane.aa_case]
                self.assertIn(case.profile, ("N", "N-r"))
                self.assertEqual(case.op, "WRITE")
                self.assertEqual(case.status, "active")
                self.assertTrue(case.sentinel)

    def test_sentinels_are_few_and_tracked(self) -> None:
        for lane in self.matrix.lanes.values():
            sentinels = [case for case in lane.cases if case.sentinel]
            self.assertLessEqual(len(sentinels), 2, lane.name)
            for case in sentinels:
                self.assertEqual((case.role, case.status), ("track", "active"))

    def test_pairs_compare_like_with_like(self) -> None:
        for lane in self.matrix.lanes.values():
            by_key = {case.case_key: case for case in lane.cases}
            for pair in lane.pairs:
                num, den = by_key[pair.num], by_key[pair.den]
                with self.subTest(pair=pair.key):
                    self.assertEqual((num.op, num.backend), (den.op, den.backend))
                    self.assertEqual(
                        profile_family(num.profile), profile_family(den.profile)
                    )
                    if pair.kind == "vram-dram":
                        self.assertIn("VRAM", (num.init_mem, num.tgt_mem))
                        self.assertNotIn("VRAM", (den.init_mem, den.tgt_mem))
                        self.assertEqual(num.transport, den.transport)
                    elif pair.kind == "rc-tcp":
                        self.assertEqual((num.transport, den.transport), ("rc", "tcp"))
                        self.assertEqual(num.init_mem, den.init_mem)
                    elif pair.kind == "direct-buffered":
                        self.assertEqual((num.direct, den.direct), (True, False))
                        self.assertEqual(num.engine, den.engine)

    def test_read_never_shares_a_directory_with_another_operation(self) -> None:
        # nixlbench fills a file for READ only when it is too short, so a
        # file a WRITE case left fails the READ data check
        # (nixl_worker.cpp, initBasicDescFile).
        for lane in self.matrix.lanes.values():
            ops = collections.defaultdict(set)
            for case in lane.cases:
                if case.backend in ("POSIX", "AIS"):
                    ops[(case.backend, case.flags["filepath"])].add(case.op)
            for where, seen in ops.items():
                self.assertEqual(len(seen), 1, f"{lane.name} {where}: {sorted(seen)}")

    def test_gusli_reads_before_it_writes(self) -> None:
        # Only READ makes a type F file, and WRITE overwrites what a READ
        # of a type K device needs to find there.
        for lane in self.matrix.lanes.values():
            ops = [case.op for case in lane.cases if case.backend == "GUSLI"]
            if ops:
                self.assertEqual(ops, ["READ", "WRITE"], lane.name)
        for case in self.cases():
            if case.backend == "GUSLI":
                self.assertTrue(case.direct, case.case_key)

    def test_what_real_nixlbench_cannot_pass_is_fixed_or_parked(self) -> None:
        parked: collections.Counter[tuple[str, ...]] = collections.Counter()
        for case in self.cases():
            flags = self.flags(case)
            with self.subTest(case=case.case_key):
                if case.backend == "GUSLI":
                    # One device: a batch above 1 indexes past the device list.
                    batch = (flags["start_batch_size"], flags["max_batch_size"])
                    self.assertEqual(batch, (1, 1))
                    self.assertEqual(flags.get("pipeline_depth", 1), 1)
                if case.transport == "shm":
                    self.assertEqual(case.blocked_by, ("ucx-shm-ep",))
                    self.assertNotEqual(case.role, "gate")
                if (case.backend, case.op, case.status) == ("REDIS", "READ", "active"):
                    # One value of max_block_size is seeded per key.
                    self.assertEqual(flags["start_block_size"], flags["max_block_size"])
            if case.lane == "bench-container" and case.status == "blocked":
                parked[case.blocked_by] += 1
        self.assertEqual(
            dict(parked),
            {("E8",): 2, ("ucx-shm-ep",): 4, ("nixlbench-redis-read-sweep",): 1},
        )

    def test_crt_limit_is_the_smallest_block(self) -> None:
        crt = [case for case in self.cases() if case.engine == "crt"]
        self.assertTrue(crt)
        for case in crt:
            flags = self.flags(case)
            self.assertEqual(flags["obj_crt_min_limit"], flags["start_block_size"])

    def test_needs_name_what_an_environment_may_lack(self) -> None:
        for case in self.cases():
            want = set()
            if case.flags.get("posix_api_type") == "URING":
                want.add("io_uring")
            if case.backend == "REDIS":
                want.add("redis")
            if case.backend == "OBJ":
                want.add("s3")
            if case.backend == "LIBFABRIC":
                want.add("libfabric")
            if str(case.flags.get("device_list", "")).startswith("11:K:"):
                want.add("blkdev")
            self.assertLessEqual(want, set(case.needs), case.case_key)

    def test_placeholders_are_ones_the_runner_resolves(self) -> None:
        for case in self.cases():
            if case.status != "active":
                continue
            texts = [str(value) for value in self.flags(case).values()]
            texts += list(case.env.values())
            for name in PLACEHOLDER_RE.findall(" ".join(texts)):
                with self.subTest(case=case.case_key, placeholder=name):
                    self.assertTrue(
                        name in RUNNER_PLACEHOLDERS
                        or name in ENV_VARS
                        or HOST_VAR_RE.fullmatch(name)
                    )

    def test_no_secret_is_committed(self) -> None:
        for case in self.cases():
            for name in lib.SECRET_FLAGS:
                value = case.flags.get(name)
                if value is not None:
                    self.assertRegex(str(value), r"^\{[a-z0-9_]+\}$", case.case_key)

    def test_container_env_resolves_every_active_case(self) -> None:
        code, text = run_cli(
            "validate",
            "--matrix",
            str(BENCH),
            "--lane",
            "bench-container",
            "--env",
            str(CONTAINER_ENV),
        )
        self.assertEqual(code, 0, text)
        code, text = run_cli(
            "list",
            "--matrix",
            str(BENCH),
            "--lane",
            "bench-container",
            "--tier",
            "nightly",
            "--env",
            str(CONTAINER_ENV),
            "--commands",
        )
        self.assertEqual(code, 0, text)
        self.assertNotRegex(text, r"\{[a-z_.]+\}")
        lane = self.matrix.lanes["bench-container"]
        for case in lane.cases:
            if case.status == "active":
                self.assertIn(case.case_key, text)


if __name__ == "__main__":
    unittest.main()
