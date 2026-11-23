#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""nixlbench runner of the AMD bench lanes: one lane and tier in, records out.

    amd_bench_run.py validate [--matrix DIR] [--lane LANE] [--env FILE]
    amd_bench_run.py list [--matrix DIR] [--lane LANE] [--tier TIER]
        [--env FILE] [--commands] [--format text|json]
    amd_bench_run.py run [--matrix DIR] --lane LANE --tier TIER --env FILE
        --out DIR [--run-meta FILE] [--only GLOB]... [--deadline-epoch N |
        --budget-s N] [--sentinel-rounds N] [--aa | --no-aa] [--summary-out FILE]

'run' writes DIR/records.ndjson (schema v1: run, case, sample, counter and
test records) and DIR/logs/<case>/. It exits 0 when no required case failed,
1 when one did, 2 when the matrix, env, run-meta or DIR cannot be used and
nothing was run, and 3 on an internal error. Every selected case gets a
record, whatever happens to it. Needs amd_bench_lib.py beside it and the
standard library.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import math
import os
import pathlib
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import tomllib
import traceback
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Iterable, Mapping, Optional, Sequence

try:
    import amd_bench_lib as lib
except ImportError:
    sys.exit("amd_bench_lib.py must sit beside this script")

RDMA_TRANSPORTS = ("rc", "ud", "verbs")
UCX_TLS_TRANSPORTS = ("tcp", "rc", "ud", "shm")
NETWORK_FAMILIES = ("rc", "ud", "dc", "tcp")
FILE_BACKENDS = ("POSIX", "AIS")
# nixlbench refuses any argument that contains one of these, values included.
DISABLED_TOKENS = ("flagfile", "fromenv", "tryfromenv")
VRAM_BUFFER_CAP = 64 << 20
MESH_BLOCK_CAP = 16 << 20
HEADER_10 = (
    "Block Size (B)",
    "Batch Size",
    "B/W (GB/Sec)",
    "Avg Lat. (us)",
    "Avg Prep (us)",
    "P99 Prep (us)",
    "Avg Post (us)",
    "P99 Post (us)",
    "Avg Tx (us)",
    "P99 Tx (us)",
)
HEADER_12 = (
    HEADER_10[:3] + ("Aggregate B/W (GB/Sec)", "Network Util (%)") + HEADER_10[3:]
)
KEYS_10 = (
    "block_size",
    "batch_size",
    "bw_gbps",
    "lat_avg_us",
    "prep_avg_us",
    "prep_p99_us",
    "post_avg_us",
    "post_p99_us",
    "tx_avg_us",
    "tx_p99_us",
)
KEYS_12 = KEYS_10[:3] + ("agg_bw_gbps", "net_util_pct") + KEYS_10[3:]
ECHO_FLAGS = (
    "backend",
    "op_type",
    "initiator_seg_type",
    "target_seg_type",
    "scheme",
    "num_threads",
    "total_buffer_size",
    "start_block_size",
    "max_block_size",
    "start_batch_size",
    "max_batch_size",
    "num_iter",
    "warmup_iter",
    "large_blk_iter_ftr",
    "pipeline_depth",
    "posix_api_type",
    "storage_enable_direct",
    "num_files",
)
SENTINEL_ROUNDS = {"pr": 1, "nightly": 3, "weekly": 5}
LISTEN_MARK = "for ASIO runtime"
CONNECT_LINE = "ASIO runtime bind/listen error -- using connect() instead"
CONSISTENCY_LINE = "Consistency check failed"
ULIMIT_NOTE = "amd-bench: ulimit -l unlimited refused"
MARKER_RE = re.compile(r"amd-bench: exit rc=(\d+)")
# What nixlbench says when its ASIO runtime gives up: mostly the peer's death.
ASIO_ABORT_RE = re.compile(r"^(?:\s*what\(\):\s+)?ASIO \w", re.MULTILINE)
# bash's report of a job that a signal ended, which repeats the whole command.
JOB_LINE_RE = re.compile(r"^(bash: line \d+:\s*\d+\s.*?)\s+timeout --signal=INT .*$")
PLACEHOLDER_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_.]*)\}")
CONFIG_RE = re.compile(r"^.*?\(--([a-z0-9_]+)[=)].*?:(?: (.*))?$")
NUMBER_RE = re.compile(r"-?(\d+(\.\d+)?|inf|nan)")
UCX_COMPLAINT_RE = re.compile(r"UCX\s+(WARN|ERROR|FATAL)")
PLAIN_TOKEN_RE = re.compile(r"[\w@%+=:,./*-]+")
LOG_CAP = 16 << 20
HUGEPAGE = 2 << 20
SECRET_MIN = 6
# ssh gives up on a host that does not answer, instead of hanging on it.
SSH_CONNECT_S = 15
HOST_CHECK_S = 20.0
# The longest a host gets for a small question, or to pass on a Ctrl-C.
QUERY_S = 30.0
STOP_S = 15.0
# What the runner waits beyond a limit the host enforces, before it steps in.
SLACK_S = 30.0
MARKER_GRACE_S = 5.0
CALIBRATION = (
    "python3 -I -c 'import time;n={bytes};a=bytearray(n);b=bytearray(n);"
    "t=time.perf_counter()\nfor _ in range(4): b[:]=a\n"
    "print(4*n/(time.perf_counter()-t)/1e9)'"
)


