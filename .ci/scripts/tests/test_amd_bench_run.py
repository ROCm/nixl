# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of amd_bench_run.py.

    python3 -m unittest discover -s .ci/scripts/tests -v

The parser fixtures posix_write.stdout, posix_read.stdout and
ucx_asio_init.stdout under fixtures/nixlbench are real nixlbench output (a
wheel built from upstream main, hostname replaced, trailing blanks removed);
the others say in their first line that they are synthetic. The end-to-end
tests drive fixtures/fake_nixlbench.py on this machine: no network beyond
127.0.0.1, no docker, no ssh.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import random
import shutil
import signal
import socket
import sys
import tempfile
import threading
import time
import unittest
from typing import Any
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_lib as lib  # noqa: E402
import amd_bench_run as run  # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
OUTPUT = FIXTURES / "nixlbench"
FAKE = FIXTURES / "fake_nixlbench.py"

COMMON = {"check_consistency": True, "warmup_iter": 16, "large_blk_iter_ftr": 4}
PROFILE_FLAGS: dict[str, dict[str, Any]] = {
    "S": {
        **COMMON,
        "num_iter": 64,
        "total_buffer_size": 33554432,
        "start_block_size": 65536,
        "max_block_size": 65536,
        "start_batch_size": 4,
        "max_batch_size": 4,
    },
    "N": {
        **COMMON,
        "num_iter": 128,
        "total_buffer_size": 268435456,
        "start_block_size": 4096,
        "max_block_size": 4194304,
        "start_batch_size": 4,
        "max_batch_size": 4,
    },
    "B": {
        **COMMON,
        "num_iter": 128,
        "total_buffer_size": 67108864,
        "start_block_size": 65536,
        "max_block_size": 65536,
        "start_batch_size": 1,
        "max_batch_size": 32,
    },
}
PROFILE_FLAGS["N-r"] = {**PROFILE_FLAGS["N"], "max_block_size": 2097152}
WHEN_VRAM: dict[str, dict[str, Any]] = {
    "N": {"total_buffer_size": 67108864},
    "N-r": {"total_buffer_size": 67108864},
}
PROFILES = {
    name: lib.Profile(name, flags, WHEN_VRAM.get(name, {}), len(lib.sweep(flags)))
    for name, flags in PROFILE_FLAGS.items()
}
LANE = lib.Lane("t", "T", "test", "container", "single", "active", (), "", "", (), ())
VM_LANE = lib.Lane(
    "t", "T", "gha-kvm-vm2", "emulated", "two-host", "active", (), "", "", (), ()
)


def make_case(**changes: Any) -> lib.Case:
    fields: dict[str, Any] = dict(
        lane="t",
        index=0,
        case_key="t/x",
        workload_key="x",
        profile="S",
        backend="POSIX",
        engine="",
        transport="none",
        init_mem="DRAM",
        tgt_mem="FILE",
        op="WRITE",
        scheme="pairwise",
        runtime="NONE",
        threads=1,
        direct=False,
        flags={},
        env={},
        role="info",
        required=False,
        tier="pr",
        sentinel=False,
        status="active",
        blocked_by=(),
        attach="none",
        needs=(),
        timeout_s=20,
        pair_key="",
        pair_role="",
    )
    fields.update(changes)
    return lib.Case(**fields)


def ucx_case(**changes: Any) -> lib.Case:
    fields: dict[str, Any] = dict(
        backend="UCX", runtime="ASIO", tgt_mem="DRAM", transport="tcp"
    )
    fields.update(changes)
    return make_case(**fields)


def toml_value(value: Any) -> str:
    return json.dumps(value)


def profile_toml(name: str) -> str:
    lines = [
        f"{key} = {toml_value(value)}" for key, value in PROFILE_FLAGS[name].items()
    ]
    lines += ["[amd]", f'name = "{name}"', f"rows = {PROFILES[name].rows}"]
    if name in WHEN_VRAM:
        lines.append("[amd.when_vram]")
        lines += [f"{key} = {value}" for key, value in WHEN_VRAM[name].items()]
    return "\n".join(lines) + "\n"


def lane_toml(name: str, body: str, **header: Any) -> str:
    fields = dict(
        schema=1,
        lane=name,
        lane_id="T",
        env_class="test",
        timing_trust="container",
        topology="single",
        status="active",
        aa_case="",
        description="runner tests",
    )
    fields.update(header)
    head = "\n".join(f"{key} = {toml_value(value)}" for key, value in fields.items())
    return f"{head}\n\n{body}"


PORT_SPAN = 64


def free_port() -> int:
    """Base of PORT_SPAN free ports that no connection of this machine takes."""
    low, high = 32768, 60999
    try:
        ranges = pathlib.Path("/proc/sys/net/ipv4/ip_local_port_range").read_text()
        low, high = (int(value) for value in ranges.split())
    except (OSError, ValueError):
        pass
    # Outside the ephemeral range, or a connection could hold a port as its
    # source port by the time a case binds it.
    below = low - PORT_SPAN > 12000
    first, last = (10000, low - PORT_SPAN) if below else (high + 1, 65000)
    for _ in range(50):
        base = random.randrange(first, last - PORT_SPAN)
        held = []
        try:
            for port in range(base, base + PORT_SPAN):
                held.append(socket.socket())
                held[-1].bind(("127.0.0.1", port))
            return base
        except OSError:
            continue
        finally:
            for sock in held:
                sock.close()
    raise AssertionError("no free port range on 127.0.0.1")


STORAGE = """
[[case]]
key = "{lane}/posix/{{mode}}"
workload = "posix/{{mode}}"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
flags = {{ filepath = "{{posix_dir}}", posix_api_type = "AIO" }}
env = {{ FAKE_NIXLBENCH_MODE = "{{mode}}" }}
[case.axes]
mode = {modes}
"""
DEFAULTS = '[defaults]\ntier = "pr"\nprofile = "S"\ntimeout_s = 20\n'


def storage(lane: str, *modes: str) -> str:
    return STORAGE.format(lane=lane, modes=json.dumps(list(modes)))


class Bench:
    """A matrix, env files and output directories in one temporary directory."""

    def __init__(self, test: Any, *, for_class: bool = False) -> None:
        self.root = pathlib.Path(tempfile.mkdtemp(prefix="amd-bench-test-"))
        cleanup = test.addClassCleanup if for_class else test.addCleanup
        cleanup(shutil.rmtree, self.root, True)
        self.matrix = self.root / "bench"
        (self.matrix / "profiles").mkdir(parents=True)
        (self.matrix / "lanes").mkdir()
        for name in PROFILES:
            (self.matrix / "profiles" / f"{name}.toml").write_text(profile_toml(name))
        self.runs = 0

    def lane(self, name: str, body: str, **header: Any) -> None:
        text = lane_toml(name, DEFAULTS + body, **header)
        (self.matrix / "lanes" / f"{name}.toml").write_text(text)

    def env(
        self,
        name: str = "env",
        *,
        provides: tuple[str, ...] = (),
        tuning: str = "",
        extra: str = "",
        host: str = "",
    ) -> pathlib.Path:
        text = f"""schema = 1
env_class = "test"
provides = {json.dumps(list(provides))}
[roles]
init = "here"
tgt = "here"
[hosts.here]
kind = "local"
work_dir = "{self.root}/work"
nixlbench = "{FAKE}"
{host}
[vars]
asio_port_base = {free_port()}
posix_dir = "{self.root}/posix"
s3_access_key = "AKIDsecretA1"
s3_secret_key = "hunter2-SECRET"
[tuning]
kill_after_s = 2
pair_skew_s = 0.05
listen_wait_s = 5
peer_grace_s = 1
{tuning or "calibrate = false"}
{extra}
"""
        path = self.root / f"{name}.toml"
        path.write_text(text)
        return path

    def command(self, *args: str) -> tuple[int, str]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stdout):
            code = run.main(list(args))
        return code, stdout.getvalue()

    def run(
        self, lane: str, *args: str, env: pathlib.Path | None = None, tier: str = "pr"
    ) -> Result:
        self.runs += 1
        out = self.root / f"out{self.runs}"
        code, text = self.command(
            "run",
            "--matrix",
            str(self.matrix),
            "--lane",
            lane,
            "--tier",
            tier,
            "--env",
            str(env or self.env()),
            "--out",
            str(out),
            *args,
        )
        return Result(code, text, out)


class Result:
    def __init__(self, code: int, text: str, out: pathlib.Path) -> None:
        self.code = code
        self.text = text
        self.out = out
        self.records: list[dict[str, Any]] = []
        path = out / "records.ndjson"
        if path.is_file():
            for _, record, error in lib.read_ndjson(path):
                assert record is not None, error
                self.records.append(record)

    def kind(self, kind: str) -> list[dict[str, Any]]:
        return [record for record in self.records if record["kind"] == kind]

    def case(self, key: str, variant: str = "head", round_: int = 0) -> dict[str, Any]:
        for record in self.kind("case"):
            found = (record["case_key"], record["variant"], record["round"])
            if found == (key, variant, round_):
                return record
        raise AssertionError(f"no case record for {key}\n{self.text}")

    def of_case(self, kind: str, case: dict[str, Any]) -> list[dict[str, Any]]:
        return [r for r in self.kind(kind) if r["case_uid"] == case["case_uid"]]

    def log(self, case: dict[str, Any], name: str) -> str:
        return (self.out / case["log_ref"] / name).read_text()


class ParserTest(unittest.TestCase):
    def table(self, name: str) -> run.Table:
        return run.parse_stdout((OUTPUT / name).read_text())

    def test_real_posix_write(self) -> None:
        table = self.table("posix_write.stdout")
        self.assertEqual(table.columns, run.KEYS_10)
        self.assertEqual(
            table.rows,
            (
                {
                    "block_size": 16384,
                    "batch_size": 4,
                    "bw_gbps": 4.429712,
                    "lat_avg_us": 3.7,
                    "prep_avg_us": 5.0,
                    "prep_p99_us": 5.0,
                    "post_avg_us": 14.1,
                    "post_p99_us": 31.0,
                    "tx_avg_us": 0.6,
                    "tx_p99_us": 2.0,
                },
            ),
        )
        self.assertEqual(table.noise_lines, 1)
        self.assertIsInstance(table.rows[0]["block_size"], int)

    def test_real_posix_read(self) -> None:
        table = self.table("posix_read.stdout")
        values = [table.rows[0][key] for key in run.KEYS_10]
        self.assertEqual(
            values, [16384, 4, 11.954450, 1.4, 1.0, 1.0, 4.8, 20.0, 0.6, 2.0]
        )

    def test_real_ucx_initiator(self) -> None:
        table = self.table("ucx_asio_init.stdout")
        values = [table.rows[0][key] for key in run.KEYS_10]
        self.assertEqual(
            values, [16384, 4, 3.030566, 5.4, 3.0, 3.0, 4.4, 7.0, 17.2, 43.0]
        )
        self.assertEqual(table.config["asio_address"], "127.0.0.1")
        self.assertEqual(table.config["asio_port"], "12345")
        self.assertEqual(table.config["runtime_type"], "ASIO")
        self.assertNotIn("posix_api_type", table.config)

    def test_config_echo(self) -> None:
        config = self.table("posix_write.stdout").config
        expected = {
            "backend": "POSIX",
            "op_type": "WRITE",
            "initiator_seg_type": "DRAM",
            "target_seg_type": "DRAM",
            "scheme": "pairwise",
            "check_consistency": "1",
            "total_buffer_size": "80000000",
            "start_block_size": "16384",
            "num_iter": "112",
            "warmup_iter": "16",
            "large_blk_iter_ftr": "16",
            "num_threads": "1",
            "posix_api_type": "AIO",
            "storage_enable_direct": "0",
            "num_files": "1",
            "filenames": "",
            "randomize_location_mode": "none",
            "prepared_xfer": "0",
        }
        self.assertEqual({key: config.get(key) for key in expected}, expected)

    def test_config_echo_keeps_trailing_blank_form(self) -> None:
        line = "filenames (--filenames=filename1,filename2,...)             : \n"
        self.assertEqual(run.parse_stdout(line).config, {"filenames": ""})

    def test_twelve_columns(self) -> None:
        table = self.table("twelve_col.stdout")
        self.assertEqual(table.columns, run.KEYS_12)
        self.assertEqual(len(table.rows), 2)
        self.assertEqual(table.rows[0]["agg_bw_gbps"], 1.024)
        self.assertEqual(table.rows[0]["net_util_pct"], 0.4096)
        self.assertEqual(table.rows[1]["block_size"], 8192)
        self.assertEqual(table.rows[1]["tx_p99_us"], 6.0)

    def test_unknown_header_fails(self) -> None:
        with self.assertRaisesRegex(run.TableError, "unknown table header"):
            self.table("bad_header.stdout")

    def test_short_row_fails(self) -> None:
        with self.assertRaisesRegex(run.TableError, "malformed table row"):
            self.table("short_row.stdout")

    def test_bad_cell_fails(self) -> None:
        text = (
            (OUTPUT / "posix_write.stdout").read_text().replace("4.429712", "4,429712")
        )
        with self.assertRaises(run.TableError):
            run.parse_stdout(text)

    def test_no_header_is_an_empty_table(self) -> None:
        text = (OUTPUT / "posix_write.stdout").read_text().split("Block Size")[0]
        table = run.parse_stdout(text)
        self.assertEqual((table.columns, table.rows), ((), ()))
        self.assertEqual(table.config["backend"], "POSIX")
        self.assertEqual(run.parse_stdout(""), run.Table((), (), {}, 0))

    def test_inf_nan_and_noise(self) -> None:
        text = (OUTPUT / "posix_write.stdout").read_text()
        text = text.replace("4.429712", "inf     ").replace(" 3.7 ", " -nan ")
        text += "Ctrl-C received, exiting...\nGetting Redis key: x\n\n"
        table = run.parse_stdout(text)
        self.assertIsNone(table.rows[0]["bw_gbps"])
        self.assertIsNone(table.rows[0]["lat_avg_us"])
        self.assertEqual(table.rows[0]["prep_avg_us"], 5.0)
        self.assertEqual(table.noise_lines, 3)
        # Too long for a double: not a number JSON can carry.
        huge = (
            (OUTPUT / "posix_write.stdout").read_text().replace("4.429712", "9" * 400)
        )
        self.assertIsNone(run.parse_stdout(huge).rows[0]["bw_gbps"])

    def test_ucx_log(self) -> None:
        rc = (OUTPUT / "ucx_proto_rc.ucx.log").read_text()
        self.assertEqual(run.parse_ucx_log(rc), "rc+rocm_copy")
        tcp = (OUTPUT / "ucx_proto_tcp.ucx.log").read_text()
        self.assertEqual(run.parse_ucx_log(tcp), "tcp")
        self.assertIsNone(run.parse_ucx_log("UCX  WARN  nothing to see\n"))
        self.assertIsNone(run.parse_ucx_log(""))
        self.assertEqual(len(run.UCX_COMPLAINT_RE.findall(rc)), 1)


