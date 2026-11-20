# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The committed bench matrix under .ci/bench: it loads, every case passes
the runner's rules, it holds the cases the programme plan counts, and each
active lane has an env template that resolves its cases."""

from __future__ import annotations

import collections
import contextlib
import io
import pathlib
import re
import sys
import tomllib
import unittest
from typing import Any, Iterator

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_lib as lib  # noqa: E402
import amd_bench_run as run  # noqa: E402

BENCH = pathlib.Path(__file__).resolve().parents[2] / "bench"
ENVS = BENCH / "envs"

# Plan section 2.6: cases per lane and profile, N-r counted with N and the
# Key Value profiles together.
PLAN_COUNTS = {
    "bench-container": {"S": 26, "N": 12},
    "vm1-local": {"S": 29, "N": 6},
    "vm2-net": {"S": 26, "N": 4, "B": 1},
    "vm1-loop": {"S": 16, "N": 2},
    "vm1-fabric": {"S": 21, "N": 4, "KV": 11},
}
ACTIVE_LANES = ("bench-container", "vm1-local", "vm2-net", "vm1-loop", "vm1-fabric")
PR_LANES = ("bench-container", "vm1-local")
# What a lane job sets in its env template, and never a value of its own.
JOB = "@job"
# Per env class: the most its template may claim to provide, and the keys
# whose value only the lane job knows.
TEMPLATES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "gha-container": (frozenset(("io_uring", "redis")), frozenset()),
    "gha-kvm-vm1": (
        frozenset(("blkdev", "gpu", "io_uring")),
        frozenset(("hosts.guest.ssh_config", "vars.gusli_blkdev")),
    ),
    "gha-kvm-vm2": (
        frozenset(("gpu", "rdma", "two_hosts")),
        frozenset(
            (
                "hosts.guest1.ssh_config",
                "hosts.guest1.vars.ucx_net_devices",
                "hosts.guest2.ssh_config",
                "hosts.guest2.vars.ucx_net_devices",
            )
        ),
    ),
    "gha-kvm-vm1-loopback": (
        frozenset(("gpu", "rdma")),
        frozenset(("hosts.guest.ssh_config",)),
    ),
    "gha-kvm-vm1-nvmeof": (
        frozenset(("blkdev", "gpu", "io_uring")),
        frozenset(("hosts.guest.ssh_config", "vars.gusli_blkdev")),
    ),
}
# Needs of active cases that no job sets up yet: those cases end as skip/env.
NEVER_PROVIDED = {"bench-container": {"s3"}}
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
    if name.startswith("KV-"):
        return "KV"
    return "N" if name == "N-r" else name


def strings(value: Any, path: str = "") -> Iterator[tuple[str, str]]:
    """(dotted path, text) of every string in a parsed TOML document."""
    if isinstance(value, str):
        yield path, value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from strings(item, f"{path}.{key}" if path else key)
    elif isinstance(value, list):
        for number, item in enumerate(value):
            yield from strings(item, f"{path}[{number}]")


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

    def test_the_active_storage_lanes_declare_the_same_storage_pairs(self) -> None:
        # A ratio exists only where the matrix declares the pair, so a lane
        # that runs the direct and buffered cases must also pair them.
        def storage_pairs(name: str) -> set[tuple[str, str, str]]:
            lane = self.matrix.lanes[name]
            workload = {case.case_key: case.workload_key for case in lane.cases}
            return {
                (pair.kind, workload[pair.num], workload[pair.den])
                for pair in lane.pairs
                if pair.kind == "direct-buffered"
            }

        local = storage_pairs("vm1-local")
        self.assertEqual(len(local), 6)
        self.assertEqual(storage_pairs("vm1-fabric"), local)

    def test_blocked_if_and_only_if_blocked_by(self) -> None:
        for lane in self.matrix.lanes.values():
            self.assertEqual(lane.status == "blocked", bool(lane.blocked_by), lane.name)
            for case in lane.cases:
                with self.subTest(case=case.case_key):
                    self.assertEqual(case.status == "blocked", bool(case.blocked_by))
                    if lane.status == "blocked":
                        self.assertEqual(case.status, "blocked")

    def test_only_the_five_lanes_are_active(self) -> None:
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

    def test_rdma_in_one_guest_names_its_device_itself(self) -> None:
        # The runner sets UCX_NET_DEVICES only between two hosts.
        for lane in self.matrix.lanes.values():
            for case in lane.cases:
                own = case.env.get("UCX_NET_DEVICES")
                rdma = case.backend == "UCX" and case.transport in ("rc", "ud")
                with self.subTest(case=case.case_key):
                    if rdma and lane.topology == "single":
                        self.assertEqual(own, "{self.ucx_net_devices}")
                    else:
                        self.assertIsNone(own)


class EnvTemplateTest(unittest.TestCase):
    """The env templates under .ci/bench/envs, one per env class of an active
    lane, as the lane jobs render them."""

    matrix: lib.Matrix
    envs: dict[str, run.Env]

    @classmethod
    def setUpClass(cls) -> None:
        cls.matrix = lib.load_matrix(BENCH)
        try:
            cls.envs = {
                path.stem: run.load_env(path) for path in sorted(ENVS.glob("*.toml"))
            }
        except run.EnvError as exc:
            raise AssertionError("\n".join(exc.problems)) from exc

    def active_lanes(self) -> list[lib.Lane]:
        return [lane for lane in self.matrix.lanes.values() if lane.status == "active"]

    def plans(self, lane: lib.Lane) -> Iterator[tuple[lib.Case, run.Plan]]:
        env = self.envs[lane.env_class]
        homes = run.display_homes(env)
        for case in lane.cases:
            if case.status == "active":
                profile = self.matrix.profiles[case.profile]
                yield case, run.plan_case(case, profile, env, homes, "0" * 20, 0)

    def test_there_is_one_template_per_active_env_class(self) -> None:
        wanted = {lane.env_class for lane in self.active_lanes()}
        self.assertEqual(set(self.envs), wanted)
        self.assertEqual(set(TEMPLATES), wanted)
        for name, env in self.envs.items():
            self.assertEqual(env.env_class, name)

    def test_template_resolves_every_active_case_of_its_lanes(self) -> None:
        for name in self.envs:
            code, text = run_cli(
                "validate", "--matrix", str(BENCH), "--env", str(ENVS / f"{name}.toml")
            )
            self.assertEqual(code, 0, text)
        for lane in self.active_lanes():
            with self.subTest(lane=lane.name):
                where = ["--matrix", str(BENCH), "--lane", lane.name]
                where += ["--env", str(ENVS / f"{lane.env_class}.toml")]
                code, text = run_cli("validate", *where)
                self.assertEqual(code, 0, text)
                code, text = run_cli("list", *where, "--tier", "nightly", "--commands")
                self.assertEqual(code, 0, text)
                self.assertNotRegex(text, r"\{[a-z_.]+\}")
                self.assertNotIn("     # ", text)
                for case in lane.cases:
                    if case.status == "active":
                        self.assertIn(case.case_key, text)

    def test_job_values_are_the_ones_only_the_job_knows(self) -> None:
        for name, (_, job_keys) in TEMPLATES.items():
            data = tomllib.loads((ENVS / f"{name}.toml").read_text(encoding="utf-8"))
            found = {path for path, text in strings(data) if text == JOB}
            self.assertEqual(found, set(job_keys), name)
            for path, text in strings(data):
                self.assertTrue(text == JOB or JOB not in text, f"{name}: {path}")

    def test_provides_is_the_most_the_job_can_set_up(self) -> None:
        for name, (provides, _) in TEMPLATES.items():
            self.assertEqual(list(self.envs[name].provides), sorted(provides), name)

    def test_only_the_s3_cases_lack_what_they_need(self) -> None:
        for lane in self.active_lanes():
            needed = {
                need
                for case in lane.cases
                if case.status == "active"
                for need in case.needs
            }
            lacking = needed - set(self.envs[lane.env_class].provides)
            self.assertEqual(lacking, NEVER_PROVIDED.get(lane.name, set()), lane.name)

    def test_roles_and_hosts(self) -> None:
        for name, env in self.envs.items():
            with self.subTest(env=name):
                self.assertEqual(set(env.roles), {"init", "tgt"})
                self.assertEqual(set(env.roles.values()), set(env.hosts))
                two = env.roles["init"] != env.roles["tgt"]
                self.assertEqual(two, "two_hosts" in env.provides)
                self.assertEqual(env.asio_port_base, 21000)
                self.assertEqual(env.tuning, run.Tuning())
                for host, spec in env.hosts.items():
                    if spec.kind == "local":
                        self.assertTrue(spec.work_dir.startswith("/"))
                        self.assertTrue(spec.nixlbench.startswith("/"))
                        continue
                    self.assertEqual(spec.ssh_host, host)
                    self.assertEqual(spec.ssh_config, JOB)
                    # Under the guest's home directory, where the job
                    # installs the wheels.
                    self.assertEqual(spec.work_dir, "amd-bench/run")
                    self.assertEqual(spec.nixlbench, "amd-bench/venv/bin/nixlbench")
                    self.assertEqual(spec.setup, ())
                    if two:
                        # The initiator's address is where the target connects.
                        self.assertIn("ip", spec.vars)

    def test_probes(self) -> None:
        for name, env in self.envs.items():
            with self.subTest(env=name):
                names = [probe.name for probe in env.probes]
                self.assertEqual(len(names), len(set(names)))
                hosts = set(env.roles.values())
                for probe in env.probes:
                    self.assertLessEqual(set(probe.classes), set(env.provides))
                    if not probe.classes:
                        self.assertEqual(probe.when, "start", probe.name)
                # A device that can wedge is looked at after each case that
                # used it, in every guest that has one.
                for device in ("gpu", "rdma"):
                    probed = {
                        env.roles.get(probe.host, probe.host)
                        for probe in env.probes
                        if probe.when == "between" and device in probe.classes
                    }
                    want = hosts if device in env.provides else set()
                    self.assertEqual(probed, want, device)
        both = {"guest1", "guest2"}
        self.assertEqual(set(self.envs["gha-kvm-vm2"].roles.values()), both)
        kv = [p for p in self.envs["gha-kvm-vm1-nvmeof"].probes if not p.classes]
        self.assertEqual([p.cmd for p in kv], ["test -c /run/amd-bench/kv"])

    def test_ucx_device_lists(self) -> None:
        seen: dict[str, set[str | None]] = collections.defaultdict(set)
        for lane in self.active_lanes():
            for case, plan in self.plans(lane):
                if case.backend == "UCX":
                    for proc in plan.procs:
                        seen[lane.name].add(proc.env.get("UCX_NET_DEVICES"))
        self.assertEqual(
            dict(seen),
            {
                "bench-container": {None},
                "vm1-local": {None},
                # Each guest's own list, which only the job knows.
                "vm2-net": {JOB},
                "vm1-loop": {"ernic0:1"},
            },
        )

    def test_storage_paths(self) -> None:
        for name in ("vm1-local", "vm1-fabric"):
            paths = set()
            for case, plan in self.plans(self.matrix.lanes[name]):
                if case.backend in ("POSIX", "AIS"):
                    paths.add(str(plan.flags["filepath"]).rsplit("/", 1)[0])
                elif case.backend == "GUSLI":
                    self.assertEqual(plan.flags["device_list"], f"11:K:{JOB}")
            fs = "/mnt/amd-bench/fs"
            self.assertEqual(paths, {f"{fs}/posix", f"{fs}/ais"}, name)


if __name__ == "__main__":
    unittest.main()