class EnvError(lib.BenchError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


class TableError(Exception):
    pass


def note(message: str) -> None:
    sys.stderr.write(f"amd_bench_run.py: {message}\n")


def utc(seconds: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(seconds))


# --- stdout and UCX log parsers -------------------------------------------


@dataclass(frozen=True)
class Table:
    columns: tuple[str, ...]
    rows: tuple[dict[str, int | float | None], ...]
    config: dict[str, str]
    noise_lines: int


def _cell(token: str) -> float | None:
    # A number too long for a double reads as infinity, which JSON lacks.
    value = float(token)
    return value if math.isfinite(value) else None


def parse_stdout(text: str) -> Table:
    """The config echo and the result table of one nixlbench process."""
    columns: tuple[str, ...] = ()
    rows: list[dict[str, int | float | None]] = []
    config: dict[str, str] = {}
    noise = 0
    for line in text.splitlines():
        if not columns:
            if line.startswith("Block Size"):
                names = tuple(re.split(r"\s{2,}", line.strip()))
                if names not in (HEADER_10, HEADER_12):
                    raise TableError(f"unknown table header: {line.strip()}")
                columns = KEYS_10 if names == HEADER_10 else KEYS_12
            else:
                echo = CONFIG_RE.match(line)
                if echo:
                    config.setdefault(echo.group(1), (echo.group(2) or "").strip())
            continue
        tokens = line.split()
        if not tokens:
            continue
        if not tokens[0].isascii() or not tokens[0].isdigit():
            noise += 1
            continue
        if (
            len(tokens) != len(columns)
            or not tokens[1].isdigit()
            or not all(NUMBER_RE.fullmatch(token) for token in tokens)
        ):
            raise TableError(f"malformed table row: {line.strip()}")
        row: dict[str, int | float | None] = {
            "block_size": int(tokens[0]),
            "batch_size": int(tokens[1]),
        }
        row.update(zip(columns[2:], map(_cell, tokens[2:])))
        rows.append(row)
    return Table(columns, tuple(rows), config, noise)


def _ucx_lanes(text: str) -> dict[tuple[str, str], list[str]]:
    """{(endpoint kind, operation): [transport/device, ...]} from UCX_PROTO_INFO."""
    lanes: dict[tuple[str, str], list[str]] = {}
    kind: Optional[str] = None
    operation: Optional[str] = None
    for line in text.splitlines():
        row = re.sub(r"^\[[^]]*\] \[[^]]*\]\s*", "", line)
        title = re.match(r"\| \S+ (self|intra-node|inter-node) cfg#\d+", row)
        if title:
            kind, operation = title.group(1), None
        elif kind and operation is None and row.startswith("| ") and "ucp_" in row:
            operation = row.strip("| ").strip()
        elif kind and operation and row.startswith("|") and row.count("|") == 4:
            found = lanes.setdefault((kind, operation), [])
            for lane in re.findall(r"[a-z0-9_]+/[A-Za-z0-9_:.-]+", row.split("|")[3]):
                if lane not in found:
                    found.append(lane)
    return lanes


def _family(lane: str) -> str:
    transport = lane.split("/", 1)[0]
    for name in NETWORK_FAMILIES:
        if transport == name or transport.startswith(name + "_"):
            return name
    return transport


def parse_ucx_log(text: str) -> str | None:
    """Transport families UCX selected for ucp_put and ucp_get towards the peer."""
    families = {
        _family(lane)
        for (kind, operation), found in _ucx_lanes(text).items()
        if kind != "self" and ("ucp_put" in operation or "ucp_get" in operation)
        for lane in found
    }
    return "+".join(sorted(families)) or None


# --- load-time rules ------------------------------------------------------


def check_case(case: lib.Case, profile: lib.Profile, lane: lib.Lane) -> list[str]:
    """Why nixlbench or this runner would refuse the case; empty when it runs."""
    return flag_problems(case, lib.effective_flags(case, profile), lane.env_class)


def flag_problems(
    case: lib.Case, flags: Mapping[str, lib.FlagValue], env_class: str
) -> list[str]:
    """check_case on flags given: the template ones, or those of one launch."""
    problems: list[str] = []

    def number(name: str, default: int) -> int | None:
        value = flags.get(name, default)
        # A placeholder is only known at run time.
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def text(name: str, default: str) -> str:
        return str(flags.get(name, default))

    threads = case.threads
    storage = case.backend not in lib.PAIR_BACKENDS
    vram = "VRAM" in (case.init_mem, case.tgt_mem)
    total = number("total_buffer_size", 0)
    initiators = number("num_initiator_dev", 1)
    targets = number("num_target_dev", 1)
    start_block = number("start_block_size", 4096)
    max_block = number("max_block_size", 64 << 20)
    start_batch = number("start_batch_size", 1)
    max_batch = number("max_batch_size", 1)

    if "total_buffer_size" not in flags:
        problems.append("total_buffer_size is not set and the 8 GiB default is too big")
    elif total is not None and total < 1:
        problems.append("total_buffer_size must be positive")
    devices = (("num_initiator_dev", initiators), ("num_target_dev", targets))
    for name, count in devices:
        if count is not None and count < 1:
            problems.append(f"{name} must be at least 1")
    largest = max_block * max_batch if max_block and max_batch else 0
    if total and total > 0:
        for name, count in devices + (("threads", threads),):
            if count and count > 0 and largest > total // count:
                problems.append(
                    f"max_block_size x max_batch_size ({largest}) exceeds "
                    f"total_buffer_size / {name} ({total // count})"
                )
        for name, count in devices:
            if count and count > 0 and total % (count * threads):
                problems.append(
                    f"total_buffer_size is not divisible by {name} x threads "
                    f"({count * threads})"
                )

    factor = number("large_blk_iter_ftr", 16)
    num_iter, warmup = number("num_iter", 1000), number("warmup_iter", 100)
    if num_iter is not None and num_iter < 1:
        problems.append("num_iter must be at least 1")
    if factor is not None and factor <= 0:
        problems.append("large_blk_iter_ftr must be greater than 0")
    elif factor is not None:
        for name, value in (("num_iter", num_iter), ("warmup_iter", warmup)):
            if value is not None and value % (threads * factor):
                problems.append(
                    f"{name} ({value}) is not a multiple of threads x "
                    f"large_blk_iter_ftr ({threads * factor}); nixlbench would "
                    "round it up"
                )

    for name, first, last in (
        ("block", start_block, max_block),
        ("batch", start_batch, max_batch),
    ):
        if first is None or last is None:
            continue
        if first < 1 or last < 1:
            problems.append(f"start_{name}_size and max_{name}_size must be positive")
        elif first > last:
            problems.append(f"start_{name}_size exceeds max_{name}_size")

    depth = number("pipeline_depth", 1)
    if depth is not None and depth < 1:
        problems.append("pipeline_depth must be at least 1")
    if case.op == "QUERY":
        if depth != 1:
            problems.append("QUERY requires pipeline_depth 1")
        if not storage:
            problems.append("QUERY is for storage backends only")

    if case.backend == "POSIX":
        if text("posix_api_type", "AIO") not in ("AIO", "URING", "POSIXAIO"):
            problems.append("posix_api_type must be AIO, URING or POSIXAIO")
        if case.init_mem != "DRAM":
            problems.append("POSIX needs init_mem DRAM")
    if case.backend == "AIS" and case.init_mem != "VRAM":
        problems.append("AIS needs init_mem VRAM")
    if case.backend == "OBJ":
        if text("obj_scheme", "http") not in ("http", "https"):
            problems.append("obj_scheme must be http or https")
        if text("obj_req_checksum", "supported") not in ("supported", "required"):
            problems.append("obj_req_checksum must be supported or required")

    randomize = text("randomize_location_mode", "none")
    if randomize not in ("none", "blockaligned", "bytealigned"):
        problems.append(
            "randomize_location_mode must be none, blockaligned or bytealigned"
        )
    elif randomize != "none" and not storage:
        problems.append("randomize_location_mode is for storage backends only")
    elif randomize == "bytealigned":
        if case.direct:
            problems.append("bytealigned randomisation excludes direct I/O")
        if flags.get("check_consistency") is True:
            problems.append("bytealigned randomisation excludes check_consistency")

    if flags.get("prepared_xfer") is True and flags.get("reregister_mem") is True:
        problems.append("prepared_xfer is incompatible with reregister_mem")

    files = number("num_files", 1)
    if case.backend in FILE_BACKENDS and files is not None and initiators:
        buffers = threads * initiators
        if files < 1 or files > buffers or buffers % files:
            problems.append(
                f"num_files ({files}) must divide, and not exceed, threads x "
                f"num_initiator_dev ({buffers})"
            )

    if case.runtime == "ASIO" and None not in (initiators, targets):
        assert initiators is not None and targets is not None
        if initiators + targets != 2:
            problems.append("ASIO supports exactly one initiator and one target device")

    for name in lib.flag_order(flags):
        token = f"--{name}={render(flags[name])}"
        banned = [word for word in DISABLED_TOKENS if word in token]
        if banned:
            problems.append(f"flag {name} contains '{banned[0]}', which nixlbench bans")

    if case.backend == "GUSLI" and (max_batch, depth) != (1, 1):
        # nixlbench indexes the device list by the position in the batch.
        if max_batch is not None and depth is not None:
            problems.append("GUSLI needs max_batch_size 1 and pipeline_depth 1")
    if flags.get("use_hugepages") is True and total is not None and total % HUGEPAGE:
        problems.append(
            "use_hugepages needs total_buffer_size in multiples of 2097152; "
            "nixlbench would round it up"
        )

    if env_class.startswith("gha-kvm") and threads > 2:
        problems.append("VM lanes run at most 2 threads")
    if vram and total is not None and total > VRAM_BUFFER_CAP:
        problems.append("a VRAM case needs total_buffer_size of at most 67108864")
    if case.transport in RDMA_TRANSPORTS and max_block is not None:
        if max_block >= MESH_BLOCK_CAP:
            problems.append("an RDMA case needs max_block_size below 16777216")
    if case.status == "active":
        if case.scheme != "pairwise":
            problems.append(f"scheme {case.scheme} is not runnable by runner v1")
        if case.runtime == "ETCD":
            problems.append("runtime ETCD is not runnable by runner v1")
        if case.backend == "REDIS" and case.op == "READ":
            # One value of max_block_size is seeded per key, and a GET of
            # any other size fails: such a sweep is authored blocked.
            if None not in (start_block, max_block) and start_block != max_block:
                problems.append(
                    "a REDIS READ needs start_block_size equal to max_block_size"
                )
    return problems


# --- env file -------------------------------------------------------------


@dataclass(frozen=True)
class HostSpec:
    name: str
    kind: str
    ssh_config: str
    ssh_host: str
    work_dir: str
    nixlbench: str
    setup: tuple[str, ...]
    vars: dict[str, str]
    env: dict[str, str]


@dataclass(frozen=True)
class Probe:
    name: str
    host: str
    when: str
    classes: tuple[str, ...]
    cmd: str
    timeout_s: int


@dataclass(frozen=True)
class Tuning:
    kill_after_s: int = 30
    pair_skew_s: float = 1.0
    listen_wait_s: float = 20
    peer_grace_s: float = 20
    calibrate: bool = True
    calib_bytes: int = 268435456


@dataclass(frozen=True)
class Env:
    env_class: str
    provides: tuple[str, ...]
    roles: dict[str, str]
    hosts: dict[str, HostSpec]
    vars: dict[str, str]
    asio_port_base: int
    tuning: Tuning
    probes: tuple[Probe, ...]


def render(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class _EnvReader:
    def __init__(self) -> None:
        self.problems: list[str] = []

    def table(
        self, value: Any, where: str, keys: Iterable[str] | None
    ) -> dict[str, Any]:
        if not isinstance(value, dict):
            self.problems.append(f"{where} must be a table")
            return {}
        if keys is not None:
            for name in sorted(set(value) - set(keys)):
                self.problems.append(f"{where}: unknown key '{name}'")
        return value

    def get(
        self, table: Mapping[str, Any], where: str, key: str, kind: Any, default: Any
    ) -> Any:
        value = table.get(key, default)
        if isinstance(value, bool) and kind is not bool or not isinstance(value, kind):
            if value is None:
                self.problems.append(f"{where}: '{key}' is required")
            else:
                self.problems.append(f"{where}: '{key}' has the wrong type")
            return default
        return value

    def strings(
        self, table: Mapping[str, Any], where: str, key: str
    ) -> tuple[str, ...]:
        value = table.get(key, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            self.problems.append(f"{where}: '{key}' must be a list of strings")
            return ()
        return tuple(value)

    def scalars(self, value: Any, where: str) -> dict[str, str]:
        table = self.table(value, where, None)
        for name, item in table.items():
            if not isinstance(item, (str, int, float)):
                self.problems.append(f"{where}: '{name}' must be a string or a number")
        return {name: render(item) for name, item in table.items()}

    def host(self, name: str, raw: Any) -> HostSpec:
        where = f"[hosts.{name}]"
        table = self.table(
            raw,
            where,
            (
                "kind",
                "ssh_config",
                "ssh_host",
                "work_dir",
                "nixlbench",
                "setup",
                "vars",
                "env",
            ),
        )
        kind = self.get(table, where, "kind", str, None)
        if kind not in (None, "local", "ssh"):
            self.problems.append(f"{where}: 'kind' must be local or ssh")
        if kind == "ssh" and not table.get("ssh_host"):
            self.problems.append(f"{where}: an ssh host needs 'ssh_host'")
        environ = self.scalars(table.get("env", {}), f"[hosts.{name}.env]")
        for key in environ:
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
                self.problems.append(
                    f"[hosts.{name}.env]: '{key}' is not a variable name"
                )
        return HostSpec(
            name,
            kind or "local",
            self.get(table, where, "ssh_config", str, ""),
            self.get(table, where, "ssh_host", str, ""),
            self.get(table, where, "work_dir", str, None) or "",
            self.get(table, where, "nixlbench", str, "nixlbench"),
            self.strings(table, where, "setup"),
            self.scalars(table.get("vars", {}), f"[hosts.{name}.vars]"),
            environ,
        )

    def probe(self, number: int, raw: Any, known: Iterable[str]) -> Probe:
        where = f"[[probe]] {number}"
        table = self.table(
            raw, where, ("name", "host", "when", "classes", "cmd", "timeout_s")
        )
        probe = Probe(
            self.get(table, where, "name", str, None) or "",
            self.get(table, where, "host", str, "init"),
            self.get(table, where, "when", str, None) or "",
            self.strings(table, where, "classes"),
            self.get(table, where, "cmd", str, None) or "",
            self.get(table, where, "timeout_s", int, 30),
        )
        if probe.when not in ("start", "between", "end"):
            self.problems.append(f"{where}: 'when' must be start, between or end")
        if probe.host not in known:
            self.problems.append(f"{where}: no role or host '{probe.host}'")
        for name in sorted(set(probe.classes) - set(lib.NEEDS)):
            self.problems.append(f"{where}: unknown class '{name}'")
        if probe.timeout_s < 1:
            self.problems.append(f"{where}: 'timeout_s' must be positive")
        return probe


def load_env(path: pathlib.Path) -> Env:
    """The env file at path, or EnvError listing every problem."""
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
        data = json.loads(text) if path.suffix == ".json" else tomllib.loads(text)
    except (OSError, ValueError) as exc:
        raise EnvError([f"cannot read env {path}: {exc}"]) from exc
    reader = _EnvReader()
    top = reader.table(
        data,
        "env",
        (
            "schema",
            "env_class",
            "provides",
            "roles",
            "hosts",
            "vars",
            "tuning",
            "probe",
        ),
    )
    if top.get("schema") != 1:
        reader.problems.append("env: 'schema' must be 1")
    env_class = reader.get(top, "env", "env_class", str, None) or ""
    provides = reader.strings(top, "env", "provides")
    for name in sorted(set(provides) - set(lib.NEEDS)):
        reader.problems.append(f"env: 'provides' has the unknown need '{name}'")
    hosts = {
        name: reader.host(name, raw)
        for name, raw in reader.table(top.get("hosts", {}), "[hosts]", None).items()
    }
    roles = reader.table(top.get("roles", {}), "[roles]", ("init", "tgt"))
    if "init" not in roles:
        reader.problems.append("[roles]: 'init' is required")
    for role, name in roles.items():
        if name not in hosts:
            reader.problems.append(f"[roles]: {role} names the unknown host '{name}'")
    variables = reader.table(top.get("vars", {}), "[vars]", None)
    port_base = reader.get(variables, "[vars]", "asio_port_base", int, 21000)
    if not 1024 <= port_base <= 64535:
        reader.problems.append("[vars]: 'asio_port_base' must be 1024 to 64535")
    names = ("kill_after_s", "pair_skew_s", "listen_wait_s", "peer_grace_s")
    tuned = reader.table(
        top.get("tuning", {}), "[tuning]", names + ("calibrate", "calib_bytes")
    )
    defaults = Tuning()
    tuning = Tuning(
        reader.get(tuned, "[tuning]", "kill_after_s", int, defaults.kill_after_s),
        reader.get(
            tuned, "[tuning]", "pair_skew_s", (int, float), defaults.pair_skew_s
        ),
        reader.get(
            tuned, "[tuning]", "listen_wait_s", (int, float), defaults.listen_wait_s
        ),
        reader.get(
            tuned, "[tuning]", "peer_grace_s", (int, float), defaults.peer_grace_s
        ),
        reader.get(tuned, "[tuning]", "calibrate", bool, defaults.calibrate),
        reader.get(tuned, "[tuning]", "calib_bytes", int, defaults.calib_bytes),
    )
    if tuning.kill_after_s < 1 or tuning.calib_bytes < 1:
        reader.problems.append(
            "[tuning]: 'kill_after_s' and 'calib_bytes' must be positive"
        )
    if min(tuning.pair_skew_s, tuning.listen_wait_s, tuning.peer_grace_s) < 0:
        reader.problems.append("[tuning]: the waits must not be negative")
    raw_probes = top.get("probe", [])
    if not isinstance(raw_probes, list):
        reader.problems.append("probe must be an array of tables")
        raw_probes = []
    known = set(roles) | set(hosts)
    probes = tuple(
        reader.probe(number, raw, known) for number, raw in enumerate(raw_probes, 1)
    )
    if reader.problems:
        raise EnvError([f"{path}: {text}" for text in reader.problems])
    return Env(
        env_class,
        provides,
        dict(roles),
        hosts,
        reader.scalars(variables, "[vars]"),
        port_base,
        tuning,
        probes,
    )


# --- from case to command lines -------------------------------------------


@dataclass(frozen=True)
class ProcPlan:
    role: str
    host: str
    case_dir: str
    argv: tuple[str, ...]
    env: dict[str, str]
    mkdirs: tuple[str, ...]


@dataclass(frozen=True)
class Plan:
    dirname: str
    procs: tuple[ProcPlan, ...]
    flags: dict[str, lib.FlagValue]
    problems: tuple[str, ...]
    secrets: tuple[str, ...] = ()


def slug(case_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]", "_", case_key.replace("/", "__"))


def dir_name(position: int, case_key: str, variant: str, round_: int) -> str:
    suffix = ".aa" if variant == "aa" else f".r{round_}" if round_ else ""
    return f"{position:03d}-{slug(case_key)}{suffix}"


def ucx_tls(case: lib.Case) -> str | None:
    """UCX_TLS for the case's transport; None leaves the choice to UCX."""
    if case.backend != "UCX" or case.transport not in UCX_TLS_TRANSPORTS:
        return None
    gpu = "VRAM" in (case.init_mem, case.tgt_mem)
    return case.transport + (",rocm" if gpu else "")


def work_dir(spec: HostSpec, home: str | None) -> str | None:
    if spec.work_dir.startswith("/"):
        return spec.work_dir.rstrip("/") or "/"
    return f"{home.rstrip('/')}/{spec.work_dir}".rstrip("/") if home else None


def plan_case(
    case: lib.Case,
    profile: lib.Profile,
    env: Env,
    homes: Mapping[str, str],
    run_uid: str,
    position: int,
    variant: str = "head",
    round_: int = 0,
) -> Plan:
    """The processes of one execution; problems says why it cannot be launched."""
    dirname = dir_name(position, case.case_key, variant, round_)
    problems: list[str] = []
    missing: set[str] = set()
    hosts = {role: env.hosts[name] for role, name in env.roles.items()}
    roles = ["init", "tgt"] if case.backend in lib.PAIR_BACKENDS else ["init"]
    if "tgt" in roles and "tgt" not in hosts:
        problems.append("the env has no tgt role")
        roles = ["init"]

    shared = dict(env.vars)
    shared["asio_port"] = str(env.asio_port_base + position % 1000)
    shared["benchmark_group"] = f"{slug(case.case_key)}-{position}"
    if hosts.get("tgt", hosts["init"]) is hosts["init"]:
        shared["asio_address"] = "127.0.0.1"
    elif "ip" in hosts["init"].vars:
        shared["asio_address"] = hosts["init"].vars["ip"]
    for role, spec in hosts.items():
        shared.update({f"{role}.{name}": value for name, value in spec.vars.items()})

    def fill(text: str, context: Mapping[str, str]) -> str:
        def swap(match: re.Match[str]) -> str:
            if match.group(1) not in context:
                missing.add(match.group(1))
                return match.group(0)
            return context[match.group(1)]

        return PLACEHOLDER_RE.sub(swap, text)

    template = lib.effective_flags(case, profile)
    late_numbers = any(
        isinstance(value, str) and lib.NIXLBENCH_FLAGS.get(name) is int
        for name, value in template.items()
    )
    # A device list is for the link between two hosts, not for 127.0.0.1.
    two_hosts = hosts.get("tgt", hosts["init"]) is not hosts["init"]
    procs = []
    secrets: set[str] = set()
    flags: dict[str, lib.FlagValue] = {}
    for role in roles:
        spec = hosts[role]
        context = dict(shared)
        context.update({f"self.{name}": value for name, value in spec.vars.items()})
        base = work_dir(spec, homes.get(spec.name))
        case_dir = f"{base}/{run_uid}/{dirname}" if base else ""
        if base:
            context["case_dir"] = case_dir
        else:
            problems.append(f"the home directory of host {spec.name} is not known")
        resolved: dict[str, lib.FlagValue] = {}
        for name, value in template.items():
            if isinstance(value, str):
                value = fill(value, context)
                if lib.NIXLBENCH_FLAGS.get(name) is int:
                    if re.fullmatch(r"\d+", value):
                        value = int(value)
                    elif not PLACEHOLDER_RE.search(value):
                        problems.append(f"flag {name} is not a number")
            resolved[name] = value
        if late_numbers:
            # The numbers were placeholders when check_case saw them.
            problems += flag_problems(case, resolved, env.env_class)
        program = spec.nixlbench
        if "/" in program and not program.startswith("/"):
            # Like work_dir: the process starts in the case directory.
            program = f"{homes.get(spec.name, '')}/{program}"
        argv = [program] + [
            f"--{name}={render(resolved[name])}" for name in lib.flag_order(resolved)
        ]
        for token in argv[1:]:
            banned = [word for word in DISABLED_TOKENS if word in token]
            if banned:
                problems.append(
                    f"{token.split('=')[0]} contains '{banned[0]}', which nixlbench bans"
                )
        environ = dict(spec.env)
        if case.backend == "UCX":
            environ["UCX_PROTO_INFO"] = "y"
            environ["UCX_LOG_FILE"] = f"{case_dir}/{role}.ucx.log"
            tls = ucx_tls(case)
            if tls:
                environ["UCX_TLS"] = tls
            if two_hosts and "ucx_net_devices" in spec.vars:
                environ["UCX_NET_DEVICES"] = spec.vars["ucx_net_devices"]
        environ.update({name: fill(value, context) for name, value in case.env.items()})
        secrets.update(
            str(resolved[name]) for name in lib.SECRET_FLAGS if name in resolved
        )
        secrets.update(value for name, value in environ.items() if secret_name(name))
        mkdirs = [case_dir]
        if isinstance(resolved.get("filepath"), str) and resolved["filepath"]:
            mkdirs.append(str(resolved["filepath"]))
        procs.append(
            ProcPlan(role, spec.name, case_dir, tuple(argv), environ, tuple(mkdirs))
        )
        if role == "init":
            flags = resolved
    if missing:
        names = ", ".join("{" + name + "}" for name in sorted(missing))
        problems.insert(0, f"unresolved {names}")
    # Longest first, so that a secret inside another is not left half masked.
    known = sorted(
        (value for value in secrets if len(value) >= SECRET_MIN and "{" not in value),
        key=lambda value: (-len(value), value),
    )
    return Plan(
        dirname, tuple(procs), flags, tuple(dict.fromkeys(problems)), tuple(known)
    )


def scrub(text: str, secrets: Sequence[str] = ()) -> str:
    """text with the known secret values and whatever looks like one masked."""
    for value in secrets:
        text = text.replace(value, "***")
    return lib.redact_text(text)


def secret_name(name: str) -> bool:
    return lib.redact_text(f"{name}=x") != f"{name}=x"


def shown_env(environ: Mapping[str, str]) -> dict[str, str]:
    return {
        name: "***" if secret_name(name) else value
        for name, value in sorted(environ.items())
    }


def shown_argv(argv: Sequence[str]) -> list[str]:
    masked = []
    for token in argv:
        name, eq, _ = token.partition("=")
        secret = eq and name.lstrip("-") in lib.SECRET_FLAGS
        masked.append(f"{name}=***" if secret else token)
    return lib.redact_argv(masked)


def cmdline(proc: ProcPlan) -> str:
    """The command of one process as a shell line, secrets masked."""

    def quote(token: str) -> str:
        return token if PLAIN_TOKEN_RE.fullmatch(token) else shlex.quote(token)

    parts = [f"{name}={quote(value)}" for name, value in shown_env(proc.env).items()]
    return " ".join(parts + [quote(token) for token in shown_argv(proc.argv)])


def launch_script(
    proc: ProcPlan, spec: HostSpec, timeout_s: int, kill_after_s: int
) -> str:
    quote = shlex.quote
    command = " ".join(quote(token) for token in proc.argv)
    lines = [
        "set -uo pipefail",
        f'ulimit -l unlimited 2>/dev/null || echo "{ULIMIT_NOTE}" >&2',
        f"mkdir -p {' '.join(map(quote, proc.mkdirs))}"
        f" && cd {quote(proc.case_dir)} || exit 125",
    ]
    for number, line in enumerate(spec.setup, 1):
        # On its own line: a setup line may end in a comment.
        failed = f'echo "amd-bench: setup line {number} failed" >&2; exit 125'
        lines += [line, f"test $? -eq 0 || {{ {failed}; }}"]
    lines += [
        *(f"export {name}={quote(value)}" for name, value in sorted(proc.env.items())),
        # Started with '&' and nothing around it: bash then leaves SIGINT
        # ignored in timeout itself, which uutils timeout needs in order to
        # pass a SIGINT on instead of killing at once. GNU timeout does not care.
        f"timeout --signal=INT --kill-after={kill_after_s}s {timeout_s}s"
        f" {command} </dev/null &",
        f'pid=$!; echo "$pid" > {proc.role}.pid',
        'wait "$pid"; rc=$?',
        'echo "amd-bench: exit rc=$rc" >&2',
        'exit "$rc"',
    ]
    return "\n".join(lines) + "\n"


# --- hosts and processes --------------------------------------------------


class Host:
    def __init__(self, spec: HostSpec) -> None:
        self.spec = spec
        self.home: str | None = None
        self.problem = ""

    def command(self) -> list[str]:
        if self.spec.kind == "local":
            return ["bash", "-s"]
        options = ["-F", self.spec.ssh_config] if self.spec.ssh_config else []
        options += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_S}"]
        return ["ssh", *options, self.spec.ssh_host, "bash", "-s"]

    def popen(self, script: str) -> subprocess.Popen[bytes]:
        # Its own session, so a Ctrl-C at the runner's terminal reaches the
        # runner only and the processes are stopped in order.
        process = subprocess.Popen(
            self.command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        assert process.stdin is not None
        try:
            process.stdin.write(script.encode())
            process.stdin.close()
        except OSError:
            pass
        return process

    def capture(self, script: str, timeout_s: float) -> tuple[int, str, str]:
        if self.problem:
            return 255, "", self.problem
        try:
            done = subprocess.run(
                self.command(),
                input=script.encode(),
                capture_output=True,
                timeout=timeout_s,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired as exc:
            self.check()
            return 124, (exc.stdout or b"").decode("utf-8", "replace"), "timed out"
        except OSError as exc:
            return 127, "", str(exc)
        return (
            done.returncode,
            done.stdout.decode("utf-8", "replace"),
            done.stderr.decode("utf-8", "replace"),
        )

    def shell(self, script: str, timeout_s: float) -> tuple[int, str]:
        code, out, err = self.capture(script, timeout_s)
        return code, out + err

    def read_text(self, path: str) -> str:
        if self.spec.kind == "local":
            try:
                return pathlib.Path(path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                return ""
        # stdout alone: the ssh client's own warnings go to stderr.
        code, out, _ = self.capture(f"cat -- {shlex.quote(path)}", QUERY_S)
        return out if code == 0 else ""

    def check(self) -> None:
        """Marks an ssh host that no longer answers, so nothing waits on it again."""
        if self.spec.kind != "ssh" or self.problem:
            return
        try:
            done = subprocess.run(
                self.command(),
                input=b"exit 0\n",
                capture_output=True,
                timeout=HOST_CHECK_S,
                start_new_session=True,
            )
            failure = "" if done.returncode == 0 else f"ssh exit {done.returncode}"
        except subprocess.TimeoutExpired:
            failure = f"no answer in {HOST_CHECK_S:.0f} s"
        except OSError as exc:
            failure = str(exc)
        if failure:
            self.problem = f"host {self.spec.name} stopped answering: {failure}"
            note(self.problem)

    def resolve_home(self) -> None:
        if self.spec.kind == "local":
            self.home = os.path.expanduser("~")
            return
        code, out, err = self.capture('printf %s "$HOME"', QUERY_S)
        if code == 0 and out.startswith("/"):
            self.home = out.strip()
        else:
            last = (err.strip().splitlines() or [f"exit {code}"])[-1]
            self.problem = f"host {self.spec.name} is unreachable: {last}"


class _Pump(threading.Thread):
    """Copies one stream of a process, redacted, to its log and to memory."""

    def __init__(
        self,
        stream: IO[bytes],
        path: pathlib.Path,
        secrets: Sequence[str],
        watch: str = "",
    ) -> None:
        super().__init__(daemon=True)
        self.stream = stream
        self.path = path
        self.secrets = secrets
        self.watch = watch
        self.seen = threading.Event()
        self.lines: list[str] = []
        self.marker: int | None = None
        self.start()

    def run(self) -> None:
        size = 0
        with open(self.path, "w", encoding="utf-8") as log:
            for raw in self.stream:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                line = scrub(line, self.secrets)
                found = MARKER_RE.fullmatch(line)
                if found:
                    self.marker = int(found.group(1))
                if self.watch and self.watch in line:
                    self.seen.set()
                if size > LOG_CAP:
                    continue
                size += len(line) + 1
                if size > LOG_CAP:
                    line = f"amd-bench: log cut at {LOG_CAP} bytes"
                self.lines.append(line)
                log.write(line + "\n")
                log.flush()
        self.stream.close()

    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class ProcResult:
    role: str
    kind: str
    rc: int | None
    marker: bool
    stdout: str
    stderr: str
    wall_s: float
    ended: float
    stopped: bool = False


class _Proc:
    def __init__(
        self,
        plan: ProcPlan,
        host: Host,
        script: str,
        logs: pathlib.Path,
        secrets: Sequence[str] = (),
    ) -> None:
        self.plan = plan
        self.host = host
        self.started = time.monotonic()
        self.ended: float | None = None
        self.stop_sent: float | None = None
        self.terminated: float | None = None
        self.marked: float | None = None
        self.stopped = False
        self.failure = ""
        self.popen: subprocess.Popen[bytes] | None = None
        self.out: _Pump | None = None
        self.err: _Pump | None = None
        try:
            self.popen = host.popen(script)
        except OSError as exc:
            self.failure = f"cannot start {host.command()[0]}: {exc}"
            self.ended = self.started
            return
        assert self.popen.stdout is not None and self.popen.stderr is not None
        self.out = _Pump(
            self.popen.stdout, logs / f"{plan.role}.stdout.log", secrets, LISTEN_MARK
        )
        self.err = _Pump(self.popen.stderr, logs / f"{plan.role}.stderr.log", secrets)

    def poll(self) -> bool:
        """True once the process is over."""
        if self.ended is None and self.popen is not None:
            now = time.monotonic()
            if self.popen.poll() is not None:
                self.ended = now
            elif self.err is not None and self.err.marker is not None:
                # The script is over, yet an ssh client can wait on a pipe that
                # something the command started still holds.
                self.marked = self.marked or now
                if now - self.marked > MARKER_GRACE_S:
                    self.popen.terminate()
                    self.ended = now
        return self.ended is not None

    def listening(self) -> bool:
        return self.out is not None and self.out.seen.is_set()

    def stop(self) -> None:
        """Asks the process to end, the way a Ctrl-C would."""
        pid_file = shlex.quote(f"{self.plan.case_dir}/{self.plan.role}.pid")
        script = f"for i in 1 2 3; do test -s {pid_file} && break; sleep 0.3; done\n"
        script += f'kill -INT "$(cat {pid_file})"\n'
        self.host.shell(script, STOP_S)
        self.stop_sent = time.monotonic()

    def result(self) -> ProcResult:
        code = self.popen.returncode if self.popen is not None else None
        stdout = stderr = ""
        marker = False
        if self.out is not None and self.err is not None:
            # After a terminate, what the host left running may hold the pipes.
            wait = 1 if self.terminated else 10
            self.out.join(wait)
            self.err.join(wait)
            stdout, stderr = self.out.text(), self.err.text()
            if self.err.marker is not None:
                marker, code = True, self.err.marker
        ended = self.ended if self.ended is not None else time.monotonic()
        return ProcResult(
            self.plan.role,
            self.host.spec.kind,
            code,
            marker,
            stdout,
            stderr or self.failure,
            round(ended - self.started, 3),
            ended,
            self.stopped,
        )


# --- gates ----------------------------------------------------------------


@dataclass(frozen=True)
class Verdict:
    status: str
    error_class: str
    excerpt: str
    consistency: str
    transport_obs: str | None
    table: Table


def excerpt_of(text: str, secrets: Sequence[str] = ()) -> str:
    return lib.clip(scrub(text, secrets))


def own_stderr(result: ProcResult) -> list[str]:
    """What the process and its shell said, without the runner's own lines."""
    lines = []
    for line in result.stderr.splitlines():
        if not line.strip() or line == ULIMIT_NOTE or MARKER_RE.fullmatch(line):
            continue
        lines.append(" ".join(JOB_LINE_RE.sub(r"\1", line).split()))
    return lines


def stderr_tail(result: ProcResult) -> str:
    lines = own_stderr(result)
    return " | ".join(lines[-5:]) or f"{result.role} exited {result.rc}, stderr empty"


def asio_abort(result: ProcResult) -> bool:
    """nixlbench aborting over its runtime: what the peer of a dead process does."""
    text = "\n".join(own_stderr(result))
    return (result.rc or 0) >= 128 and bool(ASIO_ABORT_RE.search(text))


def evaluate(
    case: lib.Case,
    flags: Mapping[str, lib.FlagValue],
    results: Sequence[ProcResult],
    ucx_log: str,
) -> Verdict:
    """Status of a launched case from its exit codes, output and UCX log."""
    by_role = {result.role: result for result in results}
    init, tgt = by_role["init"], by_role.get("tgt")
    table_error = ""
    try:
        table = parse_stdout(init.stdout)
    except TableError as exc:
        table, table_error = Table((), (), {}, 0), str(exc)

    exits_ok = all(result.marker and result.rc == 0 for result in results)
    output = [text for result in results for text in (result.stdout, result.stderr)]
    # nixlbench checks the data of READ and WRITE only.
    if flags.get("check_consistency") is not True or case.op == "QUERY":
        consistency = "off"
    elif any(CONSISTENCY_LINE in text for text in output):
        consistency = "failed"
    elif exits_ok and table.config.get("check_consistency") == "1":
        consistency = "clean"
    else:
        consistency = "unknown"

    transport_obs = None
    transport_fault = ""
    if case.backend == "UCX":
        transport_obs = parse_ucx_log(ucx_log) or "unknown"
        if transport_obs != "unknown" and case.transport in UCX_TLS_TRANSPORTS:
            network = set(transport_obs.split("+")).intersection(NETWORK_FAMILIES)
            wanted = set() if case.transport == "shm" else {case.transport}
            if network != wanted:
                transport_fault = f"UCX selected {transport_obs}, not {case.transport}"

    seen = [(row["block_size"], row["batch_size"]) for row in table.rows]
    expected = lib.sweep(flags)
    rows_fault = ""
    if seen != expected:
        rows_fault = f"{len(seen)} of {len(expected)} rows, or not the sweep asked for"

    echoed = []
    for name in ECHO_FLAGS:
        if name in flags and name in table.config:
            value = flags[name]
            asked = str(int(value)) if isinstance(value, bool) else str(value)
            if asked != table.config[name]:
                echoed.append(f"{name} asked {asked} ran {table.config[name]}")

    role_fault = ""
    if tgt is not None:
        # A target that died before it reached the runtime shows neither line;
        # that is an exit failure, not a swap.
        if CONNECT_LINE in init.stdout:
            role_fault = "the initiator connected instead of binding"
        elif CONNECT_LINE not in tgt.stdout:
            if "Block Size" in tgt.stdout or (tgt.marker and tgt.rc == 0):
                role_fault = "the target bound the address instead of connecting"

    def failed(result: ProcResult) -> bool:
        return not result.marker or result.rc != 0

    # One process decides: the first to fail that the runner did not stop.
    # The abort of a process that lost its peer is a consequence, and is
    # passed over while another failed. End times alone would not tell: the
    # two can end within one poll.
    ordered = sorted(results, key=lambda result: result.ended)
    faults = [result for result in ordered if failed(result) and not result.stopped]
    faults = faults or [result for result in ordered if failed(result)]
    causes = [result for result in faults if not asio_abort(result)]
    hit: ProcResult | None = next(iter(causes or faults), None)

    def verdict(status: str, error_class: str, text: str) -> Verdict:
        return Verdict(
            status, error_class, excerpt_of(text), consistency, transport_obs, table
        )

    if hit and not hit.marker:
        return verdict(
            "fail", "ssh" if hit.kind == "ssh" else "launch", stderr_tail(hit)
        )
    if hit and hit.rc == 124:
        return verdict("timeout", "timeout", stderr_tail(hit))
    if hit and hit.rc == 137:
        return verdict("timeout", "kill", stderr_tail(hit))
    if hit and (hit.rc or 0) >= 128:
        return verdict(
            "crash", "asio" if asio_abort(hit) else "signal", stderr_tail(hit)
        )
    if table_error:
        return verdict("fail", "table", table_error)
    if role_fault:
        return verdict("fail", "role-swap", role_fault)
    if consistency == "failed":
        said = [r for r in ordered if CONSISTENCY_LINE in r.stdout + r.stderr]
        return verdict("fail", "consistency", stderr_tail((said or [init])[0]))
    if hit:
        return verdict("fail", "exit-nonzero", stderr_tail(hit))
    if rows_fault:
        return verdict("fail", "rows", rows_fault)
    if echoed:
        return verdict("fail", "config-echo", "; ".join(echoed))
    if transport_fault:
        return verdict("fail", "transport", transport_fault)
    if consistency == "unknown":
        return verdict(
            "fail", "consistency", "nixlbench did not echo check_consistency"
        )
    return verdict("pass", "", "")


# --- schedule -------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    position: int
    case: lib.Case
    variant: str
    round: int


def rank(case: lib.Case, lane: lib.Lane) -> int:
    """Place in a boot: what can wedge a device runs after what cannot."""
    vram = "VRAM" in (case.init_mem, case.tgt_mem)
    rdma = case.transport in RDMA_TRANSPORTS
    if vram and case.backend in lib.PAIR_BACKENDS and lane.topology == "single":
        return 4
    return 2 * rdma + vram


def schedule(
    lane: lib.Lane, cases: Sequence[lib.Case], sentinel_rounds: int, aa: bool
) -> list[Entry]:
    def rounds(case: lib.Case) -> int:
        return sentinel_rounds if case.sentinel and case.status == "active" else 1

    items: list[tuple[lib.Case, str, int]] = []
    for number in range(max(map(rounds, cases), default=0)):
        turn = [case for case in cases if rounds(case) > number]
        for case in sorted(turn, key=lambda case: (rank(case, lane), case.index)):
            items.append((case, "head", number))
            if aa and number == 0 and case.status == "active":
                if case.case_key == lane.aa_case:
                    items.append((case, "aa", 0))
    return [Entry(position, *item) for position, item in enumerate(items)]


# --- run ------------------------------------------------------------------


def load_meta(path: pathlib.Path | None) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise lib.BenchError(f"cannot read run-meta {path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise lib.BenchError(f"run-meta {path} is not a JSON object")
    unknown = sorted(set(meta) - set(lib.META_KEYS))
    if unknown:
        raise lib.BenchError(f"run-meta {path} has unknown keys: {', '.join(unknown)}")
    return meta


def cpu_model() -> str | None:
    try:
        text = pathlib.Path("/proc/cpuinfo").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return None
    found = re.search(r"^model name\s*:\s*(.+)$", text, re.MULTILINE)
    return found.group(1).strip() if found else None


def first_run_record(
    meta: Mapping[str, Any], matrix: lib.Matrix, lane: lib.Lane, tier: str
) -> dict[str, Any]:
    record = lib.blank_record("run")
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    record.update(
        repo="local", stamp=stamp, run_attempt=1, rocm_version="unknown", features=[]
    )
    if meta.get("tier", tier) != tier:
        # The same file goes to the collector, which takes the tier from it.
        raise lib.BenchError(f"run-meta has tier {meta['tier']!r}, --tier is {tier}")
    record.update(meta)
    if "bench_run_id" not in meta:
        record["bench_run_id"] = f"local-{record['stamp']}"
    record.update(
        status="partial",
        tier=tier,
        lane=lane.name,
        env_class=lane.env_class,
        timing_trust=lane.timing_trust,
        matrix_sha256=lib.matrix_sha256(lib.matrix_manifest(matrix)),
        runner_cpu_model=cpu_model(),
        started_at=utc(),
    )
    try:
        record["run_uid"] = lib.run_uid(
            record["repo"],
            record["bench_run_id"],
            record["run_attempt"],
            lane.name,
            record["rocm_version"],
        )
        record["env_fp"] = lib.env_fingerprint(record)
    except (TypeError, ValueError) as exc:
        raise lib.BenchError(f"run-meta cannot be recorded: {exc}") from exc
    return record


@dataclass
class Options:
    only: tuple[str, ...] = ()
    deadline: float | None = None
    sentinel_rounds: int = 1
    aa: bool = False
    summary_out: pathlib.Path | None = None


@dataclass
class _Sample:
    load1: float | None = None
    steal: int = 0
    total: int = 0


@dataclass
class _Outcome:
    record: dict[str, Any]
    samples: list[dict[str, Any]] = field(default_factory=list)
    counters: list[dict[str, Any]] = field(default_factory=list)
    launched: bool = False


class Runner:
    def __init__(
        self,
        matrix: lib.Matrix,
        lane: lib.Lane,
        tier: str,
        env: Env,
        out: pathlib.Path,
        meta: Mapping[str, Any],
        options: Options,
    ) -> None:
        self.matrix = matrix
        self.lane = lane
        self.env = env
        self.out = out
        self.options = options
        self.hosts = {name: Host(spec) for name, spec in env.hosts.items()}
        self.run = first_run_record(meta, matrix, lane, tier)
        self.run_uid: str = self.run["run_uid"]
        self.entries = schedule(
            lane, lib.select_cases(lane, tier), options.sentinel_rounds, options.aa
        )
        self.suspect: set[str] = set()
        self.interrupt = ""
        self.incomplete = False
        self.required_failed = False
        self.summary: list[tuple[str, str, str, str, str]] = []
        self.writer: lib.NdjsonWriter | None = None

    # -- small helpers --

    def host_of(self, name: str) -> Host:
        return self.hosts[self.env.roles.get(name, name)]

    def emit(self, record: Mapping[str, Any]) -> None:
        assert self.writer is not None
        self.writer.write(record)

    def on_signal(self, number: int, frame: Any) -> None:
        self.interrupt = signal.Signals(number).name

    def sleep(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.interrupt and time.monotonic() < end:
            time.sleep(min(0.05, max(0.0, end - time.monotonic())))

    def homes(self) -> dict[str, str]:
        return {name: host.home for name, host in self.hosts.items() if host.home}

    # -- host measurements --

    def calibrate(self) -> float | None:
        script = CALIBRATION.format(bytes=self.env.tuning.calib_bytes)
        code, out, _ = self.host_of("init").capture(script, 300)
        try:
            return round(float(out.strip().splitlines()[-1]), 4) if code == 0 else None
        except (IndexError, ValueError):
            return None

    def guest_facts(self) -> None:
        host = self.host_of("init")
        if host.spec.kind != "ssh" or host.home is None:
            return
        script = "uname -r; nproc; awk '/^MemTotal:/ {print $2}' /proc/meminfo"
        code, out, _ = host.capture(script, QUERY_S)
        lines = out.split()
        if code == 0 and len(lines) == 3 and lines[1].isdigit() and lines[2].isdigit():
            self.run.update(
                guest_kernel=lines[0],
                guest_vcpus=int(lines[1]),
                guest_mem=int(lines[2]) * 1024,
            )

    def load(self) -> _Sample:
        script = "cat /proc/loadavg; grep '^cpu ' /proc/stat"
        code, out, _ = self.host_of("init").capture(script, QUERY_S)
        lines = out.splitlines()
        sample = _Sample()
        try:
            if code == 0:
                sample.load1 = float(lines[0].split()[0])
                fields = [int(value) for value in lines[1].split()[1:9]]
                sample.steal, sample.total = fields[7], sum(fields)
        except (IndexError, ValueError):
            pass
        return sample

    # -- probes --

    def probe(self, probe: Probe, subject: str) -> None:
        kill = self.env.tuning.kill_after_s
        script = (
            f"timeout --signal=INT --kill-after={kill}s {probe.timeout_s}s "
            f"bash -c {shlex.quote(probe.cmd)} </dev/null"
        )
        started = time.monotonic()
        code, out = self.host_of(probe.host).shell(
            script, probe.timeout_s + kill + SLACK_S
        )
        record = lib.blank_record("test")
        status = "pass" if code == 0 else "timeout" if code in (124, 137) else "fail"
        record.update(
            run_uid=self.run_uid,
            suite="probe",
            test_id=f"{probe.name}/{subject}",
            status=status,
            duration_s=round(time.monotonic() - started, 3),
        )
        if status != "pass":
            lines = [line.strip() for line in out.splitlines() if line.strip()]
            record["error_excerpt"] = excerpt_of(
                " | ".join(lines[-5:]) or f"exit {code}, no output"
            )
            self.suspect.update(probe.classes)
            doubted = ", ".join(probe.classes) or "nothing"
            note(f"probe {probe.name} {status}: {doubted} now suspect")
        self.emit(record)

    def probes(
        self, when: str, subject: str, needs: Iterable[str] | None = None
    ) -> None:
        for probe in self.env.probes:
            if probe.when != when or self.interrupt:
                continue
            if needs is None or set(needs).intersection(probe.classes):
                self.probe(probe, subject)

    # -- one case --

    def decide(self, entry: Entry, plan: Plan) -> tuple[str, str]:
        """(skip reason, text) for a case that is not launched, else ('', '')."""
        case = entry.case
        globs = self.options.only
        if globs and not any(
            fnmatch.fnmatchcase(case.case_key, glob) for glob in globs
        ):
            return "filter", ""
        if self.interrupt:
            return "aborted", f"the runner received {self.interrupt}"
        if self.options.deadline is not None:
            # A case that starts must be able to end, kill included, in time.
            longest = case.timeout_s + self.env.tuning.kill_after_s
            if time.time() >= self.options.deadline:
                return "budget", "the deadline passed before this case"
            if time.time() + longest > self.options.deadline:
                return "budget", f"under {longest} s left before the deadline"
        lacking = sorted(set(case.needs) - set(self.env.provides))
        if lacking:
            return "env", f"needs {', '.join(lacking)}"
        doubted = sorted(self.suspect.intersection(case.needs))
        if doubted:
            return (
                "device-suspect",
                f"suspect after a failed probe or a kill: {', '.join(doubted)}",
            )
        down = self.down(plan)
        if down or plan.problems:
            return "env", down or "; ".join(plan.problems)
        return "", ""

    def down(self, plan: Plan) -> str:
        """Why a host of the plan cannot be used, or nothing."""
        found = [self.hosts[proc.host].problem for proc in plan.procs]
        return "; ".join(dict.fromkeys(filter(None, found)))

    def supervise(self, procs: list[_Proc], timeout_s: int) -> None:
        tuning = self.env.tuning
        limit = timeout_s + tuning.kill_after_s + SLACK_S
        # Every process is polled each turn: the first to end starts the
        # grace of the other.
        while not all([proc.poll() for proc in procs]):
            now = time.monotonic()
            ends = [proc.ended for proc in procs if proc.ended is not None]
            for proc in procs:
                if proc.ended is not None:
                    continue
                if proc.stop_sent is None:
                    peer_gone = bool(ends) and now - min(ends) >= tuning.peer_grace_s
                    if self.interrupt or peer_gone:
                        proc.stopped = True
                        proc.stop()
                    elif now - proc.started > limit:
                        proc.stop()
                elif proc.terminated is None:
                    # The host never answered the stop; only now is the ssh
                    # client or the local shell itself ended. At once when
                    # the host is known to be gone.
                    late = now - proc.stop_sent > tuning.kill_after_s + SLACK_S
                    if late or proc.host.problem:
                        assert proc.popen is not None
                        proc.terminated = now
                        proc.popen.terminate()
                elif now - proc.terminated > 10:
                    proc.ended = now
            time.sleep(0.02)

    def launch(
        self, case: lib.Case, plan: Plan, logs: pathlib.Path
    ) -> tuple[list[ProcResult], float | None]:
        tuning = self.env.tuning
        scripts = [
            launch_script(
                proc, self.env.hosts[proc.host], case.timeout_s, tuning.kill_after_s
            )
            for proc in plan.procs
        ]
        first = plan.procs[0]
        procs = [_Proc(first, self.hosts[first.host], scripts[0], logs, plan.secrets)]
        skew = None
        if len(plan.procs) == 2:
            init = procs[0]
            end = time.monotonic() + tuning.listen_wait_s
            while not (init.listening() or init.poll() or self.interrupt):
                if time.monotonic() >= end:
                    break
                time.sleep(0.02)
            if not init.poll():
                self.sleep(tuning.pair_skew_s)
            # A target without an initiator would bind the address itself.
            if not init.poll() and not self.interrupt:
                tgt = plan.procs[1]
                procs.append(
                    _Proc(tgt, self.hosts[tgt.host], scripts[1], logs, plan.secrets)
                )
                skew = round(procs[1].started - init.started, 3)
        self.supervise(procs, case.timeout_s)
        return [proc.result() for proc in procs], skew

    def counter(
        self, case_uid: str, name: str, value: float, unit: str
    ) -> dict[str, Any]:
        record = lib.blank_record("counter")
        record.update(
            case_uid=case_uid, name=name, value=value, unit=unit, deterministic=False
        )
        return record

    def execute(
        self, entry: Entry, plan: Plan, outcome: _Outcome, before: _Sample
    ) -> None:
        case, record = entry.case, outcome.record
        logs = self.out / "logs" / plan.dirname
        logs.mkdir(parents=True, exist_ok=True)
        record.update(
            order=entry.position,
            started_at=utc(),
            cmdline=cmdline(plan.procs[0]),
            log_ref=f"logs/{plan.dirname}",
        )
        started = time.monotonic()
        results, skew = self.launch(case, plan, logs)
        record["duration_s"] = round(time.monotonic() - started, 3)
        for proc, result in zip(plan.procs, results):
            if not result.marker:
                self.hosts[proc.host].check()
        # A launch that never reported has nothing more to tell, and its host
        # may hang on every further question.
        answered = all(result.marker for result in results)
        after = self.load() if answered else _Sample()

        ucx_logs = {}
        if case.backend == "UCX":
            for proc, result in zip(plan.procs, results):
                path = f"{proc.case_dir}/{proc.role}.ucx.log"
                text = self.hosts[proc.host].read_text(path) if result.marker else ""
                text = scrub(text, plan.secrets)
                ucx_logs[proc.role] = text
                (logs / f"{proc.role}.ucx.log").write_text(text, encoding="utf-8")

        verdict = evaluate(case, plan.flags, results, ucx_logs.get("init", ""))
        by_role = {result.role: result for result in results}
        record.update(
            status=verdict.status,
            error_class=verdict.error_class,
            error_excerpt=verdict.excerpt,
            consistency=verdict.consistency,
            transport_obs=verdict.transport_obs,
            exit_init=by_role["init"].rc,
            exit_tgt=by_role["tgt"].rc if "tgt" in by_role else None,
            rows_seen=len(verdict.table.rows),
            load1_before=before.load1,
            load1_after=after.load1,
        )
        if after.total > before.total:
            steal = 100 * (after.steal - before.steal) / (after.total - before.total)
            record["steal_pct"] = round(steal, 2)
        if self.interrupt and any(result.stopped for result in results):
            # Its processes were interrupted, so this is no verdict on the case.
            record.update(
                status="skip",
                skip_reason="aborted",
                error_class="",
                error_excerpt=f"interrupted: the runner received {self.interrupt}",
            )
        for row in verdict.table.rows:
            sample = lib.blank_record("sample")
            sample.update(row, case_uid=record["case_uid"])
            outcome.samples.append(sample)
        uid = record["case_uid"]
        for result in results:
            outcome.counters.append(
                self.counter(uid, f"proc_wall_s.{result.role}", result.wall_s, "s")
            )
        if skew is not None:
            outcome.counters.append(self.counter(uid, "pair_skew_s", skew, "s"))
        if case.backend == "UCX":
            complaints = sum(
                bool(UCX_COMPLAINT_RE.search(line))
                for text in ucx_logs.values()
                for line in text.splitlines()
            )
            outcome.counters.append(
                self.counter(uid, "ucx_warn_lines", complaints, "count")
            )
        if any(result.rc == 137 for result in results):
            wedged = {"gpu", "rdma"}.intersection(case.needs)
            if wedged:
                self.suspect.update(wedged)
                note(
                    f"{case.case_key} was killed: {', '.join(sorted(wedged))} now suspect"
                )

    def run_entry(self, entry: Entry) -> _Outcome:
        case = entry.case
        profile = self.matrix.profiles[case.profile]
        outcome = _Outcome(
            lib.case_record(
                case, profile, self.run_uid, variant=entry.variant, round_=entry.round
            )
        )
        if case.status == "blocked":
            return outcome
        try:
            self.attempt(entry, profile, outcome)
        except Exception as exc:
            # One case that the runner itself trips over must not end the lane.
            traceback.print_exc()
            outcome.record.update(
                status="fail",
                skip_reason=None,
                error_class="internal",
                error_excerpt=excerpt_of(f"{type(exc).__name__}: {exc}"),
            )
        return outcome

    def attempt(self, entry: Entry, profile: lib.Profile, outcome: _Outcome) -> None:
        case = entry.case
        plan = plan_case(
            case,
            profile,
            self.env,
            self.homes(),
            self.run_uid,
            entry.position,
            entry.variant,
            entry.round,
        )
        reason, text = self.decide(entry, plan)
        before = _Sample()
        if not reason:
            # Also the last look at whether the hosts still answer.
            before = self.load()
            text = self.down(plan)
            reason = "env" if text else ""
        if reason:
            outcome.record.update(
                status="skip", skip_reason=reason, error_excerpt=excerpt_of(text)
            )
            return
        outcome.launched = True
        self.execute(entry, plan, outcome, before)

    def account(self, entry: Entry, record: Mapping[str, Any]) -> None:
        status, reason = record["status"], record["skip_reason"]
        if reason in ("budget", "aborted"):
            self.incomplete = True
        failed = status in ("fail", "timeout", "crash")
        if status == "skip" and reason in (
            "budget",
            "device-suspect",
            "env",
            "aborted",
        ):
            failed = True
        if failed and record["required"]:
            self.required_failed = True
        label = record["case_key"]
        if entry.variant != "head" or entry.round:
            label += f" ({entry.variant} round {entry.round})"
        detail = record["error_class"] or reason or ""
        rows = f"{record['rows_seen'] or 0}/{record['rows_expected']}"
        took = "" if record["duration_s"] is None else f"{record['duration_s']:.1f} s"
        self.summary.append((label, status, detail, rows, took))
        order = "---" if record["order"] is None else f"{record['order']:03d}"
        print(f"{order} {status:<8} {detail:<14} {label} {took}".rstrip(), flush=True)

    # -- the whole run --

    def body(self) -> None:
        tuning = self.env.tuning
        for name in dict.fromkeys(self.env.roles.values()):
            self.hosts[name].resolve_home()
            if self.hosts[name].problem:
                note(self.hosts[name].problem)
        self.guest_facts()
        self.run["runner_disk_free_before"] = shutil.disk_usage(self.out).free
        if tuning.calibrate:
            self.run["calib_gbps_first"] = self.run["calib_gbps"] = self.calibrate()
        self.emit(self.run)
        self.probes("start", "start")
        for entry in self.entries:
            outcome = self.run_entry(entry)
            self.emit(outcome.record)
            for record in outcome.samples + outcome.counters:
                self.emit(record)
            self.account(entry, outcome.record)
            if outcome.launched:
                self.probes("between", entry.case.case_key, entry.case.needs)
        self.probes("end", "end")
        if tuning.calibrate and not self.interrupt:
            last = self.calibrate()
            both = [value for value in (self.run["calib_gbps_first"], last) if value]
            self.run.update(
                calib_gbps_last=last, calib_gbps=min(both) if both else None
            )
        self.run.update(
            status="partial" if self.incomplete else "complete",
            runner_disk_free_after=shutil.disk_usage(self.out).free,
            finished_at=utc(),
        )
        self.emit(self.run)

    def write_summary(self) -> None:
        if self.options.summary_out is None:
            return
        lines = [
            f"### nixlbench {self.lane.name} ({self.run['tier']}, ROCm "
            f"{self.run['rocm_version']}): {self.run['status']}",
            "",
            "| Case | Status | Class | Rows | Duration |",
            "|---|---|---|---|---|",
        ]
        lines += [
            f"| `{label}` | {status} | {detail} | {rows} | {took} |"
            for label, status, detail, rows, took in self.summary
        ]
        try:
            with open(self.options.summary_out, "a", encoding="utf-8") as stream:
                stream.write("\n".join(lines) + "\n\n")
        except OSError as exc:
            note(f"cannot write the summary: {exc}")

    def main(self) -> int:
        try:
            self.out.mkdir(parents=True, exist_ok=True)
            self.writer = lib.NdjsonWriter(self.out / "records.ndjson")
        except OSError as exc:
            note(f"cannot write to --out {self.out}: {exc}")
            return 2
        # SIGHUP too: the session that started the runner may go away.
        previous = {
            number: signal.signal(number, self.on_signal)
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
        }
        try:
            self.body()
            self.write_summary()
        except Exception:
            traceback.print_exc()
            note("internal error; the records written so far are valid")
            return 3
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)
            self.writer.close()
        return 1 if self.required_failed else 0


# --- commands -------------------------------------------------------------


def lane_problems(matrix: lib.Matrix, lane: lib.Lane) -> list[str]:
    return [
        f"lanes/{lane.name}.toml: {case.case_key}: {text}"
        for case in lane.cases
        for text in check_case(case, matrix.profiles[case.profile], lane)
    ]


def display_homes(env: Env) -> dict[str, str]:
    """Home directories for display: a remote one is not looked up."""
    return {
        name: os.path.expanduser("~") if spec.kind == "local" else "$HOME"
        for name, spec in env.hosts.items()
    }


def print_problems(problems: Iterable[str]) -> int:
    for text in problems:
        print(text)
    return 2


def load_all(
    args: argparse.Namespace,
) -> tuple[lib.Matrix | None, Env | None, list[str]]:
    problems: list[str] = []
    matrix = env = None
    try:
        matrix = lib.load_matrix(args.matrix)
    except lib.MatrixError as exc:
        problems += exc.problems
    except lib.BenchError as exc:
        problems.append(str(exc))
    if matrix is not None and args.lane and args.lane not in matrix.lanes:
        problems.append(f"{args.matrix}: no lane '{args.lane}'")
    if args.env is not None:
        try:
            env = load_env(args.env)
        except EnvError as exc:
            problems += exc.problems
    if matrix is not None and env is not None and args.lane in matrix.lanes:
        wanted = matrix.lanes[args.lane].env_class
        if env.env_class != wanted:
            problems.append(
                f"{args.env}: env_class is '{env.env_class}' but lane {args.lane} "
                f"needs '{wanted}'"
            )
    return matrix, env, problems


def chosen_lanes(
    matrix: lib.Matrix, env: Env | None, name: str | None
) -> list[lib.Lane]:
    if name:
        return [matrix.lanes[name]] if name in matrix.lanes else []
    lanes = [matrix.lanes[key] for key in sorted(matrix.lanes)]
    if env is not None:
        lanes = [lane for lane in lanes if lane.env_class == env.env_class]
    return lanes


def cmd_validate(args: argparse.Namespace) -> int:
    matrix, env, problems = load_all(args)
    if matrix is not None:
        for name in sorted(matrix.lanes):
            if not args.lane or name == args.lane:
                problems += lane_problems(matrix, matrix.lanes[name])
        if env is not None:
            homes = display_homes(env)
            for lane in chosen_lanes(matrix, env, args.lane):
                for case in lane.cases:
                    if case.status != "active":
                        continue
                    profile = matrix.profiles[case.profile]
                    plan = plan_case(case, profile, env, homes, "0" * 20, 0)
                    problems += [
                        f"{args.env}: {case.case_key}: {text}" for text in plan.problems
                    ]
    if problems:
        return print_problems(problems)
    print("ok")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    commands = args.commands or args.format == "json"
    if commands and args.env is None:
        return print_problems(["--commands and --format json need --env"])
    matrix, env, problems = load_all(args)
    if problems or matrix is None:
        return print_problems(problems)
    tier = args.tier or lib.TIERS[-1]
    listing = []
    for lane in chosen_lanes(matrix, env, args.lane):
        entries = schedule(
            lane, lib.select_cases(lane, tier), SENTINEL_ROUNDS[tier], tier != "pr"
        )
        for entry in entries:
            case = entry.case
            profile = matrix.profiles[case.profile]
            label = case.case_key
            if entry.variant != "head" or entry.round:
                label += f" ({entry.variant} round {entry.round})"
            rows = len(lib.sweep(lib.effective_flags(case, profile)))
            lines = [
                f"{entry.position:>3}  {label}  {case.tier}  {case.role}  "
                f"{case.status}  {case.profile}  {rows}"
            ]
            if env is not None and commands and case.status == "active":
                plan = plan_case(
                    case,
                    profile,
                    env,
                    display_homes(env),
                    "<run_uid>",
                    entry.position,
                    entry.variant,
                    entry.round,
                )
                lines += [f"     # {text}" for text in plan.problems]
                for proc in plan.procs:
                    lines.append(f"     {proc.role}@{proc.host}: {cmdline(proc)}")
                    listing.append(
                        {
                            "case_key": case.case_key,
                            "order": entry.position,
                            "argv": shown_argv(proc.argv),
                            "env": shown_env(proc.env),
                            "host": proc.host,
                            "role": proc.role,
                            "variant": entry.variant,
                            "round": entry.round,
                        }
                    )
            if args.format == "text":
                print("\n".join(lines))
    if args.format == "json":
        print(json.dumps(listing, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    matrix, env, problems = load_all(args)
    if matrix is None or env is None or problems:
        return print_problems(problems)
    lane = matrix.lanes[args.lane]
    problems = lane_problems(matrix, lane)
    if args.sentinel_rounds is not None and args.sentinel_rounds < 1:
        problems.append("--sentinel-rounds must be at least 1")
    records = args.out / "records.ndjson"
    if records.is_file() and records.stat().st_size:
        problems.append(f"{records} already holds a run; give an empty --out")
    if problems:
        return print_problems(problems)
    deadline = args.deadline_epoch
    if args.budget_s is not None:
        deadline = time.time() + args.budget_s
    options = Options(
        tuple(args.only),
        deadline,
        args.sentinel_rounds or SENTINEL_ROUNDS[args.tier],
        args.tier != "pr" if args.aa is None else args.aa,
        args.summary_out,
    )
    try:
        meta = load_meta(args.run_meta)
        runner = Runner(matrix, lane, args.tier, env, args.out, meta, options)
    except lib.BenchError as exc:
        return print_problems([str(exc)])
    try:
        faults = lib.validate_record(runner.run, lib.load_schema())
    except lib.BenchError as exc:
        note(f"the run record is not checked: {exc}")
        faults = []
    if faults:
        return print_problems([f"run-meta gives an invalid run record: {faults[0]}"])
    return runner.main()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    default_matrix = pathlib.Path(__file__).resolve().parent.parent / "bench"

    def command(
        name: str, handler: Callable[[argparse.Namespace], int], lane: bool
    ) -> Any:
        sub = commands.add_parser(name)
        sub.set_defaults(handler=handler)
        sub.add_argument(
            "--matrix", type=pathlib.Path, default=default_matrix, metavar="DIR"
        )
        sub.add_argument("--lane", required=lane)
        sub.add_argument("--env", type=pathlib.Path, required=lane, metavar="FILE")
        return sub

    command("validate", cmd_validate, False)
    listing = command("list", cmd_list, False)
    listing.add_argument("--tier", choices=lib.TIERS)
    listing.add_argument("--commands", action="store_true")
    listing.add_argument("--format", choices=("text", "json"), default="text")
    run = command("run", cmd_run, True)
    run.add_argument("--tier", choices=lib.TIERS, required=True)
    run.add_argument("--out", type=pathlib.Path, required=True, metavar="DIR")
    run.add_argument("--run-meta", type=pathlib.Path, metavar="FILE")
    run.add_argument("--only", action="append", default=[], metavar="GLOB")
    limit = run.add_mutually_exclusive_group()
    limit.add_argument("--deadline-epoch", type=float, metavar="N")
    limit.add_argument("--budget-s", type=float, metavar="N")
    run.add_argument("--sentinel-rounds", type=int, metavar="N")
    run.add_argument("--aa", action=argparse.BooleanOptionalAction)
    run.add_argument("--summary-out", type=pathlib.Path, metavar="FILE")
    args = parser.parse_args(argv)
    handler: Callable[[argparse.Namespace], int] = args.handler
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