class CheckCaseTest(unittest.TestCase):
    def problems(self, case: lib.Case, lane: lib.Lane = LANE) -> str:
        return "\n".join(run.check_case(case, PROFILES[case.profile], lane))

    def refused(self, text: str, lane: lib.Lane = LANE, **changes: Any) -> None:
        maker = ucx_case if changes.pop("ucx", False) else make_case
        self.assertIn(text, self.problems(maker(**changes), lane))

    def test_profiles_pass_with_one_and_two_threads(self) -> None:
        for profile in PROFILES:
            for threads in (1, 2):
                for case in (
                    make_case(profile=profile, threads=threads),
                    ucx_case(profile=profile, threads=threads),
                    ucx_case(
                        profile=profile,
                        threads=threads,
                        init_mem="VRAM",
                        tgt_mem="VRAM",
                        transport="rc",
                    ),
                ):
                    for lane in (LANE, VM_LANE):
                        with self.subTest(profile=profile, threads=threads, case=case):
                            self.assertEqual(self.problems(case, lane), "")

    def test_rule_1_buffer(self) -> None:
        bare = lib.Profile("S", {"num_iter": 64, "warmup_iter": 16}, {}, None)
        found = run.check_case(make_case(), bare, LANE)
        self.assertIn("total_buffer_size is not set", "\n".join(found))
        self.refused(
            "total_buffer_size / threads",
            threads=2,
            flags={"total_buffer_size": 262144},
        )
        self.refused(
            "total_buffer_size / num_initiator_dev",
            flags={"total_buffer_size": 524288, "num_initiator_dev": 4},
        )
        self.refused(
            "total_buffer_size / num_target_dev",
            flags={"total_buffer_size": 524288, "num_target_dev": 4},
        )
        self.refused("not divisible by num_initiator_dev x threads", threads=3)
        self.refused(
            "not divisible by num_target_dev x threads", flags={"num_target_dev": 3}
        )

    def test_rule_2_iterations(self) -> None:
        self.refused("num_iter must be at least 1", flags={"num_iter": 0})
        self.refused(
            "large_blk_iter_ftr must be greater", flags={"large_blk_iter_ftr": 0}
        )
        self.refused("num_iter (70) is not a multiple", flags={"num_iter": 70})
        self.refused("warmup_iter (10) is not a multiple", flags={"warmup_iter": 10})
        self.refused(
            "warmup_iter (16) is not a multiple", threads=8, flags={"num_iter": 96}
        )

    def test_rule_3_sweep_bounds(self) -> None:
        self.refused("start_block_size exceeds", flags={"start_block_size": 131072})
        self.refused("start_batch_size exceeds", flags={"start_batch_size": 8})
        self.refused("must be positive", flags={"start_batch_size": 0})

    def test_rule_4_pipeline_and_query(self) -> None:
        self.refused("pipeline_depth must be at least 1", flags={"pipeline_depth": 0})
        self.refused(
            "QUERY requires pipeline_depth 1", op="QUERY", flags={"pipeline_depth": 2}
        )
        self.refused("QUERY is for storage backends", ucx=True, op="QUERY")
        self.assertEqual(self.problems(make_case(op="QUERY")), "")

    def test_rule_5_enumerated_strings(self) -> None:
        self.refused("posix_api_type must be", flags={"posix_api_type": "LIBAIO"})
        obj: dict[str, Any] = dict(backend="OBJ", tgt_mem="OBJ")
        self.refused("obj_scheme must be", flags={"obj_scheme": "ftp"}, **obj)
        self.refused("obj_req_checksum must be", flags={"obj_req_checksum": "x"}, **obj)

    def test_rule_6_randomize(self) -> None:
        mode = "randomize_location_mode"
        self.refused(f"{mode} must be", flags={mode: "random"})
        self.refused("storage backends only", ucx=True, flags={mode: "blockaligned"})
        self.refused("excludes direct", direct=True, flags={mode: "bytealigned"})
        self.refused("excludes check_consistency", flags={mode: "bytealigned"})
        loose = {mode: "bytealigned", "check_consistency": False}
        self.assertEqual(self.problems(make_case(flags=loose)), "")
        self.assertEqual(self.problems(make_case(flags={mode: "blockaligned"})), "")

    def test_rule_7_prepared_and_reregister(self) -> None:
        self.refused(
            "prepared_xfer is incompatible",
            flags={"prepared_xfer": True, "reregister_mem": True},
        )

    def test_rule_8_num_files(self) -> None:
        self.refused("num_files (2) must divide", flags={"num_files": 2})
        self.refused("num_files (3) must divide", threads=4, flags={"num_files": 3})
        self.assertEqual(
            self.problems(make_case(threads=2, flags={"num_files": 2})), ""
        )
        redis = make_case(backend="REDIS", tgt_mem="KV", flags={"num_files": 2})
        self.assertEqual(self.problems(redis), "")

    def test_rule_9_asio_devices(self) -> None:
        self.refused("exactly one initiator", ucx=True, flags={"num_target_dev": 2})

    def test_rule_10_storage_memory(self) -> None:
        self.refused("POSIX needs init_mem DRAM", init_mem="VRAM")
        self.refused("AIS needs init_mem VRAM", backend="AIS")
        self.assertEqual(self.problems(make_case(backend="AIS", init_mem="VRAM")), "")

    def test_rule_11_banned_tokens(self) -> None:
        for word in run.DISABLED_TOKENS:
            self.refused("which nixlbench bans", flags={"filepath": f"/data/{word}/x"})
        self.refused(
            "flag filepath contains 'flagfile'", flags={"filepath": "/flagfile"}
        )

    def test_rule_12_runner_caps(self) -> None:
        self.refused("at most 2 threads", VM_LANE, threads=4)
        self.assertEqual(self.problems(make_case(threads=4)), "")
        big = {"total_buffer_size": 134217728}
        self.refused("at most 67108864", ucx=True, init_mem="VRAM", flags=big)
        self.refused("at most 67108864", ucx=True, tgt_mem="VRAM", flags=big)
        wide = {"max_block_size": 16777216, "total_buffer_size": 67108864}
        for transport in ("rc", "ud"):
            self.refused("below 16777216", ucx=True, transport=transport, flags=wide)
        self.assertEqual(self.problems(ucx_case(flags=wide)), "")
        self.refused("scheme tp is not runnable", ucx=True, scheme="tp")
        self.refused("runtime ETCD is not runnable", ucx=True, runtime="ETCD")
        parked = ucx_case(
            scheme="tp", runtime="ETCD", status="blocked", blocked_by=("W8a",)
        )
        self.assertEqual(self.problems(parked), "")

    def test_rule_13_what_nixlbench_cannot_run(self) -> None:
        gusli: dict[str, Any] = dict(backend="GUSLI", tgt_mem="BLK", direct=True)
        one = {"start_batch_size": 1, "max_batch_size": 1}
        self.refused("GUSLI needs max_batch_size 1", **gusli)
        self.refused(
            "GUSLI needs max_batch_size 1", flags={**one, "pipeline_depth": 2}, **gusli
        )
        self.assertEqual(self.problems(make_case(flags=one, **gusli)), "")
        redis: dict[str, Any] = dict(backend="REDIS", tgt_mem="OBJ")
        self.refused("a REDIS READ needs", profile="N", op="READ", **redis)
        self.assertEqual(self.problems(make_case(op="READ", **redis)), "")
        self.assertEqual(self.problems(make_case(profile="N", **redis)), "")
        parked = make_case(
            profile="N", op="READ", status="blocked", blocked_by=("x",), **redis
        )
        self.assertEqual(self.problems(parked), "")
        odd = {"use_hugepages": True, "total_buffer_size": 3145728}
        self.refused("use_hugepages needs total_buffer_size", flags=odd)
        self.assertEqual(self.problems(make_case(flags={"use_hugepages": True})), "")

    def test_a_placeholder_is_left_for_run_time(self) -> None:
        self.assertEqual(self.problems(make_case(flags={"num_files": "{files}"})), "")


EXAMPLE_1 = """
[defaults]
tier = "pr"
timeout_s = 120
tgt_mem = "FILE"

[[case]]
key = "bench-container/posix/{api}/{io}/{op}/S"
workload = "posix/{api}/{io}/{op}/S"
profile = "S"
backend = "POSIX"
engine = "{api}"
flags = { posix_api_type = "{api}", filepath = "{posix_dir}" }
[case.axes]
api = ["AIO", "URING", "POSIXAIO"]
io = ["buffered", "direct"]
op = ["WRITE", "READ"]
[[case.when]]
match = { io = "direct" }
set = { direct = true }
[[case.when]]
match = { api = "URING" }
set = { needs = ["io_uring"] }
"""
EXAMPLE_1_ENV = """
schema = 1
env_class = "gha-container"
provides = []
[roles]
init = "local"
tgt = "local"
[hosts.local]
kind = "local"
work_dir = "/tmp/amd-bench/run"
[vars]
posix_dir = "/tmp/amd-bench/posix"
"""
EXAMPLE_1_COMMAND = (
    "nixlbench --backend=POSIX --op_type=WRITE --initiator_seg_type=DRAM "
    "--scheme=pairwise --num_threads=1 --storage_enable_direct=true "
    "--check_consistency=true --filepath=/tmp/amd-bench/posix "
    "--large_blk_iter_ftr=4 --max_batch_size=4 --max_block_size=65536 "
    "--num_iter=64 --posix_api_type=URING --start_batch_size=4 "
    "--start_block_size=65536 --total_buffer_size=33554432 --warmup_iter=16"
)
EXAMPLE_2 = """
[defaults]
tier = "nightly"
attach = "net-tcp"

[[case]]
key = "vm2-net/ucx/{transport}/{mem}/{op}/S"
workload = "ucx/{transport}/{mem}/{op}/S"
profile = "S"
backend = "UCX"
runtime = "ASIO"
[case.axes]
transport = ["tcp", "rc", "ud"]
mem = ["DD", "VV", "DV", "VD"]
op = ["READ", "WRITE"]
[[case.when]]
match = { transport = ["rc", "ud"] }
set = { attach = "net-rdma" }
"""
EXAMPLE_2_ENV = """
schema = 1
env_class = "gha-kvm-vm2"
provides = ["gpu", "rdma", "two_hosts"]
[roles]
init = "guest1"
tgt = "guest2"
[hosts.guest1]
kind = "ssh"
ssh_config = "/tmp/guest-ssh-config"
ssh_host = "guest1"
work_dir = "nixl-bench/run"
setup = ["export ROCM_PATH=/opt/rocm"]
[hosts.guest1.vars]
ip = "192.168.100.11"
ucx_net_devices = "rocm-ernic0,ernic0:1"
[hosts.guest1.env]
[hosts.guest2]
kind = "ssh"
ssh_config = "/tmp/guest-ssh-config"
ssh_host = "guest2"
work_dir = "nixl-bench/run"
nixlbench = "nixl-bench/venv/bin/nixlbench"
[hosts.guest2.vars]
ip = "192.168.100.12"
ucx_net_devices = "rocm-ernic1,ernic1:1"
[vars]
asio_port_base = 21000
[tuning]
kill_after_s = 30
[[probe]]
name = "ibv"
host = "init"
when = "between"
classes = ["rdma"]
cmd = "ibv_devinfo -d ernic0 | grep -q PORT_ACTIVE"
timeout_s = 30
"""
UID = "0123456789abcdef0123"
EXAMPLE_2_DIR = f"/home/ubuntu/nixl-bench/run/{UID}/037-vm2-net__ucx__rc__VV__WRITE__S"
EXAMPLE_2_ARGS = (
    "--backend=UCX --runtime_type=ASIO --asio_address=192.168.100.11 "
    "--asio_port=21037 --op_type=WRITE --initiator_seg_type=VRAM "
    "--target_seg_type=VRAM --scheme=pairwise --num_threads=1 "
    "--check_consistency=true --large_blk_iter_ftr=4 --max_batch_size=4 "
    "--max_block_size=65536 --num_iter=64 --start_batch_size=4 "
    "--start_block_size=65536 --total_buffer_size=33554432 --warmup_iter=16"
)
EXAMPLE_2_INIT = (
    f"UCX_LOG_FILE={EXAMPLE_2_DIR}/init.ucx.log "
    "UCX_NET_DEVICES=rocm-ernic0,ernic0:1 UCX_PROTO_INFO=y UCX_TLS=rc,rocm "
    f"nixlbench {EXAMPLE_2_ARGS}"
)
EXAMPLE_2_TGT = (
    f"UCX_LOG_FILE={EXAMPLE_2_DIR}/tgt.ucx.log "
    "UCX_NET_DEVICES=rocm-ernic1,ernic1:1 UCX_PROTO_INFO=y UCX_TLS=rc,rocm "
    f"/home/ubuntu/nixl-bench/venv/bin/nixlbench {EXAMPLE_2_ARGS}"
)


class CommandTest(unittest.TestCase):
    def load(
        self, lane: str, body: str, env: str, **header: Any
    ) -> tuple[lib.Matrix, run.Env]:
        bench = Bench(self)
        (bench.matrix / "lanes" / f"{lane}.toml").write_text(
            lane_toml(lane, body, **header)
        )
        path = bench.root / "env.toml"
        path.write_text(env)
        return lib.load_matrix(bench.matrix), run.load_env(path)

    def test_worked_example_1(self) -> None:
        matrix, env = self.load(
            "bench-container", EXAMPLE_1, EXAMPLE_1_ENV, env_class="gha-container"
        )
        lane = matrix.lanes["bench-container"]
        self.assertEqual(len(lane.cases), 12)
        cases = {case.case_key: case for case in lane.cases}
        case = cases["bench-container/posix/URING/direct/WRITE/S"]
        self.assertEqual(run.check_case(case, matrix.profiles["S"], lane), [])
        plan = run.plan_case(
            case, matrix.profiles["S"], env, {"local": "/root"}, UID, 7
        )
        self.assertEqual(plan.problems, ())
        self.assertEqual(len(plan.procs), 1)
        proc = plan.procs[0]
        self.assertEqual(" ".join(proc.argv), EXAMPLE_1_COMMAND)
        self.assertEqual(run.cmdline(proc), EXAMPLE_1_COMMAND)
        self.assertEqual(proc.host, "local")
        self.assertEqual(proc.env, {})
        directory = "007-bench-container__posix__URING__direct__WRITE__S"
        self.assertEqual(plan.dirname, directory)
        self.assertEqual(
            proc.mkdirs,
            (f"/tmp/amd-bench/run/{UID}/{directory}", "/tmp/amd-bench/posix"),
        )
        script = run.launch_script(proc, env.hosts["local"], case.timeout_s, 30)
        self.assertIn(
            f"mkdir -p /tmp/amd-bench/run/{UID}/{directory} /tmp/amd-bench/posix && cd ",
            script,
        )
        self.assertIn(
            "timeout --signal=INT --kill-after=30s 120s nixlbench --backend=POSIX ",
            script,
        )
        self.assertIn('echo "$pid" > init.pid\n', script)
        self.assertIn('echo "amd-bench: exit rc=$rc" >&2\n', script)

        record = lib.case_record(case, matrix.profiles["S"], UID)
        self.assertEqual(record["flags"], "filepath={posix_dir} posix_api_type=URING")
        self.assertEqual(record["engine"], "URING")
        self.assertEqual(case.needs, ("io_uring",))

    def test_worked_example_2(self) -> None:
        matrix, env = self.load(
            "vm2-net",
            EXAMPLE_2,
            EXAMPLE_2_ENV,
            env_class="gha-kvm-vm2",
            timing_trust="emulated",
            topology="two-host",
        )
        lane = matrix.lanes["vm2-net"]
        self.assertEqual(len(lane.cases), 24)
        cases = {case.case_key: case for case in lane.cases}
        case = cases["vm2-net/ucx/rc/VV/WRITE/S"]
        self.assertEqual(case.attach, "net-rdma")
        self.assertEqual(case.needs, ("gpu", "rdma", "two_hosts"))
        self.assertEqual(run.check_case(case, matrix.profiles["S"], lane), [])
        homes = {"guest1": "/home/ubuntu", "guest2": "/home/ubuntu"}
        plan = run.plan_case(case, matrix.profiles["S"], env, homes, UID, 37)
        self.assertEqual(plan.problems, ())
        init, tgt = plan.procs
        self.assertEqual((init.role, init.host), ("init", "guest1"))
        self.assertEqual((tgt.role, tgt.host), ("tgt", "guest2"))
        self.assertEqual(run.cmdline(init), EXAMPLE_2_INIT)
        self.assertEqual(run.cmdline(tgt), EXAMPLE_2_TGT)
        self.assertEqual(init.argv[1:], tgt.argv[1:])
        script = run.launch_script(init, env.hosts["guest1"], case.timeout_s, 30)
        self.assertIn(
            "\nexport ROCM_PATH=/opt/rocm\ntest $? -eq 0 || "
            '{ echo "amd-bench: setup line 1 failed" >&2; exit 125; }\n'
            "export UCX_LOG_FILE=",
            script,
        )
        self.assertIn("export UCX_TLS=rc,rocm\n", script)
        self.assertEqual(
            run.Host(env.hosts["guest1"]).command(),
            ["ssh", "-F", "/tmp/guest-ssh-config", "-o", "BatchMode=yes"]
            + ["-o", "ConnectTimeout=15", "guest1", "bash", "-s"],
        )
        self.assertEqual(env.probes[0].classes, ("rdma",))
        unknown = run.plan_case(case, matrix.profiles["S"], env, {}, UID, 37)
        self.assertIn(
            "the home directory of host guest1 is not known", unknown.problems
        )

        tcp = run.plan_case(
            cases["vm2-net/ucx/tcp/DD/READ/S"],
            matrix.profiles["S"],
            env,
            homes,
            UID,
            1001,
        )
        self.assertIn("--asio_port=21001", tcp.procs[0].argv)
        self.assertEqual(tcp.procs[0].env["UCX_TLS"], "tcp")

    def test_one_host_uses_loopback_and_no_net_devices(self) -> None:
        matrix, env = self.load(
            "vm2-net", EXAMPLE_2, EXAMPLE_1_ENV.replace("gha-container", "test")
        )
        case = matrix.lanes["vm2-net"].cases[0]
        plan = run.plan_case(
            case, matrix.profiles["S"], env, {"local": "/root"}, UID, 3
        )
        init, tgt = plan.procs
        self.assertIn("--asio_address=127.0.0.1", init.argv)
        self.assertNotIn("UCX_NET_DEVICES", init.env)
        self.assertEqual(init.env["UCX_LOG_FILE"], f"{init.case_dir}/init.ucx.log")
        self.assertEqual(tgt.env["UCX_LOG_FILE"], f"{tgt.case_dir}/tgt.ucx.log")
        # Not even when the one host has a device list, as a guest does.
        devices = '[hosts.local.vars]\nucx_net_devices = "rocm-ernic0,ernic0:1"\n[vars]'
        matrix, env = self.load(
            "vm2-net",
            EXAMPLE_2,
            EXAMPLE_1_ENV.replace("gha-container", "test").replace("[vars]", devices),
        )
        plan = run.plan_case(
            case, matrix.profiles["S"], env, {"local": "/root"}, UID, 3
        )
        for proc in plan.procs:
            self.assertNotIn("UCX_NET_DEVICES", proc.env)

    def test_numbers_that_were_placeholders_are_checked_once_known(self) -> None:
        case = ucx_case(init_mem="VRAM", flags={"total_buffer_size": "{big}"})
        self.assertEqual(run.check_case(case, PROFILES["S"], LANE), [])
        for big, problems in (
            (33554432, ()),
            (1073741824, ("a VRAM case needs total_buffer_size of at most 67108864",)),
            (65536, None),
        ):
            _, env = self.load(
                "vm2-net",
                EXAMPLE_2,
                EXAMPLE_1_ENV.replace("gha-container", "test") + f"big = {big}\n",
            )
            plan = run.plan_case(case, PROFILES["S"], env, {"local": "/root"}, UID, 3)
            if problems is None:
                self.assertIn("exceeds total_buffer_size", plan.problems[0])
            else:
                self.assertEqual(plan.problems, problems)
            self.assertIn(f"--total_buffer_size={big}", plan.procs[0].argv)

    def test_plan_knows_the_secret_values(self) -> None:
        case = make_case(
            backend="OBJ",
            tgt_mem="OBJ",
            flags={"obj_secret_key": "{s3_secret_key}", "obj_access_key": "ci"},
            env={"REDIS_PASSWORD": "{pw}", "PLAIN": "not-a-secret"},
        )
        _, env = self.load(
            "vm2-net",
            EXAMPLE_2,
            EXAMPLE_1_ENV + 's3_secret_key = "hunter2-SECRET"\npw = "hunter2"\n',
            env_class="gha-container",
        )
        plan = run.plan_case(case, PROFILES["S"], env, {"local": "/root"}, UID, 3)
        # "ci" is too short to be told from ordinary text.
        self.assertEqual(plan.secrets, ("hunter2-SECRET", "hunter2"))
        text = 'key hunter2-SECRET and {"password": "hunter2"} ci'
        self.assertEqual(
            run.scrub(text, plan.secrets), 'key *** and {"password": "***"} ci'
        )

    def test_unresolved_and_banned_after_substitution(self) -> None:
        matrix, env = self.load(
            "bench-container",
            EXAMPLE_1,
            EXAMPLE_1_ENV.replace("posix_dir", "other_dir"),
        )
        case = matrix.lanes["bench-container"].cases[0]
        profile = matrix.profiles["S"]
        plan = run.plan_case(case, profile, env, {"local": "/root"}, UID, 0)
        self.assertEqual(plan.problems, ("unresolved {posix_dir}",))
        matrix, env = self.load(
            "bench-container",
            EXAMPLE_1,
            EXAMPLE_1_ENV.replace("/tmp/amd-bench/posix", "/data/tryfromenv"),
        )
        case = matrix.lanes["bench-container"].cases[0]
        plan = run.plan_case(case, profile, env, {"local": "/root"}, UID, 0)
        self.assertEqual(
            plan.problems, ("--filepath contains 'fromenv', which nixlbench bans",)
        )

    def test_ucx_tls(self) -> None:
        expected = {
            ("tcp", "DRAM", "DRAM"): "tcp",
            ("tcp", "VRAM", "DRAM"): "tcp,rocm",
            ("rc", "DRAM", "DRAM"): "rc",
            ("rc", "DRAM", "VRAM"): "rc,rocm",
            ("ud", "VRAM", "VRAM"): "ud,rocm",
            ("shm", "DRAM", "DRAM"): "shm",
            ("any", "VRAM", "VRAM"): None,
            ("none", "DRAM", "DRAM"): None,
        }
        for (transport, init_mem, tgt_mem), tls in expected.items():
            case = ucx_case(transport=transport, init_mem=init_mem, tgt_mem=tgt_mem)
            self.assertEqual(run.ucx_tls(case), tls, transport)
        self.assertIsNone(run.ucx_tls(make_case(transport="tcp")))
        libfabric = ucx_case(backend="LIBFABRIC", transport="verbs")
        self.assertIsNone(run.ucx_tls(libfabric))

    def test_secrets_are_masked_in_the_command_line(self) -> None:
        proc = run.ProcPlan(
            "init",
            "here",
            "/w",
            (
                "nixlbench",
                "--obj_secret_key=p w",
                "--obj_access_key=AKIDsecretA1",
                "--x=1",
            ),
            {"AWS_SECRET_ACCESS_KEY": "p w", "REDIS_PASSWORD": "pw", "PLAIN": "a b"},
            ("/w",),
        )
        shown = run.cmdline(proc)
        self.assertEqual(
            shown,
            "AWS_SECRET_ACCESS_KEY=*** PLAIN='a b' REDIS_PASSWORD=*** nixlbench "
            "--obj_secret_key=*** --obj_access_key=*** --x=1",
        )
        self.assertEqual(lib.find_secrets(shown), [])
        self.assertEqual(lib.redact_text(shown), shown)

    def test_env_file_problems(self) -> None:
        bench = Bench(self)
        path = bench.root / "bad.toml"
        path.write_text(
            EXAMPLE_2_ENV.replace('tgt = "guest2"', 'tgt = "guest3"')
            .replace('when = "between"', 'when = "often"')
            .replace("kill_after_s = 30", 'kill_after_s = "30"\nbogus = 1')
            + "[other]\n"
        )
        with self.assertRaises(run.EnvError) as caught:
            run.load_env(path)
        text = "\n".join(caught.exception.problems)
        for expected in (
            "env: unknown key 'other'",
            "[tuning]: unknown key 'bogus'",
            "[tuning]: 'kill_after_s' has the wrong type",
            "tgt names the unknown host 'guest3'",
            "'when' must be start, between or end",
        ):
            self.assertIn(expected, text)
        # A probe on a role the env does not have would fail at its first run.
        path.write_text(
            EXAMPLE_1_ENV.replace('tgt = "local"\n', "")
            + '[[probe]]\nname = "p"\nhost = "tgt"\nwhen = "start"\ncmd = "true"\n'
        )
        with self.assertRaises(run.EnvError) as caught:
            run.load_env(path)
        self.assertIn("no role or host 'tgt'", "\n".join(caught.exception.problems))
        as_json = bench.root / "env.json"
        as_json.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "env_class": "x",
                    "roles": {"init": "a"},
                    "hosts": {"a": {"kind": "local", "work_dir": "/w"}},
                }
            )
        )
        env = run.load_env(as_json)
        self.assertEqual(env.tuning, run.Tuning())
        self.assertEqual(env.asio_port_base, 21000)
        self.assertEqual(env.hosts["a"].nixlbench, "nixlbench")


# The stderr of real nixlbench processes, as the runner logged them.
JOB_LINE = (
    "bash: line 9: 262794 Aborted                 timeout --signal=INT "
    "--kill-after=2s 120s nixlbench --backend=UCX --runtime_type=ASIO "
    "--asio_address=127.0.0.1 --asio_port=21012 --op_type=WRITE"
)
ASIO_ABORT = (
    "amd-bench: ulimit -l unlimited refused\n"
    "terminate called after throwing an instance of 'std::runtime_error'\n"
    "  what():  ASIO Read head failed: End of file\n"
    "timeout: the monitored command dumped core\n"
    f"{JOB_LINE}\n"
    "amd-bench: exit rc=134"
)


def result(role: str, rc: int | None = 0, **changes: Any) -> run.ProcResult:
    fields: dict[str, Any] = dict(
        role=role,
        kind="local",
        rc=rc,
        marker=True,
        stdout="",
        stderr="",
        wall_s=1.0,
        ended=1.0,
    )
    fields.update(changes)
    return run.ProcResult(**fields)


class EvaluateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.case = ucx_case()
        self.flags = dict(lib.effective_flags(self.case, PROFILES["S"]))
        row = f"{65536:<20}{4:<15}{'1.0':<15}" + f"{'2.0':<15}" * 7
        header = "".join(f"{name:<20}" for name in run.HEADER_10)
        self.good = (
            f"{'Check consistency (--check_consistency=[0,1])':<60}: 1\n"
            f"{header}\n{'-' * 160}\n{row}\n"
        )
        self.connect = run.CONNECT_LINE + "\n"
        self.ucx = (OUTPUT / "ucx_proto_tcp.ucx.log").read_text()

    def verdict(self, *results: run.ProcResult, ucx: str | None = None) -> run.Verdict:
        log = self.ucx if ucx is None else ucx
        return run.evaluate(self.case, self.flags, results, log)

    def pair(self, **tgt: Any) -> tuple[run.ProcResult, run.ProcResult]:
        fields: dict[str, Any] = dict(stdout=self.connect, ended=2.0)
        fields.update(tgt)
        return result("init", stdout=self.good), result("tgt", **fields)

    def test_pass(self) -> None:
        verdict = self.verdict(*self.pair())
        self.assertEqual((verdict.status, verdict.error_class), ("pass", ""))
        self.assertEqual(verdict.consistency, "clean")
        self.assertEqual(verdict.transport_obs, "tcp")
        self.assertEqual(len(verdict.table.rows), 1)

    def test_unknown_transport_never_fails(self) -> None:
        verdict = self.verdict(*self.pair(), ucx="not a table\n")
        self.assertEqual((verdict.status, verdict.transport_obs), ("pass", "unknown"))

    def test_transport_gate(self) -> None:
        rc = (OUTPUT / "ucx_proto_rc.ucx.log").read_text()
        verdict = self.verdict(*self.pair(), ucx=rc)
        self.assertEqual((verdict.status, verdict.error_class), ("fail", "transport"))
        self.assertEqual(verdict.transport_obs, "rc+rocm_copy")
        self.case = ucx_case(transport="shm")
        self.assertEqual(self.verdict(*self.pair()).error_class, "transport")
        self.case = ucx_case(transport="any")
        self.assertEqual(self.verdict(*self.pair(), ucx=rc).status, "pass")

    def test_role_swap(self) -> None:
        init, tgt = self.pair()
        init.stdout = self.connect + init.stdout
        self.assertEqual(self.verdict(init, tgt).error_class, "role-swap")
        init, tgt = self.pair(stdout="")
        self.assertEqual(self.verdict(init, tgt).error_class, "role-swap")
        # A target that died before the runtime is an exit failure, not a swap.
        init, tgt = self.pair(stdout="", rc=127, stderr="nixlbench: not found")
        verdict = self.verdict(init, tgt)
        self.assertEqual(verdict.error_class, "exit-nonzero")
        self.assertEqual(verdict.excerpt, "nixlbench: not found")

    def test_target_consistency_failure(self) -> None:
        verdict = self.verdict(*self.pair(rc=1, stderr="Consistency check failed"))
        self.assertEqual((verdict.status, verdict.error_class), ("fail", "consistency"))
        self.assertEqual(verdict.consistency, "failed")

    def test_the_abort_of_the_surviving_peer_does_not_decide(self) -> None:
        # What nixlbench does: the peer of a process that exits 1 aborts over
        # its runtime, a moment later or, as polled, a moment earlier.
        check = "Consistency check failed for iov 0:0\nConsistency check failed"
        backend = "createBackend: backend 'UCX' encountered error"
        for name, init, tgt, expected, excerpt in (
            ("write", 134, (1, check), "consistency", "Consistency check failed"),
            ("backend", 134, (1, backend), "exit-nonzero", backend),
            ("no binary", 134, (127, "nixlbench: not found"), "exit-nonzero", "found"),
            ("target killed", 134, (137, "Killed"), "kill", "Killed"),
        ):
            for ended in (0.5, 3.0):
                with self.subTest(name=name, ended=ended):
                    one = result("init", init, stderr=ASIO_ABORT, ended=ended)
                    two = result("tgt", tgt[0], stdout=self.connect, stderr=tgt[1])
                    two.ended = 2.0
                    verdict = self.verdict(one, two)
                    self.assertEqual(verdict.error_class, expected)
                    self.assertTrue(verdict.excerpt.endswith(excerpt), verdict.excerpt)
        # A READ is checked by the initiator, and then the target aborts.
        one = result("init", 1, stdout=self.good, stderr=check, ended=2.0)
        two = result("tgt", 134, stdout=self.connect, stderr=ASIO_ABORT, ended=1.9)
        verdict = self.verdict(one, two)
        self.assertEqual((verdict.status, verdict.error_class), ("fail", "consistency"))
        self.assertEqual(verdict.consistency, "failed")
        # Alone, the abort is the failure.
        one = result("init", 134, stderr=ASIO_ABORT)
        verdict = self.verdict(one)
        self.assertEqual((verdict.status, verdict.error_class), ("crash", "asio"))
        self.assertIn("what(): ASIO Read head failed: End of file", verdict.excerpt)

    def test_a_crash_is_asio_only_when_nixlbench_says_so(self) -> None:
        # bash repeats the command of a job a signal ended, and that of every
        # pair has --runtime_type=ASIO in it.
        init, tgt = self.pair()
        init.rc, init.stderr = 134, JOB_LINE
        verdict = self.verdict(init, tgt)
        self.assertEqual((verdict.status, verdict.error_class), ("crash", "signal"))
        self.assertEqual(verdict.excerpt, "bash: line 9: 262794 Aborted")

    def test_query_has_no_data_check(self) -> None:
        self.case = make_case(op="QUERY")
        self.flags = dict(lib.effective_flags(self.case, PROFILES["S"]))
        verdict = self.verdict(result("init", stdout=self.good))
        self.assertEqual((verdict.status, verdict.consistency), ("pass", "off"))

    def test_consistency_unknown_and_off(self) -> None:
        init, tgt = self.pair()
        init.stdout = init.stdout.split("\n", 1)[1]
        verdict = self.verdict(init, tgt)
        self.assertEqual(
            (verdict.error_class, verdict.consistency), ("consistency", "unknown")
        )
        self.flags["check_consistency"] = False
        verdict = self.verdict(init, tgt)
        self.assertEqual((verdict.status, verdict.consistency), ("pass", "off"))

    def test_exit_classes(self) -> None:
        cases = {
            124: ("timeout", "timeout"),
            137: ("timeout", "kill"),
            134: ("crash", "signal"),
            1: ("fail", "exit-nonzero"),
        }
        for rc, expected in cases.items():
            init, tgt = self.pair()
            init.rc = rc
            verdict = self.verdict(init, tgt)
            self.assertEqual((verdict.status, verdict.error_class), expected, rc)
        init, tgt = self.pair(rc=134, stderr="terminate called\nASIO Receive timeout")
        verdict = self.verdict(init, tgt)
        self.assertEqual((verdict.status, verdict.error_class), ("crash", "asio"))

    def test_no_marker(self) -> None:
        init, tgt = self.pair()
        init.marker, init.rc, init.stderr = False, 255, "ssh: connect refused"
        self.assertEqual(self.verdict(init, tgt).error_class, "launch")
        init.kind = "ssh"
        verdict = self.verdict(init, tgt)
        self.assertEqual((verdict.status, verdict.error_class), ("fail", "ssh"))
        self.assertEqual(verdict.excerpt, "ssh: connect refused")

    def test_an_early_stopped_peer_does_not_decide(self) -> None:
        init = result(
            "init",
            1,
            stderr="l1\nl2\nl3\nl4\nl5\nboom\namd-bench: exit rc=1",
            ended=1.0,
        )
        tgt = result("tgt", 137, stdout=self.connect, ended=5.0, stopped=True)
        verdict = self.verdict(init, tgt)
        self.assertEqual(
            (verdict.status, verdict.error_class), ("fail", "exit-nonzero")
        )
        self.assertEqual(verdict.excerpt, "l2 | l3 | l4 | l5 | boom")
        # With nothing else wrong, a peer that had to be stopped is the failure.
        init, tgt = self.pair(rc=1, stopped=True)
        self.assertEqual(self.verdict(init, tgt).error_class, "exit-nonzero")

    def test_rows_and_echo(self) -> None:
        init, tgt = self.pair()
        self.flags["max_batch_size"] = 8
        self.assertEqual(self.verdict(init, tgt).error_class, "rows")
        self.flags["max_batch_size"] = 4
        init.stdout = f"{'Num iter (--num_iter=N)':<60}: 80\n" + init.stdout
        verdict = self.verdict(init, tgt)
        self.assertEqual(verdict.error_class, "config-echo")
        self.assertEqual(verdict.excerpt, "num_iter asked 64 ran 80")

    def test_excerpt_is_cut_and_redacted(self) -> None:
        init, tgt = self.pair()
        init.rc = 1
        init.stderr = "REDIS_PASSWORD=hunter2 " + "x" * 600
        excerpt = self.verdict(init, tgt).excerpt
        self.assertEqual(len(excerpt), 500)
        self.assertTrue(excerpt.startswith("REDIS_PASSWORD=*** xxx"))


class ScheduleTest(unittest.TestCase):
    def test_rank_rounds_and_aa(self) -> None:
        cases = [
            ucx_case(case_key="t/rc-vram", index=0, transport="rc", init_mem="VRAM"),
            ucx_case(case_key="t/rc", index=1, transport="rc", sentinel=True),
            ucx_case(case_key="t/tcp-vram", index=2, tgt_mem="VRAM"),
            ucx_case(case_key="t/tcp", index=3, sentinel=True),
            make_case(case_key="t/posix", index=4),
            make_case(case_key="t/parked", index=5, status="blocked", sentinel=True),
        ]
        lane = lib.Lane(
            "t",
            "T",
            "gha-kvm-vm2",
            "emulated",
            "two-host",
            "active",
            (),
            "t/tcp",
            "",
            (),
            (),
        )
        entries = run.schedule(lane, cases, 3, True)
        found = [(e.position, e.case.case_key, e.variant, e.round) for e in entries]
        self.assertEqual(
            found,
            [
                (0, "t/tcp", "head", 0),
                (1, "t/tcp", "aa", 0),
                (2, "t/posix", "head", 0),
                (3, "t/parked", "head", 0),
                (4, "t/tcp-vram", "head", 0),
                (5, "t/rc", "head", 0),
                (6, "t/rc-vram", "head", 0),
                (7, "t/tcp", "head", 1),
                (8, "t/rc", "head", 1),
                (9, "t/tcp", "head", 2),
                (10, "t/rc", "head", 2),
            ],
        )
        plain = run.schedule(lane, cases, 1, False)
        self.assertEqual(len(plain), 6)
        self.assertEqual([run.rank(case, lane) for case in cases], [3, 2, 1, 0, 0, 0])
        self.assertEqual([run.rank(case, LANE) for case in cases[:4]], [4, 2, 4, 0])
        self.assertEqual(run.rank(make_case(backend="AIS", init_mem="VRAM"), LANE), 1)
        self.assertEqual(run.dir_name(7, "t/a b/c", "head", 0), "007-t__a_b__c")
        self.assertEqual(run.dir_name(7, "t/x", "aa", 0), "007-t__x.aa")
        self.assertEqual(run.dir_name(1007, "t/x", "head", 2), "1007-t__x.r2")


PAIRS = """
[[case]]
key = "m/ucx/tcp/ok"
workload = "ucx/tcp/ok"
backend = "UCX"
transport = "tcp"
op = "WRITE"

[[case]]
key = "m/ucx/rc/vram"
workload = "ucx/rc/vram"
backend = "UCX"
transport = "rc"
init_mem = "VRAM"
tgt_mem = "VRAM"
op = "READ"

[[case]]
key = "m/ucx/tcp/tgt-inconsistent"
workload = "ucx/tcp/tgt-inconsistent"
backend = "UCX"
transport = "tcp"
op = "WRITE"
env = { FAKE_NIXLBENCH_TGT_MODE = "inconsistent" }

[[case]]
key = "m/ucx/rc/mismatch"
workload = "ucx/rc/mismatch"
backend = "UCX"
transport = "rc"
op = "WRITE"
env = { UCX_TLS = "tcp" }

[[case]]
key = "m/ucx/tcp/init-fail"
workload = "ucx/tcp/init-fail"
backend = "UCX"
transport = "tcp"
op = "WRITE"
env = { FAKE_NIXLBENCH_MODE = "fail", FAKE_NIXLBENCH_TGT_MODE = "hang" }

[[case]]
key = "m/ucx/tcp/tgt-fail"
workload = "ucx/tcp/tgt-fail"
backend = "UCX"
transport = "tcp"
op = "WRITE"
env = { FAKE_NIXLBENCH_MODE = "hang", FAKE_NIXLBENCH_TGT_MODE = "fail" }

[[case]]
key = "m/ucx/tcp/tgt-dies"
workload = "ucx/tcp/tgt-dies"
backend = "UCX"
transport = "tcp"
op = "WRITE"
env = { FAKE_NIXLBENCH_TGT_MODE = "fail" }

[[case]]
key = "m/ucx/tcp/init-inconsistent"
workload = "ucx/tcp/init-inconsistent"
backend = "UCX"
transport = "tcp"
op = "READ"
env = { FAKE_NIXLBENCH_MODE = "inconsistent" }

[[case]]
key = "m/ucx/tcp/init-abort"
workload = "ucx/tcp/init-abort"
backend = "UCX"
transport = "tcp"
op = "WRITE"
env = { FAKE_NIXLBENCH_MODE = "abort" }

[[case]]
key = "m/obj/creds"
workload = "obj/creds"
backend = "OBJ"
tgt_mem = "OBJ"
op = "WRITE"
env = { FAKE_NIXLBENCH_MODE = "creds", FAKE_NIXLBENCH_AZURE_SECRET = "azure-CONN-7" }
[case.flags]
obj_access_key = "{s3_access_key}"
obj_secret_key = "{s3_secret_key}"
obj_session_token = "tok-SESSION-9"
obj_bucket_name = "bucket"
"""
SECRETS = ("AKIDsecretA1", "hunter2-SECRET", "tok-SESSION-9", "azure-CONN-7")
MODES = ("ok", "fail", "inconsistent", "short", "bad-header", "echo-mismatch", "abort")


class ModesTest(unittest.TestCase):
    """One run over every behaviour of the fake that ends by itself."""

    done: Result
    summary: pathlib.Path

    @classmethod
    def setUpClass(cls) -> None:
        bench = Bench(cls, for_class=True)
        bench.lane("m", storage("m", *MODES, "twelve") + PAIRS)
        cls.summary = bench.root / "summary.md"
        cls.done = bench.run(
            "m",
            "--summary-out",
            str(cls.summary),
            env=bench.env(provides=("gpu", "rdma")),
        )

    def expect(self, key: str, status: str, error_class: str) -> dict[str, Any]:
        case = self.done.case(f"m/{key}")
        found = (case["status"], case["error_class"])
        self.assertEqual(found, (status, error_class), case["error_excerpt"])
        return case

    def test_exit_code_and_run_records(self) -> None:
        self.assertEqual(self.done.code, 0, self.done.text)
        first, last = self.done.records[0], self.done.records[-1]
        self.assertEqual((first["kind"], last["kind"]), ("run", "run"))
        self.assertEqual((first["status"], last["status"]), ("partial", "complete"))
        self.assertEqual(first["run_uid"], last["run_uid"])
        self.assertEqual(len(self.done.kind("run")), 2)
        self.assertEqual(last["lane"], "m")
        self.assertEqual(last["tier"], "pr")
        self.assertEqual(last["repo"], "local")
        self.assertEqual(last["rocm_version"], "unknown")
        self.assertEqual(last["bench_run_id"], f"local-{last['stamp']}")
        self.assertIsNone(last["calib_gbps"])
        self.assertIsNone(last["guest_kernel"])
        self.assertIsNone(last["degraded"])
        self.assertGreater(last["runner_disk_free_after"], 0)
        self.assertRegex(last["finished_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        manifest = lib.matrix_manifest(lib.load_matrix(self.done.out.parent / "bench"))
        self.assertEqual(last["matrix_sha256"], lib.matrix_sha256(manifest))
        self.assertEqual(last["env_fp"], lib.env_fingerprint(last))
        expected = lib.run_uid("local", last["bench_run_id"], 1, "m", "unknown")
        self.assertEqual(last["run_uid"], expected)

    def test_records_validate_against_the_schema(self) -> None:
        try:
            schema = lib.load_schema()
        except lib.BenchError as exc:
            self.skipTest(f"no schema yet: {exc}")
        self.assertGreater(len(self.done.records), 20)
        for record in self.done.records:
            self.assertEqual(lib.validate_record(record, schema), [], record)
            self.assertEqual(lib.find_secrets(record), [], record)
            self.assertEqual(tuple(record), lib.FIELDS[record["kind"]])

    def test_storage_pass(self) -> None:
        case = self.expect("posix/ok", "pass", "")
        self.assertEqual((case["exit_init"], case["exit_tgt"]), (0, None))
        self.assertEqual((case["rows_expected"], case["rows_seen"]), (1, 1))
        self.assertEqual(case["consistency"], "clean")
        self.assertIsNone(case["transport_obs"])
        self.assertIsNone(case["skip_reason"])
        self.assertEqual(case["order"], 0)
        self.assertEqual(case["log_ref"], "logs/000-m__posix__ok")
        self.assertTrue(case["cmdline"].startswith(f"FAKE_NIXLBENCH_MODE=ok {FAKE} "))
        self.assertIn(" --backend=POSIX --op_type=WRITE ", case["cmdline"])
        self.assertIn(f"--filepath={self.done.out.parent}/posix ", case["cmdline"])
        self.assertTrue((self.done.out.parent / "posix").is_dir())
        self.assertGreater(case["duration_s"], 0)
        self.assertIsInstance(case["load1_before"], float)
        self.assertIsInstance(case["load1_after"], float)
        self.assertRegex(case["started_at"], r"Z$")
        samples = self.done.of_case("sample", case)
        self.assertEqual(len(samples), 1)
        self.assertEqual(
            (samples[0]["block_size"], samples[0]["batch_size"]), (65536, 4)
        )
        self.assertEqual(samples[0]["bw_gbps"], 1.25)
        self.assertEqual(samples[0]["tx_p99_us"], 2.0)
        self.assertIsNone(samples[0]["agg_bw_gbps"])
        counters = {c["name"]: c for c in self.done.of_case("counter", case)}
        self.assertEqual(list(counters), ["proc_wall_s.init"])
        self.assertEqual(counters["proc_wall_s.init"]["unit"], "s")
        self.assertFalse(counters["proc_wall_s.init"]["deterministic"])
        self.assertIn("Block Size (B)", self.done.log(case, "init.stdout.log"))
        self.assertIn("amd-bench: exit rc=0", self.done.log(case, "init.stderr.log"))

    def test_twelve_columns_pass(self) -> None:
        case = self.expect("posix/twelve", "pass", "")
        sample = self.done.of_case("sample", case)[0]
        self.assertEqual((sample["agg_bw_gbps"], sample["net_util_pct"]), (2.5, 1.0))

    def test_fail(self) -> None:
        case = self.expect("posix/fail", "fail", "exit-nonzero")
        self.assertEqual(case["exit_init"], 1)
        self.assertEqual(case["error_excerpt"], "fake nixlbench: failing as asked")
        self.assertEqual(case["consistency"], "unknown")
        self.assertEqual(case["rows_seen"], 0)
        self.assertEqual(self.done.of_case("sample", case), [])

    def test_inconsistent(self) -> None:
        case = self.expect("posix/inconsistent", "fail", "consistency")
        self.assertEqual(case["consistency"], "failed")
        self.assertEqual(case["error_excerpt"], "Consistency check failed")
        self.assertEqual(len(self.done.of_case("sample", case)), 1)

    def test_short(self) -> None:
        case = self.expect("posix/short", "fail", "rows")
        self.assertEqual((case["rows_expected"], case["rows_seen"]), (1, 0))

    def test_bad_header(self) -> None:
        case = self.expect("posix/bad-header", "fail", "table")
        self.assertIn("unknown table header: Block Size (B)", case["error_excerpt"])

    def test_echo_mismatch(self) -> None:
        case = self.expect("posix/echo-mismatch", "fail", "config-echo")
        self.assertEqual(case["error_excerpt"], "num_iter asked 64 ran 80")
        self.assertEqual(len(self.done.of_case("sample", case)), 1)

    def test_abort_is_a_crash(self) -> None:
        case = self.expect("posix/abort", "crash", "signal")
        self.assertEqual(case["exit_init"], 134)

    def test_pair_pass(self) -> None:
        case = self.expect("ucx/tcp/ok", "pass", "")
        self.assertEqual((case["exit_init"], case["exit_tgt"]), (0, 0))
        self.assertEqual(case["transport_obs"], "tcp")
        self.assertIn(" UCX_PROTO_INFO=y UCX_TLS=tcp ", case["cmdline"])
        self.assertIn("/init.ucx.log ", case["cmdline"])
        self.assertIn("--asio_address=127.0.0.1 ", case["cmdline"])
        self.assertNotIn("UCX_NET_DEVICES", case["cmdline"])
        counters = {c["name"]: c for c in self.done.of_case("counter", case)}
        self.assertEqual(
            list(counters),
            ["proc_wall_s.init", "proc_wall_s.tgt", "pair_skew_s", "ucx_warn_lines"],
        )
        self.assertGreaterEqual(counters["pair_skew_s"]["value"], 0.05)
        self.assertLess(counters["pair_skew_s"]["value"], 5)
        self.assertEqual(counters["ucx_warn_lines"]["value"], 0)
        self.assertEqual(counters["ucx_warn_lines"]["unit"], "count")
        self.assertIn(run.CONNECT_LINE, self.done.log(case, "tgt.stdout.log"))
        self.assertNotIn(run.CONNECT_LINE, self.done.log(case, "init.stdout.log"))
        self.assertIn("ucp_put", self.done.log(case, "init.ucx.log"))
        self.assertIn("ucp_put", self.done.log(case, "tgt.ucx.log"))

    def test_pair_vram_over_rc_runs_last(self) -> None:
        case = self.expect("ucx/rc/vram", "pass", "")
        self.assertEqual(case["transport_obs"], "rc+rocm_copy")
        self.assertIn(" UCX_TLS=rc,rocm ", case["cmdline"])
        orders = [c["order"] for c in self.done.kind("case")]
        self.assertEqual(case["order"], max(orders))
        self.assertEqual(orders, sorted(orders))

    def test_pair_target_exit_code_is_gated(self) -> None:
        # As with nixlbench, the initiator aborts once its target is gone.
        case = self.expect("ucx/tcp/tgt-inconsistent", "fail", "consistency")
        self.assertEqual((case["exit_init"], case["exit_tgt"]), (134, 1))
        self.assertEqual(case["consistency"], "failed")
        self.assertEqual(case["error_excerpt"], "Consistency check failed")
        self.assertEqual(len(self.done.of_case("sample", case)), 1)

    def test_pair_failures_are_classed_by_the_process_that_failed(self) -> None:
        case = self.expect("ucx/tcp/tgt-dies", "fail", "exit-nonzero")
        self.assertEqual((case["exit_init"], case["exit_tgt"]), (134, 1))
        self.assertEqual(
            case["error_excerpt"], "fake nixlbench: the target fails as asked"
        )
        case = self.expect("ucx/tcp/init-inconsistent", "fail", "consistency")
        self.assertEqual((case["exit_init"], case["exit_tgt"]), (1, 134))
        self.assertEqual(case["error_excerpt"], "Consistency check failed")
        # The target's abort over ASIO is not what went wrong, and neither
        # is the --runtime_type=ASIO of the command bash repeats.
        case = self.expect("ucx/tcp/init-abort", "crash", "signal")
        self.assertEqual((case["exit_init"], case["exit_tgt"]), (134, 134))
        self.assertNotIn("--runtime_type", case["error_excerpt"])
        self.assertIn("ASIO Read head failed", self.done.log(case, "tgt.stderr.log"))

    def test_transport_mismatch(self) -> None:
        case = self.expect("ucx/rc/mismatch", "fail", "transport")
        self.assertEqual(case["transport_obs"], "tcp")
        self.assertEqual(case["error_excerpt"], "UCX selected tcp, not rc")

    def test_initiator_failure_stops_a_hung_target_early(self) -> None:
        case = self.expect("ucx/tcp/init-fail", "fail", "exit-nonzero")
        self.assertEqual(case["exit_init"], 1)
        self.assertEqual(case["error_excerpt"], "fake nixlbench: failing as asked")
        # GNU timeout passes the command's status on; uutils reports 124.
        self.assertIn(case["exit_tgt"], (1, 124))
        self.assertLess(case["duration_s"], 15)
        self.assertIn("Ctrl-C received", self.done.log(case, "tgt.stdout.log"))

    def test_target_failure_stops_a_hung_initiator_early(self) -> None:
        case = self.expect("ucx/tcp/tgt-fail", "fail", "exit-nonzero")
        self.assertEqual(case["exit_tgt"], 1)
        self.assertEqual(
            case["error_excerpt"], "fake nixlbench: the target fails as asked"
        )
        self.assertIn(case["exit_init"], (1, 124))
        self.assertLess(case["duration_s"], 15)
        self.assertIn("Ctrl-C received", self.done.log(case, "init.stdout.log"))

    def test_credentials_are_redacted_everywhere(self) -> None:
        case = self.expect("obj/creds", "pass", "")
        self.assertIn("--obj_access_key=*** ", case["cmdline"])
        self.assertIn("--obj_secret_key=*** ", case["cmdline"])
        self.assertIn("--obj_session_token=*** ", case["cmdline"])
        self.assertIn("FAKE_NIXLBENCH_AZURE_SECRET=*** ", case["cmdline"])
        self.assertIn("obj_secret_key=***", case["flags"])
        stdout = self.done.log(case, "init.stdout.log")
        for label in (
            "OBJ S3 access key",
            "OBJ S3 secret key",
            "OBJ S3 session token",
            "Azure Blob Storage connection string",
        ):
            self.assertRegex(stdout, rf"(?m)^{label} .*: \*\*\*$")
        stderr = self.done.log(case, "init.stderr.log")
        self.assertIn("AWS_SECRET_ACCESS_KEY=*** was exported", stderr)
        for path in self.done.out.rglob("*"):
            if path.is_file():
                text = path.read_text()
                for secret in SECRETS:
                    self.assertNotIn(secret, text, path)
        for secret in SECRETS:
            self.assertNotIn(secret, self.done.text)

    def test_summary(self) -> None:
        text = self.summary.read_text()
        self.assertIn("### nixlbench m (pr, ROCm unknown): complete", text)
        self.assertIn("| `m/posix/ok` | pass |  | 1/1 | ", text)
        self.assertIn("| `m/posix/short` | fail | rows | 0/1 | ", text)
        self.assertIn("000 pass ", self.done.text)


KILLS = """
[[case]]
key = "k/posix/hang"
workload = "posix/hang"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
timeout_s = 2
flags = { filepath = "{posix_dir}" }
env = { FAKE_NIXLBENCH_MODE = "hang" }

[[case]]
key = "k/posix/hang-hard"
workload = "posix/hang-hard"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
timeout_s = 2
needs = ["gpu"]
flags = { filepath = "{posix_dir}" }
env = { FAKE_NIXLBENCH_MODE = "hang-hard" }

[[case]]
key = "k/posix/after-gpu"
workload = "posix/after-gpu"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
needs = ["gpu"]
required = true
flags = { filepath = "{posix_dir}" }

[[case]]
key = "k/posix/after-plain"
workload = "posix/after-plain"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
flags = { filepath = "{posix_dir}" }
"""


class TimeoutTest(unittest.TestCase):
    done: Result

    @classmethod
    def setUpClass(cls) -> None:
        bench = Bench(cls, for_class=True)
        bench.lane("k", KILLS)
        cls.done = bench.run("k", env=bench.env(provides=("gpu",)))

    def test_sigint_ends_a_hung_process(self) -> None:
        case = self.done.case("k/posix/hang")
        self.assertEqual((case["status"], case["error_class"]), ("timeout", "timeout"))
        self.assertEqual(case["exit_init"], 124)
        self.assertGreaterEqual(case["duration_s"], 2)
        self.assertLess(case["duration_s"], 4)
        self.assertIn("Ctrl-C received", self.done.log(case, "init.stdout.log"))
        self.assertEqual(case["rows_seen"], 0)

    def test_kill_follows_an_ignored_sigint(self) -> None:
        case = self.done.case("k/posix/hang-hard")
        self.assertEqual((case["status"], case["error_class"]), ("timeout", "kill"))
        self.assertEqual(case["exit_init"], 137)
        self.assertGreaterEqual(case["duration_s"], 4)
        self.assertLess(case["duration_s"], 8)
        self.assertNotIn("Ctrl-C received", self.done.log(case, "init.stdout.log"))

    def test_a_kill_makes_the_device_class_suspect(self) -> None:
        case = self.done.case("k/posix/after-gpu")
        self.assertEqual(
            (case["status"], case["skip_reason"]), ("skip", "device-suspect")
        )
        self.assertIn("gpu", case["error_excerpt"])
        self.assertIsNone(case["order"])
        self.assertIsNone(case["log_ref"])
        self.assertEqual(self.done.case("k/posix/after-plain")["status"], "pass")
        self.assertEqual(self.done.code, 1, "a required case was skipped as suspect")
        self.assertEqual(self.done.records[-1]["status"], "complete")


SCHEDULING = """
[[case]]
key = "s/posix/a1"
workload = "posix/a1"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
sentinel = true
flags = { filepath = "{posix_dir}" }

[[case]]
key = "s/posix/a2"
workload = "posix/a2"
backend = "POSIX"
op = "READ"
tgt_mem = "FILE"
flags = { filepath = "{posix_dir}" }

[[case]]
key = "s/posix/uring"
workload = "posix/uring"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
needs = ["io_uring"]
flags = { filepath = "{posix_dir}" }

[[case]]
key = "s/posix/nowhere"
workload = "posix/nowhere"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
flags = { filepath = "{nope_dir}/{self.also_nope}" }

[[case]]
key = "s/posix/gpu"
workload = "posix/gpu"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
needs = ["gpu"]
flags = { filepath = "{posix_dir}" }

[[case]]
key = "s/fabric/parked"
workload = "fabric/parked"
backend = "LIBFABRIC"
transport = "verbs"
op = "WRITE"
status = "blocked"
blocked_by = ["E8", "W15"]

[[case]]
key = "s/posix/weekly"
workload = "posix/weekly"
backend = "POSIX"
op = "WRITE"
tgt_mem = "FILE"
tier = "weekly"
flags = { filepath = "{posix_dir}" }
"""
PROBES = """
[[probe]]
name = "hip"
host = "init"
when = "start"
classes = ["gpu"]
cmd = "echo no device >&2; exit 3"
[[probe]]
name = "ring"
host = "here"
when = "between"
classes = ["io_uring"]
cmd = "true"
[[probe]]
name = "slow"
when = "end"
classes = ["rdma"]
cmd = "sleep 30"
timeout_s = 1
"""


class SchedulingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = Bench(self)
        self.bench.lane("s", SCHEDULING, aa_case="s/posix/a1")

    def statuses(self, done: Result) -> dict[str, tuple[str, str | None]]:
        return {
            f"{c['case_key']}{'' if c['variant'] == 'head' else '.aa'}.{c['round']}": (
                c["status"],
                c["skip_reason"],
            )
            for c in done.kind("case")
        }

    def test_skips_blocked_and_only(self) -> None:
        env = self.bench.env(provides=("gpu",))
        done = self.bench.run(
            "s", "--only", "s/posix/a*", "--only", "s/*/[np]*", env=env
        )
        self.assertEqual(done.code, 0, done.text)
        self.assertEqual(
            self.statuses(done),
            {
                "s/posix/a1.0": ("pass", None),
                "s/posix/a2.0": ("pass", None),
                "s/posix/uring.0": ("skip", "filter"),
                "s/posix/nowhere.0": ("skip", "env"),
                "s/posix/gpu.0": ("skip", "filter"),
                "s/fabric/parked.0": ("blocked", None),
            },
        )
        parked = done.case("s/fabric/parked")
        self.assertEqual(parked["blocked_by"], ["E8", "W15"])
        self.assertIsNone(parked["order"])
        self.assertIsNone(parked["cmdline"])
        nowhere = done.case("s/posix/nowhere")
        self.assertEqual(
            nowhere["error_excerpt"], "unresolved {nope_dir}, {self.also_nope}"
        )
        self.assertEqual([c["order"] for c in done.kind("case")][:2], [0, 1])
        self.assertEqual(done.records[-1]["status"], "complete")
        self.assertEqual(
            sorted(p.name for p in (done.out / "logs").iterdir()),
            [
                "000-s__posix__a1",
                "001-s__posix__a2",
            ],
        )

    def test_missing_need(self) -> None:
        done = self.bench.run("s")
        uring = done.case("s/posix/uring")
        self.assertEqual((uring["status"], uring["skip_reason"]), ("skip", "env"))
        self.assertEqual(uring["error_excerpt"], "needs io_uring")
        self.assertEqual(done.case("s/posix/gpu")["error_excerpt"], "needs gpu")
        self.assertIsNone(uring["exit_init"])
        self.assertEqual(done.code, 0)

    def test_budget(self) -> None:
        done = self.bench.run("s", "--budget-s", "0", tier="weekly")
        self.assertEqual(done.code, 0, done.text)
        statuses = self.statuses(done)
        self.assertEqual(statuses.pop("s/fabric/parked.0"), ("blocked", None))
        self.assertEqual(len(statuses), 6 + 4 + 1)
        self.assertEqual(set(statuses.values()), {("skip", "budget")})
        self.assertEqual(done.records[-1]["status"], "partial")
        self.assertFalse((done.out / "logs").exists())
        past = self.bench.run("s", "--deadline-epoch", str(time.time() - 1))
        self.assertEqual(past.case("s/posix/a1")["skip_reason"], "budget")
        ahead = self.bench.run("s", "--deadline-epoch", str(time.time() + 600))
        self.assertEqual(ahead.case("s/posix/a1")["status"], "pass")
        # A case is not started unless it can end, kill included, in time.
        near = self.bench.run("s", "--deadline-epoch", str(time.time() + 15))
        case = near.case("s/posix/a1")
        self.assertEqual((case["status"], case["skip_reason"]), ("skip", "budget"))
        self.assertEqual(case["error_excerpt"], "under 22 s left before the deadline")
        self.assertEqual(near.records[-1]["status"], "partial")

    def test_rounds_and_aa(self) -> None:
        done = self.bench.run(
            "s", "--sentinel-rounds", "2", "--aa", "--only", "s/posix/a*"
        )
        cases = [c for c in done.kind("case") if c["status"] == "pass"]
        self.assertEqual(len(cases), 4)
        found = [
            (c["case_key"], c["variant"], c["round"], c["order"]) for c in cases[:4]
        ]
        self.assertEqual(
            found,
            [
                ("s/posix/a1", "head", 0, 0),
                ("s/posix/a1", "aa", 0, 1),
                ("s/posix/a2", "head", 0, 2),
                ("s/posix/a1", "head", 1, 7),
            ],
        )
        self.assertEqual(len({c["case_uid"] for c in cases}), len(cases))
        self.assertEqual({c["status"] for c in cases[:4]}, {"pass"})
        self.assertEqual(cases[1]["log_ref"], "logs/001-s__posix__a1.aa")
        self.assertEqual(cases[3]["log_ref"], "logs/007-s__posix__a1.r1")
        self.assertEqual(len(done.of_case("sample", cases[1])), 1)
        heads = [
            c for c in done.kind("case") if (c["variant"], c["round"]) == ("head", 0)
        ]
        self.assertEqual(len(heads), 6, "one head round-0 record per selected case")

        default = self.bench.run("s", "--budget-s", "0")
        self.assertEqual(len(default.kind("case")), 6, "pr: one round and no A/A")
        nightly = self.bench.run("s", "--budget-s", "0", tier="nightly")
        self.assertEqual(len(nightly.kind("case")), 6 + 2 + 1, "three rounds and A/A")

    def test_probes(self) -> None:
        env = self.bench.env(provides=("gpu", "io_uring"), extra=PROBES)
        done = self.bench.run("s", env=env)
        gpu = done.case("s/posix/gpu")
        self.assertEqual(
            (gpu["status"], gpu["skip_reason"]), ("skip", "device-suspect")
        )
        self.assertEqual(done.case("s/posix/uring")["status"], "pass")
        tests = {record["test_id"]: record for record in done.kind("test")}
        self.assertEqual(list(tests), ["hip/start", "ring/s/posix/uring", "slow/end"])
        self.assertEqual(tests["hip/start"]["status"], "fail")
        self.assertEqual(tests["hip/start"]["error_excerpt"], "no device")
        self.assertEqual(tests["hip/start"]["suite"], "probe")
        self.assertEqual(tests["ring/s/posix/uring"]["status"], "pass")
        self.assertEqual(tests["ring/s/posix/uring"]["error_excerpt"], "")
        self.assertEqual(tests["slow/end"]["status"], "timeout")
        self.assertLess(tests["slow/end"]["duration_s"], 10)
        kinds = [record["kind"] for record in done.records]
        self.assertEqual(kinds[:2], ["run", "test"])
        self.assertEqual(kinds[-2:], ["test", "run"])
        uring = done.records.index(done.case("s/posix/uring"))
        after = [record["kind"] for record in done.records[uring + 1 : uring + 4]]
        self.assertEqual(after, ["sample", "counter", "test"])

    def test_a_probe_with_no_class_records_and_gates_nothing(self) -> None:
        probe = (
            '[[probe]]\nname = "cpu"\nwhen = "start"\nclasses = []\n'
            "cmd = \"echo 'guest CPU: GenuineIntel, family 6'; exit 1\"\n"
        )
        env = self.bench.env(provides=("gpu", "io_uring"), extra=probe)
        done = self.bench.run("s", env=env)
        self.assertEqual(done.code, 0, done.text)
        (test,) = done.kind("test")
        self.assertEqual((test["test_id"], test["status"]), ("cpu/start", "fail"))
        self.assertEqual(test["error_excerpt"], "guest CPU: GenuineIntel, family 6")
        self.assertIn("probe cpu fail: nothing now suspect", done.text)
        reasons = {case["skip_reason"] for case in done.kind("case")}
        self.assertNotIn("device-suspect", reasons)
        self.assertEqual(done.case("s/posix/gpu")["status"], "pass")

    def test_required_failure_exits_1(self) -> None:
        self.bench.lane(
            "r",
            storage("r", "ok", "fail").replace(
                "[case.axes]", "required = true\n[case.axes]"
            ),
        )
        done = self.bench.run("r")
        self.assertEqual(done.code, 1, done.text)
        self.assertTrue(done.case("r/posix/fail")["required"])
        self.assertEqual(done.records[-1]["status"], "complete")
        self.assertEqual(self.bench.run("r", "--only", "r/posix/ok").code, 0)
        self.assertEqual(self.bench.run("r", "--budget-s", "0").code, 1)

    def test_calibration_and_run_meta(self) -> None:
        meta = self.bench.root / "meta.json"
        fields = {
            "repo": "ROCm/nixl",
            "bench_run_id": "4242",
            "run_attempt": 2,
            "stamp": "20261119T010203Z",
            "rocm_version": "10.1.0",
            "branch": "main-amd",
            "features": ["for-upstream/x@abc"],
            "gpu_vram_limit": 1024,
            "nixlbench_sha256": "f" * 64,
            "tier": "pr",
        }
        self.assertLessEqual(set(fields), set(lib.META_KEYS))
        meta.write_text(json.dumps(fields))
        env = self.bench.env(tuning="calib_bytes = 1048576")
        done = self.bench.run(
            "s", "--only", "s/posix/a2", "--run-meta", str(meta), env=env
        )
        self.assertEqual(done.code, 0, done.text)
        first, last = done.records[0], done.records[-1]
        self.assertEqual({key: last[key] for key in fields}, fields)
        self.assertEqual(
            last["run_uid"], lib.run_uid("ROCm/nixl", "4242", 2, "s", "10.1.0")
        )
        self.assertGreater(first["calib_gbps_first"], 0)
        self.assertIsNone(first["calib_gbps_last"])
        self.assertGreater(last["calib_gbps_last"], 0)
        self.assertEqual(
            last["calib_gbps"], min(last["calib_gbps_first"], last["calib_gbps_last"])
        )
        self.assertEqual(first["calib_gbps_first"], last["calib_gbps_first"])
        self.assertEqual(last["env_fp"], lib.env_fingerprint(last))

    def test_invalid_inputs_exit_2_and_run_nothing(self) -> None:
        meta = self.bench.root / "meta.json"
        meta.write_text('{"rocm": "10.1.0"}')
        done = self.bench.run("s", "--run-meta", str(meta))
        self.assertEqual((done.code, done.records), (2, []))
        self.assertIn("unknown keys: rocm", done.text)
        # The collector reads the same file, and takes the tier from it.
        meta.write_text('{"tier": "nightly"}')
        done = self.bench.run("s", "--run-meta", str(meta))
        self.assertEqual((done.code, done.records), (2, []))
        self.assertIn("run-meta has tier 'nightly', --tier is pr", done.text)
        meta.write_text('{"run_attempt": "2"}')
        done = self.bench.run("s", "--run-meta", str(meta))
        if "is not checked" not in done.text:
            self.assertEqual((done.code, done.records), (2, []))

        bad = self.bench.env("bad", extra="[extra]\n")
        done = self.bench.run("s", env=bad)
        self.assertEqual((done.code, done.records), (2, []))
        self.assertIn("unknown key 'extra'", done.text)
        other = self.bench.env("other")
        other.write_text(
            other.read_text().replace('env_class = "test"', 'env_class = "x"')
        )
        done = self.bench.run("s", env=other)
        self.assertEqual(done.code, 2)
        self.assertIn("env_class is 'x' but lane s needs 'test'", done.text)
        self.assertEqual(self.bench.run("nolane").code, 2)

        good = self.bench.run("s", "--budget-s", "0")
        self.assertEqual(good.code, 0)
        again, text = self.bench.command(
            "run",
            "--matrix",
            str(self.bench.matrix),
            "--lane",
            "s",
            "--tier",
            "pr",
            "--env",
            str(self.bench.env()),
            "--out",
            str(good.out),
        )
        self.assertEqual(again, 2)
        self.assertIn("already holds a run", text)

    def test_launch_failures(self) -> None:
        self.bench.lane("f", storage("f", "ok"))
        done = self.bench.run(
            "f", env=self.bench.env("early", host='setup = ["exit 9"]')
        )
        case = done.case("f/posix/ok")
        self.assertEqual((case["status"], case["error_class"]), ("fail", "launch"))
        self.assertEqual(case["exit_init"], 9)
        # A setup line that fails is not passed over.
        setup = 'setup = ["true # fine", "cd /nonexistent-dir 2>/dev/null"]'
        done = self.bench.run("f", env=self.bench.env("setup", host=setup))
        case = done.case("f/posix/ok")
        self.assertEqual((case["status"], case["error_class"]), ("fail", "launch"))
        self.assertEqual(case["exit_init"], 125)
        self.assertEqual(case["error_excerpt"], "amd-bench: setup line 2 failed")
        gone = self.bench.env("gone")
        gone.write_text(gone.read_text().replace(str(FAKE), "/nonexistent/nixlbench"))
        case = self.bench.run("f", env=gone).case("f/posix/ok")
        self.assertEqual(
            (case["status"], case["error_class"]), ("fail", "exit-nonzero")
        )
        self.assertIn(case["exit_init"], (126, 127))
        self.assertIn("No such file", case["error_excerpt"])

    def test_a_signal_stops_the_run_in_order(self) -> None:
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number.name):
                self.interrupted(number)

    def interrupted(self, number: signal.Signals) -> None:
        name = f"h{number.value}"
        self.bench.lane(name, storage(name, "hang", "ok"))
        work = self.bench.root / "work"
        old = set(work.glob("*/*/init.pid"))
        sent = []

        def interrupt() -> None:
            end = time.monotonic() + 15
            while time.monotonic() < end:
                if set(work.glob("*/*/init.pid")) - old:
                    time.sleep(0.5)
                    # Only while the runner handles it: the default action
                    # would end the whole test process.
                    sent.append(number)
                    os.kill(os.getpid(), number)
                    return
                time.sleep(0.05)

        before = signal.getsignal(number)
        thread = threading.Thread(target=interrupt)
        thread.start()
        done = self.bench.run(name)
        thread.join()
        self.assertEqual(sent, [number], f"the case never started\n{done.text}")
        self.assertIs(signal.getsignal(number), before)
        self.assertEqual(done.code, 0, done.text)
        hang, rest = done.case(f"{name}/posix/hang"), done.case(f"{name}/posix/ok")
        self.assertEqual((hang["status"], hang["skip_reason"]), ("skip", "aborted"))
        self.assertIn(number.name, hang["error_excerpt"])
        self.assertIn(hang["exit_init"], (1, 124))
        self.assertLess(hang["duration_s"], 15)
        self.assertEqual((rest["status"], rest["skip_reason"]), ("skip", "aborted"))
        self.assertIsNone(rest["order"])
        self.assertEqual(done.records[-1]["kind"], "run")
        self.assertEqual(done.records[-1]["status"], "partial")
        (pid_file,) = set(work.glob("*/*/init.pid")) - old
        with self.assertRaises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    def test_output_that_cannot_be_written(self) -> None:
        self.bench.lane("o", storage("o", "ok", "fail"))
        out = self.bench.root / "taken"
        out.write_text("a file, not a directory")
        target = ["--lane", "o", "--tier", "pr", "--env", str(self.bench.env())]
        code, text = self.bench.command(
            "run", "--matrix", str(self.bench.matrix), *target, "--out", str(out)
        )
        self.assertEqual(code, 2, text)
        self.assertIn("cannot write to --out", text)
        # A summary that cannot be written does not undo a run that ended.
        done = self.bench.run("o", "--summary-out", str(self.bench.root))
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("cannot write the summary", done.text)
        self.assertEqual(done.records[-1]["status"], "complete")

    def test_an_error_inside_one_case_does_not_end_the_lane(self) -> None:
        self.bench.lane("i", storage("i", "ok", "fail", "short"))
        plan_case = run.plan_case

        def faulty(case: lib.Case, *args: Any, **kwargs: Any) -> run.Plan:
            if case.case_key == "i/posix/ok":
                raise RuntimeError("boom AWS_SECRET_ACCESS_KEY=hunter2")
            return plan_case(case, *args, **kwargs)

        with mock.patch.object(run, "plan_case", faulty):
            done = self.bench.run("i")
        self.assertEqual(done.code, 0, done.text)
        case = done.case("i/posix/ok")
        self.assertEqual((case["status"], case["error_class"]), ("fail", "internal"))
        self.assertEqual(
            case["error_excerpt"], "RuntimeError: boom AWS_SECRET_ACCESS_KEY=***"
        )
        self.assertEqual(done.case("i/posix/fail")["error_class"], "exit-nonzero")
        self.assertEqual(done.case("i/posix/short")["error_class"], "rows")
        self.assertEqual(done.records[-1]["status"], "complete")
        schema = lib.load_schema()
        for record in done.records:
            self.assertEqual(lib.validate_record(record, schema), [], record)

        # The same when it is the log directory that cannot be made.
        out = self.bench.root / f"out{self.bench.runs + 1}"
        out.mkdir()
        (out / "logs").write_text("in the way")
        done = self.bench.run("i")
        self.assertEqual(done.code, 0, done.text)
        classes = [case["error_class"] for case in done.kind("case")]
        self.assertEqual(classes, ["internal"] * 3)
        self.assertEqual(done.records[-1]["kind"], "run")


SSH_STUB = """#!/bin/bash
# Stand-in for ssh in the tests: drops the options, runs the command here
# with a home directory of its own per host.
while [ $# -gt 0 ]; do
    case "$1" in -F|-o) shift 2 ;; *) break ;; esac
done
host=$1; shift
if [ "$host" = down ]; then
    echo "ssh: connect to host down port 22: No route to host" >&2
    exit 255
fi
# A guest that froze: the connection opens and nothing ever answers.
if [ -e "$STUB_HOMES/frozen-$host" ]; then
    exec sleep 60
fi
export HOME="$STUB_HOMES/$host"
mkdir -p "$HOME"
exec "$@"
"""
SSH_ENV = """schema = 1
env_class = "test"
provides = ["two_hosts"]
[roles]
init = "guest1"
tgt = "{tgt}"
[hosts.guest1]
kind = "ssh"
ssh_config = "/dev/null"
ssh_host = "one"
work_dir = "bench/run"
nixlbench = "{fake}"
{setup}
[hosts.guest1.vars]
ip = "127.0.0.1"
ucx_net_devices = "lo"
[hosts.guest2]
kind = "ssh"
ssh_host = "two"
work_dir = "bench/run"
nixlbench = "{fake}"
[hosts.gone]
kind = "ssh"
ssh_host = "down"
work_dir = "bench/run"
[vars]
asio_port_base = {port}
posix_dir = "{root}/posix"
[tuning]
kill_after_s = 2
pair_skew_s = 0.05
listen_wait_s = 5
peer_grace_s = 1
calibrate = false
"""
SSH_LANE = (
    storage("v", "ok")
    + """
[[case]]
key = "v/ucx/tcp"
workload = "ucx/tcp"
backend = "UCX"
transport = "tcp"
op = "WRITE"
"""
)


class SshHostTest(unittest.TestCase):
    """The ssh host kind, with a stub in place of the ssh client."""

    def setUp(self) -> None:
        self.bench = Bench(self)
        self.bench.lane("v", SSH_LANE, topology="two-host")
        stub = self.bench.root / "bin" / "ssh"
        stub.parent.mkdir()
        stub.write_text(SSH_STUB)
        stub.chmod(0o755)
        path = f"{stub.parent}{os.pathsep}{os.environ['PATH']}"
        homes = str(self.bench.root / "homes")
        patcher = mock.patch.dict(os.environ, {"PATH": path, "STUB_HOMES": homes})
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, tgt: str, setup: str = "") -> pathlib.Path:
        path = self.bench.root / f"ssh-{tgt}.toml"
        path.write_text(
            SSH_ENV.format(
                tgt=tgt,
                fake=FAKE,
                port=free_port(),
                root=self.bench.root,
                setup=setup,
            )
        )
        return path

    def frozen(self, setup: str) -> Result:
        """A run in which guest1 stops answering while the first case runs."""
        self.bench.lane(
            "w",
            storage("w", "ok", "fail", "short").replace(
                "[case.axes]", "timeout_s = 2\n[case.axes]"
            ),
        )
        waits: dict[str, Any] = dict.fromkeys(
            ("QUERY_S", "HOST_CHECK_S", "STOP_S", "SLACK_S"), 1.0
        )
        started = time.monotonic()
        with mock.patch.multiple(run, **waits):
            done = self.bench.run("w", env=self.env("guest1", f"setup = {setup}"))
        self.assertLess(time.monotonic() - started, 30)
        self.assertEqual(done.code, 0, done.text)
        self.assertIn("host guest1 stopped answering", done.text)
        for key in ("w/posix/fail", "w/posix/short"):
            case = done.case(key)
            self.assertEqual((case["status"], case["skip_reason"]), ("skip", "env"))
            self.assertIn("host guest1 stopped answering", case["error_excerpt"])
            self.assertIsNone(case["order"])
        self.assertEqual(done.records[-1]["kind"], "run")
        self.assertEqual(done.records[-1]["status"], "complete")
        return done

    def test_host_that_freezes_after_a_case(self) -> None:
        done = self.frozen("""['touch "$STUB_HOMES/frozen-one"']""")
        case = done.case("w/posix/ok")
        self.assertEqual(case["status"], "pass")
        self.assertIsInstance(case["load1_before"], float)
        self.assertIsNone(case["load1_after"])

    def test_host_that_freezes_under_a_case(self) -> None:
        done = self.frozen("""['touch "$STUB_HOMES/frozen-one"', "sleep 20"]""")
        case = done.case("w/posix/ok")
        self.assertEqual((case["status"], case["error_class"]), ("fail", "ssh"))
        # The limit of the case, one stop that nothing answers, one check.
        self.assertLess(case["duration_s"], 12)

    def test_pair_across_two_hosts(self) -> None:
        done = self.bench.run("v", env=self.env("guest2"))
        self.assertEqual(done.code, 0, done.text)
        pair = done.case("v/ucx/tcp")
        self.assertEqual((pair["status"], pair["error_excerpt"]), ("pass", ""))
        self.assertEqual((pair["exit_init"], pair["exit_tgt"]), (0, 0))
        self.assertEqual(pair["transport_obs"], "tcp")
        homes = self.bench.root / "homes"
        run_uid = done.records[0]["run_uid"]
        directory = f"bench/run/{run_uid}/001-v__ucx__tcp"
        self.assertIn(
            f"UCX_LOG_FILE={homes}/one/{directory}/init.ucx.log UCX_NET_DEVICES=lo ",
            pair["cmdline"],
        )
        self.assertTrue((homes / "two" / directory / "tgt.ucx.log").is_file())
        self.assertFalse((homes / "two" / directory / "init.pid").exists())
        self.assertIn("ucp_get", done.log(pair, "tgt.ucx.log"))
        self.assertEqual(done.case("v/posix/ok")["status"], "pass")
        last = done.records[-1]
        self.assertRegex(last["guest_kernel"], r"^\d+\.\d+")
        self.assertGreaterEqual(last["guest_vcpus"], 1)
        self.assertGreater(last["guest_mem"], 1 << 20)

    def test_unreachable_host(self) -> None:
        done = self.bench.run("v", env=self.env("gone"))
        self.assertEqual(done.code, 0, done.text)
        pair = done.case("v/ucx/tcp")
        self.assertEqual((pair["status"], pair["skip_reason"]), ("skip", "env"))
        self.assertEqual(
            pair["error_excerpt"],
            "host gone is unreachable: "
            "ssh: connect to host down port 22: No route to host",
        )
        self.assertEqual(done.case("v/posix/ok")["status"], "pass")
        self.assertIn("host gone is unreachable", done.text)


class CommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.bench = Bench(self)
        self.bench.lane("s", SCHEDULING, aa_case="s/posix/a1")
        self.matrix = ["--matrix", str(self.bench.matrix)]

    def test_validate(self) -> None:
        self.assertEqual(self.bench.command("validate", *self.matrix), (0, "ok\n"))
        env = self.bench.env()
        code, text = self.bench.command("validate", *self.matrix, "--env", str(env))
        self.assertEqual(code, 2)
        self.assertEqual(
            text,
            f"{env}: s/posix/nowhere: unresolved {{nope_dir}}, {{self.also_nope}}\n",
        )

        self.bench.lane(
            "z", storage("z", "ok").replace("[case.axes]", "threads = 3\n[case.axes]")
        )
        code, text = self.bench.command("validate", *self.matrix)
        self.assertEqual(code, 2)
        lines = text.splitlines()
        self.assertEqual(len(lines), 4, text)
        for line in lines:
            self.assertTrue(line.startswith("lanes/z.toml: z/posix/ok: "), line)
        self.assertIn("num_iter (64) is not a multiple", text)
        code, text = self.bench.command("validate", *self.matrix, "--lane", "s")
        self.assertEqual((code, text), (0, "ok\n"))

        self.bench.lane("z", storage("z", "ok") + "\n[[case]]\nkey = 3\nbogus = 1\n")
        code, text = self.bench.command("validate", *self.matrix)
        self.assertEqual(code, 2)
        self.assertIn("lanes/z.toml: ", text)
        out = self.bench.root / "o"
        target = ["--lane", "s", "--tier", "pr", "--env", str(env), "--out", str(out)]
        code, text = self.bench.command("run", *self.matrix, *target)
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_list(self) -> None:
        code, text = self.bench.command("list", *self.matrix, "--tier", "pr")
        self.assertEqual(code, 0)
        lines = text.splitlines()
        self.assertEqual(len(lines), 6)
        self.assertEqual(lines[0], "  0  s/posix/a1  pr  info  active  S  1")
        self.assertEqual(lines[5], "  5  s/fabric/parked  pr  info  blocked  S  1")
        code, text = self.bench.command("list", *self.matrix)
        self.assertIn("  1  s/posix/a1 (aa round 0)  pr  info  active  S  1", text)
        self.assertIn("s/posix/weekly  weekly  info  active  S  1", text)
        self.assertIn("s/posix/a1 (head round 4)", text)

    def test_list_commands(self) -> None:
        env = str(self.bench.env())
        code, text = self.bench.command("list", *self.matrix, "--commands")
        self.assertEqual((code, text), (2, "--commands and --format json need --env\n"))
        code, text = self.bench.command("list", *self.matrix, "--format", "json")
        self.assertEqual(code, 2)
        code, text = self.bench.command(
            "list",
            *self.matrix,
            "--lane",
            "s",
            "--tier",
            "pr",
            "--env",
            env,
            "--commands",
        )
        self.assertEqual(code, 0)
        self.assertIn(f"     init@here: {FAKE} --backend=POSIX --op_type=WRITE ", text)
        self.assertIn("     # unresolved {nope_dir}, {self.also_nope}", text)
        code, text = self.bench.command(
            "list",
            *self.matrix,
            "--tier",
            "pr",
            "--env",
            env,
            "--format",
            "json",
        )
        self.assertEqual(code, 0)
        listing = json.loads(text)
        self.assertEqual(len(listing), 5)
        first = listing[0]
        self.assertEqual(first["case_key"], "s/posix/a1")
        self.assertEqual((first["order"], first["host"], first["env"]), (0, "here", {}))
        self.assertEqual(first["argv"][:2], [str(FAKE), "--backend=POSIX"])
        self.assertLessEqual({"case_key", "order", "argv", "env", "host"}, set(first))


if __name__ == "__main__":
    unittest.main()
