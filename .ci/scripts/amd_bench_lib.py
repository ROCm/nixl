# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared code of the AMD nixlbench runner and collector.

Loads the matrix under .ci/bench (profiles/*.toml, lanes/*.toml), derives the
nixlbench flags, the stable ids and the matrix manifest, builds and validates
schema v1 records, and redacts or rejects secrets. Standard library only.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import math
import os
import pathlib
import re
import tomllib
from dataclasses import dataclass
from typing import IO, Any, Iterator, Mapping, Optional, Sequence

SCHEMA = 1
SCHEMA_MINOR = 0
FlagValue = bool | int | str
TIERS = ("pr", "nightly", "weekly")
ROLES = ("gate", "track", "info")
BACKENDS = (
    "UCX",
    "LIBFABRIC",
    "POSIX",
    "AIS",
    "GUSLI",
    "REDIS",
    "OBJ",
    "AZURE_BLOB",
    "NVME_KV",
)
PAIR_BACKENDS = ("UCX", "LIBFABRIC")
TRANSPORTS = ("none", "any", "tcp", "shm", "rc", "ud", "verbs")
INIT_MEMS = ("DRAM", "VRAM")
TGT_MEMS = ("DRAM", "VRAM", "FILE", "BLK", "OBJ", "KV")
OPS = ("READ", "WRITE", "QUERY")
SCHEMES = ("pairwise", "manytoone", "onetomany", "tp")
RUNTIMES = ("ASIO", "ETCD", "NONE")
ATTACH = (
    "none",
    "net-tcp",
    "net-rdma",
    "pcie-qemu",
    "pcie-spdk",
    "fabric-rdma",
    "http",
    "http-rdma",
    "local-svc",
)
NEEDS = (
    "gpu",
    "rdma",
    "two_hosts",
    "etcd",
    "redis",
    "s3",
    "io_uring",
    "blkdev",
    "libfabric",
)
TOPOLOGIES = ("single", "two-host")
TIMING_TRUST = ("emulated", "container", "hardware")
VARIANTS = ("head", "base", "aa")
CASE_STATUSES = ("pass", "fail", "timeout", "crash", "skip", "blocked", "not-run")
RUN_STATUSES = ("complete", "partial", "missing", "blocked")
TEST_STATUSES = ("pass", "fail", "skip", "timeout")
SKIP_REASONS = ("budget", "device-suspect", "env", "filter", "aborted")
ERROR_CLASSES = (
    "",
    "launch",
    "ssh",
    "exit-nonzero",
    "signal",
    "timeout",
    "kill",
    "asio",
    "consistency",
    "rows",
    "table",
    "transport",
    "config-echo",
    "role-swap",
    "internal",
)
CONSISTENCY = ("clean", "failed", "off", "unknown")
PAIR_KINDS = ("vram-dram", "rc-tcp", "direct-buffered", "fabric-pcie")
ENV_FP_KEYS = (
    "env_class",
    "rocm_version",
    "runner_image",
    "guest_image_digest",
    "overlay_sha256",
    "qemu_minimal_ref",
    "qemu_digest",
    "ernic_digest",
    "ernic_backend",
    "rocjitsu_digest",
    "spdk_digest",
    "gpu_vram_limit",
    "ib_mtu",
)

_INT_FLAGS = (
    "total_buffer_size start_block_size max_block_size start_batch_size "
    "max_batch_size num_iter warmup_iter large_blk_iter_ftr pipeline_depth "
    "num_initiator_dev num_target_dev progress_threads num_files "
    "gds_mt_num_threads posix_ios_pool_size posix_kernel_queue_size "
    "obj_crt_min_limit randomize_location_mode_seed "
    "gusli_max_simultaneous_requests"
)
_BOOL_FLAGS = (
    "check_consistency recreate_xfer reregister_mem prepared_xfer enable_pt "
    "use_hugepages obj_use_virtual_addressing obj_accelerated_enable "
    "gusli_try_use_uring"
)
_STR_FLAGS = (
    "device_list filepath filenames posix_api_type randomize_location_mode "
    "obj_access_key obj_secret_key obj_session_token obj_bucket_name "
    "obj_scheme obj_region obj_endpoint_override obj_req_checksum "
    "obj_ca_bundle obj_accelerated_type gusli_client_name gusli_config_file "
    "gusli_device_byte_offsets gusli_device_security mode"
)
NIXLBENCH_FLAGS: dict[str, type] = {
    **{name: int for name in _INT_FLAGS.split()},
    **{name: bool for name in _BOOL_FLAGS.split()},
    **{name: str for name in _STR_FLAGS.split()},
}
# The flags a case dimension sets, in the order they lead the command line.
DIMENSION_FLAGS = (
    "backend",
    "runtime_type",
    "asio_address",
    "asio_port",
    "etcd_endpoints",
    "benchmark_group",
    "op_type",
    "initiator_seg_type",
    "target_seg_type",
    "scheme",
    "num_threads",
    "storage_enable_direct",
)
RESERVED_FLAGS: frozenset[str] = frozenset(
    DIMENSION_FLAGS + ("config_file", "worker_type")
)
# Every key a --run-meta file may hold: one file serves the runner and the
# collector. 'tier' is for a collection in which no lane reported.
META_KEYS = (
    "stamp nightly_run_id bench_run_id run_attempt reason tier repo branch slug "
    "sha upstream_main_sha nixl_version nixlbench_version features merge_tree "
    "baseline_sha rocm_version ucx_version libfabric_version nixlbench_sha256 "
    "overlay_sha256 runner_image guest_image_digest gpu_vram_limit "
    "qemu_minimal_ref qemu_digest ernic_digest ernic_backend rocjitsu_digest "
    "spdk_digest ib_mtu"
).split()
EXCERPT_MAX = 500
TIMEOUT_MAX = 480
TIMEOUT_MAX_WEEKLY = 900
SECRET_FLAGS = (
    "obj_access_key",
    "obj_secret_key",
    "obj_session_token",
    "azure_blob_connection_string",
)

_SECRET_FLAG_NAMES = "|".join(SECRET_FLAGS)
_SECRET_ENV_NAME_RE = re.compile(
    r"[A-Z0-9_]*(?:PASSWORD|SECRET|TOKEN|ACCESS_KEY|AUTH)[A-Z0-9_]*"
)
# gflags takes -flag as well as --flag, so one dash is covered too. A value
# already masked is left alone, so the ")" or "):" after it survives.
# A value is taken with its quotes, so that the tail of a quoted value with a
# blank in it does not stay in clear.
_VALUE = r"""(?:'[^']*'|"[^"]*"|\S)+"""
_REDACT_FLAG_RE = re.compile(
    rf"(--?)({_SECRET_FLAG_NAMES})([= ])(?!\*\*\*(?:[\s):]|$)){_VALUE}"
)
_REDACT_ENV_RE = re.compile(rf"\b({_SECRET_ENV_NAME_RE.pattern})={_VALUE}")
_REDACT_CONFIG_RE = re.compile(
    r"(OBJ S3 access key|OBJ S3 secret key|OBJ S3 session token"
    r"|Azure Blob Storage connection string)\b(.*?): (.*)$"
)
_REDACT_URL_RE = re.compile(r"(://[^/\s:@]*):[^/\s@]+@")
_CONFIG_HINT_RE = re.compile(r"=[^)\s]*\)")
_SECRET_PATTERNS = (
    ("aws-key-id", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "github-token",
        re.compile(r"\b(gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,})\b"),
    ),
    ("private-key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("azure-account-key", re.compile(r"AccountKey=[A-Za-z0-9+/=]{20,}")),
    ("bearer", re.compile(r"(?i)\bauthorization:\s*bearer\s+\S+")),
    ("url-password", re.compile(r"://[^/\s:@]*:(?!\*\*\*@)[^/\s@]+@")),
    (
        "secret-flag",
        re.compile(rf"--?({_SECRET_FLAG_NAMES})[= ](?!\*\*\*)\S+"),
    ),
    (
        "secret-env",
        re.compile(
            r"\b[A-Z0-9_]*(PASSWORD|SECRET|TOKEN|ACCESS_KEY)[A-Z0-9_]*=(?!\*\*\*)\S+"
        ),
    ),
)

# Every key of every record kind with its blank value, in schema order.
_BLANK: dict[str, dict[str, Any]] = {
    "run": {
        "kind": "run",
        "schema": SCHEMA,
        "schema_minor": SCHEMA_MINOR,
        "run_uid": "",
        "status": "",
        "stamp": "",
        "nightly_run_id": None,
        "bench_run_id": "",
        "run_attempt": 0,
        "reason": None,
        "tier": "",
        "repo": "",
        "branch": None,
        "slug": None,
        "sha": None,
        "upstream_main_sha": None,
        "nixl_version": None,
        "nixlbench_version": None,
        "features": [],
        "merge_tree": None,
        "baseline_sha": None,
        "lane": "",
        "env_class": "",
        "timing_trust": "",
        "rocm_version": "",
        "ucx_version": None,
        "libfabric_version": None,
        "nixlbench_sha256": None,
        "matrix_sha256": "",
        "overlay_sha256": None,
        "runner_image": None,
        "runner_cpu_model": None,
        "runner_disk_free_before": None,
        "runner_disk_free_after": None,
        "guest_image_digest": None,
        "guest_kernel": None,
        "guest_vcpus": None,
        "guest_mem": None,
        "gpu_vram_limit": None,
        "qemu_minimal_ref": None,
        "qemu_digest": None,
        "ernic_digest": None,
        "ernic_backend": None,
        "rocjitsu_digest": None,
        "spdk_digest": None,
        "ib_mtu": None,
        "calib_gbps": None,
        "calib_gbps_first": None,
        "calib_gbps_last": None,
        "degraded": None,
        "env_fp": "",
        "started_at": None,
        "finished_at": None,
    },
    "case": {
        "kind": "case",
        "run_uid": "",
        "case_uid": "",
        "case_key": "",
        "workload_key": "",
        "pair_key": "",
        "pair_role": "",
        "params_sha": "",
        "profile": "",
        "role": "",
        "required": False,
        "tier": "",
        "variant": "",
        "round": 0,
        "order": None,
        "backend": "",
        "engine": "",
        "transport_req": "",
        "transport_obs": None,
        "init_mem": "",
        "tgt_mem": "",
        "op": "",
        "scheme": "",
        "runtime": "",
        "threads": 0,
        "direct": False,
        "flags": "",
        "total_buffer": None,
        "attach": "",
        "target_impl": None,
        "ns_type": None,
        "ns_lba_bs": None,
        "ns_bytes": None,
        "consistency": None,
        "path_observed": None,
        "status": "",
        "skip_reason": None,
        "blocked_by": [],
        "exit_init": None,
        "exit_tgt": None,
        "error_class": "",
        "error_excerpt": "",
        "duration_s": None,
        "rows_expected": None,
        "rows_seen": None,
        "load1_before": None,
        "load1_after": None,
        "steal_pct": None,
        "cmdline": None,
        "log_ref": None,
        "started_at": None,
    },
    "sample": {
        "kind": "sample",
        "case_uid": "",
        "block_size": 0,
        "batch_size": 0,
        "bw_gbps": None,
        "agg_bw_gbps": None,
        "net_util_pct": None,
        "lat_avg_us": None,
        "prep_avg_us": None,
        "prep_p99_us": None,
        "post_avg_us": None,
        "post_p99_us": None,
        "tx_avg_us": None,
        "tx_p99_us": None,
    },
    "counter": {
        "kind": "counter",
        "case_uid": "",
        "name": "",
        "value": 0,
        "unit": "",
        "deterministic": False,
    },
    "test": {
        "kind": "test",
        "run_uid": "",
        "suite": "",
        "test_id": "",
        "status": "",
        "duration_s": None,
        "sanitizer": None,
        "error_excerpt": "",
    },
}
FIELDS: dict[str, tuple[str, ...]] = {
    kind: tuple(blank) for kind, blank in _BLANK.items()
}


class BenchError(Exception):
    pass


class MatrixError(BenchError):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Profile:
    name: str
    flags: dict[str, FlagValue]
    when_vram: dict[str, FlagValue]
    rows: int | None


@dataclass(frozen=True)
class Case:
    lane: str
    index: int
    case_key: str
    workload_key: str
    profile: str
    backend: str
    engine: str
    transport: str
    init_mem: str
    tgt_mem: str
    op: str
    scheme: str
    runtime: str
    threads: int
    direct: bool
    flags: dict[str, FlagValue]
    env: dict[str, str]
    role: str
    required: bool
    tier: str
    sentinel: bool
    status: str
    blocked_by: tuple[str, ...]
    attach: str
    needs: tuple[str, ...]
    timeout_s: int
    pair_key: str
    pair_role: str


@dataclass(frozen=True)
class Pair:
    key: str
    kind: str
    num: str
    den: str


@dataclass(frozen=True)
class Lane:
    name: str
    lane_id: str
    env_class: str
    timing_trust: str
    topology: str
    status: str
    blocked_by: tuple[str, ...]
    aa_case: str
    description: str
    cases: tuple[Case, ...]
    pairs: tuple[Pair, ...]


@dataclass(frozen=True)
class Matrix:
    root: pathlib.Path
    profiles: dict[str, Profile]
    lanes: dict[str, Lane]


# --- matrix loading -------------------------------------------------------

_CASE_STATUS = ("active", "blocked")
_KEY_CHARS = r"[A-Za-z0-9._+/-]+"
_AXIS_RE = re.compile(r"[a-z][a-z0-9_]*")
_AXIS_REF_RE = re.compile(r"\{([a-z][a-z0-9_]*)\}")
_PLACEHOLDER_RE = re.compile(r"\{[A-Za-z_][A-Za-z0-9_.]*\}")
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_CASE_ENUMS: dict[str, tuple[str, ...]] = {
    "backend": BACKENDS,
    "transport": TRANSPORTS,
    "init_mem": INIT_MEMS,
    "tgt_mem": TGT_MEMS,
    "op": OPS,
    "scheme": SCHEMES,
    "runtime": RUNTIMES,
    "role": ROLES,
    "tier": TIERS,
    "status": _CASE_STATUS,
    "attach": ATTACH,
}
_CASE_STRINGS = ("key", "workload", "profile", "engine")
_CASE_BOOLS = ("direct", "required", "sentinel")
_CASE_INTS = ("threads", "timeout_s")
_CASE_TABLES = ("flags", "env")
_CASE_LISTS = ("blocked_by", "needs")
_DEFAULT_KEYS = frozenset(
    tuple(_CASE_ENUMS)
    + ("profile", "engine")
    + _CASE_BOOLS
    + _CASE_INTS
    + _CASE_TABLES
    + _CASE_LISTS
)
_SCALAR_KEYS = _DEFAULT_KEYS - frozenset(_CASE_TABLES + _CASE_LISTS)
_CASE_KEYS = _DEFAULT_KEYS | {"key", "workload", "axes", "when"}
_LANE_KEYS = frozenset(
    (
        "schema",
        "lane",
        "lane_id",
        "env_class",
        "timing_trust",
        "topology",
        "status",
        "blocked_by",
        "aa_case",
        "description",
        "defaults",
        "case",
        "pair",
    )
)
_MEM_LETTERS = {"D": "DRAM", "V": "VRAM"}


class _Report:
    """The problems of one file, each prefixed '<file>: <case_key or ->: '."""

    def __init__(self, problems: list[str], file: str) -> None:
        self.problems = problems
        self.file = file
        self.count = 0

    def add(self, where: str, text: str) -> None:
        self.problems.append(f"{self.file}: {where or '-'}: {text}")
        self.count += 1


def _read_toml(path: pathlib.Path, report: _Report) -> Optional[dict[str, Any]]:
    try:
        with open(path, "rb") as stream:
            return tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        report.add("", f"cannot read: {exc}")
        return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_str_list(value: Any) -> bool:
    return isinstance(value, list) and all(
        isinstance(item, str) and item for item in value
    )


def _unknown_keys(
    table: Mapping[str, Any], allowed: frozenset[str], label: str
) -> list[str]:
    return [f"unknown key '{key}' in {label}" for key in table if key not in allowed]


def _check_flags(
    flags: Any, label: str, where: str, report: _Report
) -> dict[str, FlagValue]:
    if not isinstance(flags, dict):
        report.add(where, f"{label} must be a table")
        return {}
    checked: dict[str, FlagValue] = {}
    for name, value in flags.items():
        want = NIXLBENCH_FLAGS.get(name)
        if name in RESERVED_FLAGS:
            report.add(where, f"{label}: flag '{name}' is set by a case dimension")
        elif want is None:
            report.add(where, f"{label}: unknown flag '{name}'")
        elif want is bool and not isinstance(value, bool):
            report.add(where, f"{label}: flag '{name}' must be a boolean")
        elif want is str and not isinstance(value, str):
            report.add(where, f"{label}: flag '{name}' must be a string")
        elif want is int and not (
            _is_int(value)
            or (isinstance(value, str) and _PLACEHOLDER_RE.fullmatch(value))
        ):
            report.add(
                where,
                f"{label}: flag '{name}' must be an integer or one placeholder",
            )
        else:
            checked[name] = value
    return checked


def _load_profile(path: pathlib.Path, report: _Report) -> Optional[Profile]:
    data = _read_toml(path, report)
    if data is None:
        return None
    before = report.count
    amd = data.pop("amd", {})
    if not isinstance(amd, dict):
        report.add("", "[amd] must be a table")
        amd = {}
    for text in _unknown_keys(
        amd, frozenset(("name", "rows", "description", "when_vram")), "[amd]"
    ):
        report.add("", text)
    if amd.get("name") != path.stem:
        report.add("", f"[amd] name must be '{path.stem}', the file stem")
    rows = amd.get("rows")
    if rows is not None and not _is_int(rows):
        report.add("", "[amd] rows must be an integer")
        rows = None
    if not isinstance(amd.get("description", ""), str):
        report.add("", "[amd] description must be a string")
    flags = _check_flags(data, "profile", "", report)
    when_vram = _check_flags(amd.get("when_vram", {}), "[amd.when_vram]", "", report)
    if report.count == before and rows is not None:
        for label, merged in (
            ("", flags),
            (" with when_vram", {**flags, **when_vram}),
        ):
            try:
                seen = len(sweep(merged))
            except BenchError as exc:
                report.add("", f"{exc}")
                break
            if seen != rows:
                report.add("", f"[amd] rows is {rows} but the sweep{label} is {seen}")
    if report.count != before:
        return None
    return Profile(path.stem, flags, when_vram, rows)


def _merge(base: Mapping[str, Any], over: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in over.items():
        old = merged.get(key)
        if key in _CASE_TABLES and isinstance(old, dict) and isinstance(value, dict):
            merged[key] = {**old, **value}
        else:
            merged[key] = value
    return merged


def _subst(value: Any, axes: Mapping[str, Any], typed: bool = False) -> Any:
    if isinstance(value, str):
        whole = _AXIS_REF_RE.fullmatch(value)
        if typed and whole and whole.group(1) in axes:
            return axes[whole.group(1)]
        return _AXIS_REF_RE.sub(
            lambda m: str(axes[m.group(1)]) if m.group(1) in axes else m.group(0),
            value,
        )
    if isinstance(value, list):
        return [_subst(item, axes) for item in value]
    if isinstance(value, dict):
        return {key: _subst(item, axes) for key, item in value.items()}
    return value


def _subst_case(raw: Mapping[str, Any], axes: Mapping[str, Any]) -> dict[str, Any]:
    done: dict[str, Any] = {}
    for key, value in raw.items():
        if key == "flags" and isinstance(value, dict):
            # An integer axis keeps its type where the flag is an integer.
            done[key] = {
                name: _subst(item, axes, NIXLBENCH_FLAGS.get(name) is int)
                for name, item in value.items()
            }
        else:
            done[key] = _subst(value, axes, key in _CASE_INTS)
    return done


def _same(left: Any, right: Any) -> bool:
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    return str(left) == str(right)


def _matches(
    match: Mapping[str, Any], axes: Mapping[str, Any], raw: Mapping[str, Any]
) -> bool:
    for key, wanted in match.items():
        actual = axes[key] if key in axes else raw.get(key)
        options = wanted if isinstance(wanted, list) else [wanted]
        if not any(_same(actual, option) for option in options):
            return False
    return True


def _check_axes(entry: Mapping[str, Any], where: str, report: _Report) -> bool:
    before = report.count
    axes = entry.get("axes", {})
    if not isinstance(axes, dict):
        report.add(where, "axes must be a table")
        return False
    for name, values in axes.items():
        if not _AXIS_RE.fullmatch(name):
            report.add(where, f"axis name '{name}' must match [a-z][a-z0-9_]*")
        elif name in _CASE_KEYS and name not in _SCALAR_KEYS:
            report.add(where, f"axis '{name}' cannot be named like that case field")
        if (
            not isinstance(values, list)
            or not values
            or not all(isinstance(v, str) or _is_int(v) for v in values)
        ):
            report.add(
                where,
                f"axis '{name}' must be a non-empty list of strings or integers",
            )
        elif name == "mem" and not all(
            isinstance(v, str) and re.fullmatch("[DV][DV]", v) for v in values
        ):
            report.add(where, "axis 'mem' values must be two letters of D and V")
    whens = entry.get("when", [])
    if not isinstance(whens, list) or not all(isinstance(w, dict) for w in whens):
        report.add(where, "when must be an array of tables")
        return False
    for number, when in enumerate(whens):
        label = f"when[{number}]"
        for text in _unknown_keys(when, frozenset(("match", "set")), label):
            report.add(where, text)
        match, change = when.get("match"), when.get("set")
        if not isinstance(match, dict) or not match:
            report.add(where, f"{label} needs a match table")
        else:
            for key in match:
                if key not in axes and key not in _SCALAR_KEYS:
                    report.add(where, f"{label} matches '{key}', not an axis or field")
        if not isinstance(change, dict) or not change:
            report.add(where, f"{label} needs a set table")
        else:
            for text in _unknown_keys(change, _DEFAULT_KEYS, f"{label} set"):
                report.add(where, text)
    return report.count == before


def _expand(
    entry: Mapping[str, Any], defaults: Mapping[str, Any]
) -> Iterator[dict[str, Any]]:
    axes: dict[str, list[Any]] = entry.get("axes", {})
    authored = {k: v for k, v in entry.items() if k not in ("axes", "when")}
    for combo in itertools.product(*axes.values()):
        point = dict(zip(axes, combo))
        raw = _merge(defaults, authored)
        for name, value in point.items():
            if name == "mem":
                for field, letter in zip(("init_mem", "tgt_mem"), value):
                    if field not in authored:
                        raw[field] = _MEM_LETTERS[letter]
            elif name in _SCALAR_KEYS and name not in authored:
                raw[name] = value
        for when in entry.get("when", []):
            if _matches(when["match"], point, raw):
                raw = _merge(raw, when["set"])
        yield _subst_case(raw, point)


@dataclass
class _LaneInfo:
    name: str
    topology: str
    status: str
    blocked_by: tuple[str, ...]


def _build_case(
    raw: Mapping[str, Any],
    lane: _LaneInfo,
    profiles: Mapping[str, Profile],
    lane_names: frozenset[str],
    where: str,
    report: _Report,
) -> Optional[dict[str, Any]]:
    before = report.count
    for name in ("key", "workload", "profile", "backend", "op"):
        if name not in raw:
            report.add(where, f"'{name}' is required")
    for name in _CASE_STRINGS:
        if name in raw and not isinstance(raw[name], str):
            report.add(where, f"'{name}' must be a string")
    for name, allowed in _CASE_ENUMS.items():
        if name in raw and raw[name] not in allowed:
            report.add(where, f"'{name}' must be one of {', '.join(allowed)}")
    for name in _CASE_BOOLS:
        if not isinstance(raw.get(name, False), bool):
            report.add(where, f"'{name}' must be a boolean")
    for name in _CASE_INTS:
        if not _is_int(raw.get(name, 1)):
            report.add(where, f"'{name}' must be an integer")
    for name in _CASE_LISTS:
        if not _is_str_list(raw.get(name, [])):
            report.add(where, f"'{name}' must be a list of non-empty strings")
    flags = _check_flags(raw.get("flags", {}), "flags", where, report)
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
        _ENV_NAME_RE.fullmatch(k) and isinstance(v, str) for k, v in env.items()
    ):
        report.add(where, "'env' must be a table of names to strings")
    if report.count != before:
        return None

    backend = raw["backend"]
    paired = backend in PAIR_BACKENDS
    case_key, workload_key = raw["key"], raw["workload"]
    if not re.fullmatch(re.escape(lane.name) + "/" + _KEY_CHARS, case_key):
        report.add(where, f"key must match ^{lane.name}/[A-Za-z0-9._+/-]+$")
    if not re.fullmatch(_KEY_CHARS, workload_key):
        report.add(where, "workload must match ^[A-Za-z0-9._+/-]+$")
    elif workload_key.split("/", 1)[0] in lane_names:
        report.add(where, "workload must not start with a lane name")
    profile = profiles.get(raw["profile"])
    if profile is None:
        report.add(where, f"profile '{raw['profile']}' does not exist")
    if "tgt_mem" not in raw and not paired:
        report.add(where, f"'tgt_mem' is required for backend {backend}")
    runtime = raw.get("runtime", "ASIO" if paired else "NONE")
    if paired and runtime == "NONE":
        report.add(where, f"backend {backend} needs runtime ASIO or ETCD")
    elif not paired and runtime != "NONE":
        report.add(where, f"backend {backend} needs runtime NONE")
    threads, timeout_s = raw.get("threads", 1), raw.get("timeout_s", 300)
    if threads < 1:
        report.add(where, "'threads' must be at least 1")
    # Only a case no nightly runs may take longer than a nightly case.
    longest = TIMEOUT_MAX_WEEKLY if raw.get("tier") == "weekly" else TIMEOUT_MAX
    if not 1 <= timeout_s <= longest:
        report.add(where, f"'timeout_s' must be between 1 and {longest}")
    needs = set(raw.get("needs", []))
    for name in sorted(needs - set(NEEDS)):
        report.add(where, f"unknown need '{name}'")
    status, blocked_by = raw.get("status", "active"), list(raw.get("blocked_by", []))
    if (status == "blocked") != bool(blocked_by):
        report.add(where, "status is 'blocked' if and only if blocked_by is set")
    if lane.status == "blocked":
        status = "blocked"
        blocked_by = list(lane.blocked_by) + [
            item for item in blocked_by if item not in lane.blocked_by
        ]
    if report.count != before or profile is None:
        return None

    init_mem = raw.get("init_mem", "DRAM")
    tgt_mem = raw.get("tgt_mem", "DRAM")
    transport = raw.get("transport", "none")
    if "VRAM" in (init_mem, tgt_mem) or backend == "AIS":
        needs.add("gpu")
    if transport in ("rc", "ud", "verbs"):
        needs.add("rdma")
    if lane.topology == "two-host" and paired:
        needs.add("two_hosts")
    built = {
        "lane": lane.name,
        "case_key": case_key,
        "workload_key": workload_key,
        "profile": profile.name,
        "backend": backend,
        "engine": raw.get("engine", ""),
        "transport": transport,
        "init_mem": init_mem,
        "tgt_mem": tgt_mem,
        "op": raw["op"],
        "scheme": raw.get("scheme", "pairwise"),
        "runtime": runtime,
        "threads": threads,
        "direct": raw.get("direct", False),
        "flags": flags,
        "env": dict(env),
        "role": raw.get("role", "info"),
        "required": raw.get("required", False),
        "tier": raw.get("tier", "nightly"),
        "sentinel": raw.get("sentinel", False),
        "status": status,
        "blocked_by": tuple(blocked_by),
        "attach": raw.get("attach", "none"),
        "needs": tuple(sorted(needs)),
        "timeout_s": timeout_s,
    }
    try:
        sweep(
            effective_flags(Case(index=0, pair_key="", pair_role="", **built), profile)
        )
    except BenchError as exc:
        report.add(where, f"{exc}")
        return None
    return built


def _load_pairs(
    entries: Any, keys: frozenset[str], pair_keys: set[str], report: _Report
) -> list[Pair]:
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        report.add("", "pair must be an array of tables")
        return []
    pairs = []
    for number, entry in enumerate(entries):
        key = entry.get("key")
        where = key if isinstance(key, str) and key else f"pair[{number}]"
        before = report.count
        for text in _unknown_keys(
            entry, frozenset(("key", "kind", "num", "den")), "pair"
        ):
            report.add(where, text)
        for name in ("key", "kind", "num", "den"):
            if not isinstance(entry.get(name), str) or not entry[name]:
                report.add(where, f"pair '{name}' must be a non-empty string")
        if report.count != before:
            continue
        if not re.fullmatch(_KEY_CHARS, entry["key"]):
            report.add(where, "pair key must match ^[A-Za-z0-9._+/-]+$")
        elif entry["key"] in pair_keys:
            report.add(where, "pair key is not unique")
        pair_keys.add(entry["key"])
        if entry["kind"] not in PAIR_KINDS:
            report.add(where, f"pair kind must be one of {', '.join(PAIR_KINDS)}")
        for name in ("num", "den"):
            if entry[name] not in keys:
                report.add(where, f"pair {name} '{entry[name]}' is not in this lane")
        if entry["num"] == entry["den"]:
            report.add(where, "pair num and den must differ")
        if report.count == before:
            pairs.append(Pair(entry["key"], entry["kind"], entry["num"], entry["den"]))
    return pairs


def _load_lane(
    path: pathlib.Path,
    profiles: Mapping[str, Profile],
    lane_names: frozenset[str],
    case_keys: dict[str, str],
    pair_keys: set[str],
    report: _Report,
) -> Optional[Lane]:
    data = _read_toml(path, report)
    if data is None:
        return None
    before = report.count
    for text in _unknown_keys(data, _LANE_KEYS, "the lane"):
        report.add("", text)
    if data.get("schema") != SCHEMA or isinstance(data.get("schema"), bool):
        report.add("", f"schema must be {SCHEMA}")
    if data.get("lane") != path.stem:
        report.add("", f"lane must be '{path.stem}', the file stem")
    for name in ("lane_id", "env_class"):
        if not isinstance(data.get(name), str) or not data[name]:
            report.add("", f"'{name}' must be a non-empty string")
    for name, allowed in (
        ("timing_trust", TIMING_TRUST),
        ("topology", TOPOLOGIES),
        ("status", _CASE_STATUS),
    ):
        if data.get(name, "active" if name == "status" else None) not in allowed:
            report.add("", f"'{name}' must be one of {', '.join(allowed)}")
    for name in ("aa_case", "description"):
        if not isinstance(data.get(name, ""), str):
            report.add("", f"'{name}' must be a string")
    status = data.get("status", "active")
    blocked_by = data.get("blocked_by", [])
    if not _is_str_list(blocked_by):
        report.add("", "'blocked_by' must be a list of non-empty strings")
        blocked_by = []
    elif (status == "blocked") != bool(blocked_by):
        report.add("", "status is 'blocked' if and only if blocked_by is set")
    defaults = data.get("defaults", {})
    if not isinstance(defaults, dict):
        report.add("", "[defaults] must be a table")
        defaults = {}
    for text in _unknown_keys(defaults, _DEFAULT_KEYS, "[defaults]"):
        report.add("", text)
    entries = data.get("case", [])
    if not isinstance(entries, list) or not all(isinstance(e, dict) for e in entries):
        report.add("", "case must be an array of tables")
        entries = []
    info = _LaneInfo(
        path.stem, str(data.get("topology")), str(status), tuple(blocked_by)
    )

    built: list[dict[str, Any]] = []
    declared: set[str] = set()
    for number, entry in enumerate(entries):
        key = entry.get("key")
        label = key if isinstance(key, str) and key else f"case[{number}]"
        unknown = _unknown_keys(entry, _CASE_KEYS, "the case")
        for text in unknown:
            report.add(label, text)
        if unknown or not _check_axes(entry, label, report):
            continue
        for raw in _expand(entry, defaults):
            key = raw.get("key")
            where = key if isinstance(key, str) and key else label
            if isinstance(key, str):
                if key in declared or key in case_keys:
                    other = case_keys.get(key, report.file)
                    report.add(where, f"case key is not unique (also in {other})")
                    continue
                declared.add(key)
            case = _build_case(raw, info, profiles, lane_names, where, report)
            if case is not None:
                built.append(case)
    case_keys.update((key, report.file) for key in declared)

    pairs = _load_pairs(data.get("pair", []), frozenset(declared), pair_keys, report)
    by_key = {case["case_key"]: case for case in built}
    aa_case = data.get("aa_case", "")
    if isinstance(aa_case, str) and aa_case:
        target = by_key.get(aa_case)
        if target is None:
            if aa_case not in declared:
                report.add("", f"aa_case '{aa_case}' is not a case of this lane")
        elif target["status"] != "active" or target["tier"] not in ("pr", "nightly"):
            report.add("", "aa_case must be an active case of tier pr or nightly")
    if report.count != before:
        return None

    cases = []
    for index, case in enumerate(built):
        roles = sorted(
            (pair.key, role)
            for pair in pairs
            for role, key in (("num", pair.num), ("den", pair.den))
            if key == case["case_key"]
        )
        cases.append(
            Case(
                index=index,
                pair_key=",".join(key for key, _ in roles),
                pair_role=",".join(role for _, role in roles),
                **case,
            )
        )
    return Lane(
        name=path.stem,
        lane_id=data["lane_id"],
        env_class=data["env_class"],
        timing_trust=data["timing_trust"],
        topology=data["topology"],
        status=status,
        blocked_by=tuple(blocked_by),
        aa_case=aa_case,
        description=data.get("description", ""),
        cases=tuple(cases),
        pairs=tuple(pairs),
    )


def load_matrix(root: pathlib.Path) -> Matrix:
    """The matrix under root, or MatrixError listing every structural problem."""
    root = pathlib.Path(root)
    problems: list[str] = []
    profiles: dict[str, Profile] = {}
    lanes: dict[str, Lane] = {}
    profile_files = sorted((root / "profiles").glob("*.toml"))
    lane_files = sorted((root / "lanes").glob("*.toml"))
    if not profile_files:
        problems.append(f"{root}/profiles: -: no profile files")
    if not lane_files:
        problems.append(f"{root}/lanes: -: no lane files")
    for path in profile_files:
        profile = _load_profile(path, _Report(problems, f"profiles/{path.name}"))
        if profile is not None:
            profiles[profile.name] = profile
    lane_names = frozenset(path.stem for path in lane_files)
    case_keys: dict[str, str] = {}
    pair_keys: set[str] = set()
    for path in lane_files:
        lane = _load_lane(
            path,
            profiles,
            lane_names,
            case_keys,
            pair_keys,
            _Report(problems, f"lanes/{path.name}"),
        )
        if lane is not None:
            lanes[lane.name] = lane
    if problems:
        raise MatrixError(problems)
    return Matrix(root, profiles, lanes)


def select_cases(lane: Lane, tier: str) -> list[Case]:
    """The cases a tier runs: its own and those of every earlier tier."""
    if tier not in TIERS:
        raise BenchError(f"unknown tier '{tier}'")
    limit = TIERS.index(tier)
    return [case for case in lane.cases if TIERS.index(case.tier) <= limit]


# --- flags, ids and the manifest ------------------------------------------


def flag_order(flags: Mapping[str, FlagValue]) -> list[str]:
    lead = [name for name in DIMENSION_FLAGS if name in flags]
    return lead + sorted(name for name in flags if name not in DIMENSION_FLAGS)


def effective_flags(case: Case, profile: Profile) -> dict[str, FlagValue]:
    """The nixlbench flags of a case, runner placeholders left in place."""
    flags: dict[str, FlagValue] = dict(profile.flags)
    if "VRAM" in (case.init_mem, case.tgt_mem):
        flags.update(profile.when_vram)
    flags.update(case.flags)
    flags.update(
        backend=case.backend,
        op_type=case.op,
        initiator_seg_type=case.init_mem,
        scheme=case.scheme,
        num_threads=case.threads,
    )
    if case.tgt_mem in ("DRAM", "VRAM"):
        flags["target_seg_type"] = case.tgt_mem
    if case.runtime == "ASIO":
        flags.update(
            runtime_type="ASIO",
            asio_address="{asio_address}",
            asio_port="{asio_port}",
        )
    elif case.runtime == "ETCD":
        flags.update(
            runtime_type="ETCD",
            etcd_endpoints="{etcd_endpoints}",
            benchmark_group="{benchmark_group}",
        )
    else:
        flags["storage_enable_direct"] = case.direct
    return {name: flags[name] for name in flag_order(flags)}


def sweep(flags: Mapping[str, FlagValue]) -> list[tuple[int, int]]:
    """(block, batch) of every table row, in the order nixlbench prints them."""
    bounds = []
    for name, default in (
        ("start_block_size", 4096),
        ("max_block_size", 64 << 20),
        ("start_batch_size", 1),
        ("max_batch_size", 1),
    ):
        value = flags.get(name, default)
        if not _is_int(value) or int(value) < 1:
            raise BenchError(f"{name} must be a positive integer")
        bounds.append(int(value))
    rows = []
    block = bounds[0]
    while block <= bounds[1]:
        batch = bounds[2]
        while batch <= bounds[3]:
            rows.append((block, batch))
            batch *= 2
        block *= 2
    return rows


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _sha256(obj: Any) -> str:
    return hashlib.sha256(canonical_json(obj).encode("ascii")).hexdigest()


def params_sha(case: Case, profile: Profile) -> str:
    return _sha256({"v": 1, "flags": effective_flags(case, profile), "env": case.env})


def run_uid(
    repo: str, bench_run_id: str, run_attempt: int, lane: str, rocm_version: str
) -> str:
    return _sha256(["run", 1, repo, bench_run_id, run_attempt, lane, rocm_version])[:20]


def case_uid(run_uid_: str, case_key: str, variant: str, round_: int) -> str:
    return _sha256(["case", 1, run_uid_, case_key, variant, round_])[:20]


def env_fingerprint(run: Mapping[str, Any]) -> str:
    return _sha256(["env", 1, {key: run.get(key) for key in ENV_FP_KEYS}])[:20]


def matrix_manifest(matrix: Matrix) -> dict[str, Any]:
    """What a run of this matrix is expected to produce, per tier and lane."""
    lanes: dict[str, Any] = {}
    for name, lane in matrix.lanes.items():
        cases = []
        for case in lane.cases:
            profile = matrix.profiles[case.profile]
            cases.append(
                {
                    "case_key": case.case_key,
                    "workload_key": case.workload_key,
                    "profile": case.profile,
                    "tier": case.tier,
                    "role": case.role,
                    "required": case.required,
                    "sentinel": case.sentinel,
                    "status": case.status,
                    "blocked_by": list(case.blocked_by),
                    "params_sha": params_sha(case, profile),
                    "rows_expected": len(sweep(effective_flags(case, profile))),
                }
            )
        lanes[name] = {
            "lane_id": lane.lane_id,
            "env_class": lane.env_class,
            "timing_trust": lane.timing_trust,
            "topology": lane.topology,
            "status": lane.status,
            "blocked_by": list(lane.blocked_by),
            "aa_case": lane.aa_case,
            "cases": cases,
            "pairs": [dataclasses.asdict(pair) for pair in lane.pairs],
        }
    return {
        "schema": SCHEMA,
        "profiles": {
            name: {
                "flags": dict(profile.flags),
                "when_vram": dict(profile.when_vram),
                "rows": profile.rows,
            }
            for name, profile in matrix.profiles.items()
        },
        "lanes": lanes,
        "tiers": {
            tier: {
                name: sorted(case.case_key for case in select_cases(lane, tier))
                for name, lane in matrix.lanes.items()
            }
            for tier in TIERS
        },
    }


def matrix_sha256(manifest: Mapping[str, Any]) -> str:
    return _sha256(manifest)


# --- records --------------------------------------------------------------


def blank_record(kind: str) -> dict[str, Any]:
    if kind not in _BLANK:
        raise BenchError(f"unknown record kind '{kind}'")
    return {
        key: list(value) if isinstance(value, list) else value
        for key, value in _BLANK[kind].items()
    }


def _flags_text(case: Case) -> str:
    parts = []
    for name, value in sorted({**case.flags, **case.env}.items()):
        if name in SECRET_FLAGS or _SECRET_ENV_NAME_RE.fullmatch(name):
            text = "***"
        elif isinstance(value, bool):
            text = "true" if value else "false"
        else:
            text = str(value)
        parts.append(f"{name}={text}")
    return " ".join(parts)


def case_record(
    case: Case,
    profile: Profile,
    run_uid_: str,
    *,
    variant: str = "head",
    round_: int = 0,
) -> dict[str, Any]:
    """A case record with every matrix-derived field set and nothing run."""
    flags = effective_flags(case, profile)
    total = flags.get("total_buffer_size")
    record = blank_record("case")
    record.update(
        run_uid=run_uid_,
        case_uid=case_uid(run_uid_, case.case_key, variant, round_),
        case_key=case.case_key,
        workload_key=case.workload_key,
        pair_key=case.pair_key,
        pair_role=case.pair_role,
        params_sha=params_sha(case, profile),
        profile=case.profile,
        role=case.role,
        required=case.required,
        tier=case.tier,
        variant=variant,
        round=round_,
        backend=case.backend,
        engine=case.engine,
        transport_req=case.transport,
        init_mem=case.init_mem,
        tgt_mem=case.tgt_mem,
        op=case.op,
        scheme=case.scheme,
        runtime=case.runtime,
        threads=case.threads,
        direct=case.direct,
        flags=_flags_text(case),
        total_buffer=total if _is_int(total) else None,
        attach=case.attach,
        status="blocked" if case.status == "blocked" else "not-run",
        blocked_by=list(case.blocked_by),
        rows_expected=len(sweep(flags)),
    )
    return record


# --- secrets --------------------------------------------------------------


def _redact_line(line: str) -> str:
    body = line.rstrip("\r")
    # The config line first. Its description names the flag ("(--flag=key)"),
    # and where nixlbench does not pad it the flag rule would swallow the ": "
    # that marks the value and leave the value in clear.
    masked = _REDACT_CONFIG_RE.sub(
        lambda m: m.group(1)
        + _CONFIG_HINT_RE.sub("=***)", m.group(2))
        + (": ***" if m.group(3) else ": "),
        body,
    )
    masked = _REDACT_FLAG_RE.sub(r"\1\2\3***", masked)
    masked = _REDACT_ENV_RE.sub(r"\1=***", masked)
    return _REDACT_URL_RE.sub(r"\1:***@", masked) + line[len(body) :]


def redact_text(text: str) -> str:
    """text with the values of secret flags, variables and config lines masked."""
    return "\n".join(_redact_line(line) for line in text.split("\n"))


def clip(text: str, limit: int = EXCERPT_MAX) -> str:
    """Redacted text cut to limit characters, still redacted after the cut."""
    cut = text[:limit]
    # A cut inside a mask leaves "name=*", which a later redaction would
    # lengthen again and a secret scan would take for a value.
    while len(text) > limit and redact_text(cut) != cut:
        cut = cut[:-1]
    return cut


def redact_argv(argv: Sequence[str]) -> list[str]:
    redacted = []
    mask = False
    for token in argv:
        redacted.append("***" if mask else redact_text(token))
        mask = not mask and token.lstrip("-") in SECRET_FLAGS and token.startswith("-")
    return redacted


def find_secrets(value: Any) -> list[tuple[str, str]]:
    """(JSON path, pattern name) of every secret-looking string; never the text."""
    found: list[tuple[str, str]] = []

    def scan(text: str, path: str) -> bool:
        hits = [name for name, pattern in _SECRET_PATTERNS if pattern.search(text)]
        found.extend((path, name) for name in hits)
        return bool(hits)

    def walk(item: Any, path: str) -> None:
        if isinstance(item, str):
            scan(item, path)
        elif isinstance(item, Mapping):
            for number, (key, child) in enumerate(item.items()):
                # A key that is itself a hit is reported by position, not name.
                hit = scan(str(key), f"{path}.<key {number}>")
                walk(child, f"{path}.<key {number}>" if hit else f"{path}.{key}")
        elif isinstance(item, (list, tuple)):
            for number, child in enumerate(item):
                walk(child, f"{path}[{number}]")

    walk(value, "$")
    return found


# --- schema ---------------------------------------------------------------

_TOP_KEYWORDS = frozenset(
    (
        "$schema",
        "$id",
        "title",
        "description",
        "oneOf",
        "x-schema-version",
        "x-schema-minor",
        "$defs",
    )
)
_KEYWORDS = frozenset(
    (
        "type",
        "enum",
        "const",
        "required",
        "properties",
        "additionalProperties",
        "items",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "pattern",
        "$ref",
    )
)
_JSON_TYPES = ("object", "array", "string", "integer", "number", "boolean", "null")
_REF_PREFIX = "#/$defs/"


def _check_schema_node(node: Any, defs: Mapping[str, Any], path: str) -> None:
    if not isinstance(node, dict):
        raise BenchError(f"schema: {path} is not an object")
    for keyword in node:
        if keyword not in _KEYWORDS:
            raise BenchError(f"schema: unsupported keyword '{keyword}' at {path}")
    types = node.get("type", [])
    for name in [types] if isinstance(types, str) else types:
        if name not in _JSON_TYPES:
            raise BenchError(f"schema: unknown type '{name}' at {path}")
    if "$ref" in node:
        ref = node["$ref"]
        if not isinstance(ref, str) or not ref.startswith(_REF_PREFIX):
            raise BenchError(f"schema: $ref at {path} must be {_REF_PREFIX}<name>")
        if ref[len(_REF_PREFIX) :] not in defs:
            raise BenchError(f"schema: $ref at {path} names no definition")
    if not isinstance(node.get("additionalProperties", False), bool):
        raise BenchError(f"schema: additionalProperties at {path} must be a boolean")
    if "pattern" in node:
        try:
            re.compile(node["pattern"])
        except (re.error, TypeError) as exc:
            raise BenchError(f"schema: bad pattern at {path}: {exc}") from exc
    for name, child in node.get("properties", {}).items():
        _check_schema_node(child, defs, f"{path}.{name}")
    if "items" in node:
        _check_schema_node(node["items"], defs, f"{path}[]")


def load_schema(path: pathlib.Path | None = None) -> dict[str, Any]:
    """The record schema, refused if it uses a keyword validate_record lacks."""
    if path is None:
        path = pathlib.Path(__file__).resolve().parent.parent / "bench/schema/v1.json"
    try:
        schema = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchError(f"cannot read schema {path}: {exc}") from exc
    if not isinstance(schema, dict) or not isinstance(schema.get("$defs"), dict):
        raise BenchError(f"schema {path} has no $defs")
    for keyword in schema:
        if keyword not in _TOP_KEYWORDS:
            raise BenchError(f"schema: unsupported keyword '{keyword}' at the top")
    if schema.get("x-schema-version") != SCHEMA:
        raise BenchError(f"schema {path} is not version {SCHEMA}")
    for name, node in schema["$defs"].items():
        _check_schema_node(node, schema["$defs"], name)
    for number, node in enumerate(schema.get("oneOf", [])):
        _check_schema_node(node, schema["$defs"], f"oneOf[{number}]")
    return schema


def _json_type(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object" if isinstance(value, dict) else type(value).__name__


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _json_equal(left: Any, right: Any) -> bool:
    if _is_number(left) and _is_number(right):
        return bool(left == right)
    return type(left) is type(right) and bool(left == right)


def _validate(
    value: Any, node: Mapping[str, Any], defs: Mapping[str, Any], path: str
) -> list[str]:
    """Problems of value against node. A message never quotes the value."""
    if "$ref" in node:
        node = defs[node["$ref"][len(_REF_PREFIX) :]]
    actual = _json_type(value)
    if isinstance(value, float) and not math.isfinite(value):
        # 1e999 parses as infinity, which no JSON writer can emit again.
        return [f"{path}: not a finite number"]
    if "type" in node:
        allowed = [node["type"]] if isinstance(node["type"], str) else node["type"]
        if actual not in allowed and not (actual == "integer" and "number" in allowed):
            return [f"{path}: expected {' or '.join(allowed)}, got {actual}"]
    errors = []
    if "const" in node and not _json_equal(value, node["const"]):
        errors.append(f"{path}: must be {json.dumps(node['const'])}")
    if "enum" in node and not any(_json_equal(value, item) for item in node["enum"]):
        errors.append(f"{path}: must be one of {json.dumps(node['enum'])}")
    if _is_number(value):
        if "minimum" in node and value < node["minimum"]:
            errors.append(f"{path}: must be at least {node['minimum']}")
        if "maximum" in node and value > node["maximum"]:
            errors.append(f"{path}: must be at most {node['maximum']}")
    elif isinstance(value, str):
        if "minLength" in node and len(value) < node["minLength"]:
            errors.append(f"{path}: shorter than {node['minLength']} characters")
        if "maxLength" in node and len(value) > node["maxLength"]:
            errors.append(f"{path}: longer than {node['maxLength']} characters")
        # ASCII digits only, and '$' is the end, not the place before a newline.
        if "pattern" in node and (
            not re.search(node["pattern"], value, re.ASCII) or value.endswith("\n")
        ):
            errors.append(f"{path}: does not match {node['pattern']}")
    elif isinstance(value, list):
        if "minItems" in node and len(value) < node["minItems"]:
            errors.append(f"{path}: fewer than {node['minItems']} items")
        if "items" in node:
            for number, item in enumerate(value):
                errors += _validate(item, node["items"], defs, f"{path}[{number}]")
    elif isinstance(value, dict):
        properties = node.get("properties", {})
        errors += [
            f"{path}: missing key '{key}'"
            for key in node.get("required", [])
            if key not in value
        ]
        if node.get("additionalProperties") is False:
            errors += [
                f"{path}: unexpected key '{str(key)[:40]}'"
                for key in sorted(value, key=str)
                if key not in properties
            ]
        for key, child in properties.items():
            if key in value:
                errors += _validate(value[key], child, defs, f"{path}.{key}")
    return errors


def validate_record(record: Mapping[str, Any], schema: Mapping[str, Any]) -> list[str]:
    """Why record is not a valid schema v1 record; empty when it is one."""
    if not isinstance(record, Mapping):
        return ["$: expected object"]
    kind = record.get("kind")
    if not isinstance(kind, str) or kind not in FIELDS or kind not in schema["$defs"]:
        return ["$.kind: unknown record kind"]
    return _validate(dict(record), schema["$defs"][kind], schema["$defs"], "$")


# --- NDJSON ---------------------------------------------------------------


def _no_constant(name: str) -> Any:
    raise ValueError(f"{name} is not JSON")


def _too_deep(value: Any, limit: int = 8) -> bool:
    """Records are flat; anything deeper would only exhaust a recursive reader."""
    pending = [(value, 1)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, (dict, list)):
            if depth > limit:
                return True
            children = item.values() if isinstance(item, dict) else item
            pending.extend((child, depth + 1) for child in children)
    return False


def read_ndjson(
    path: pathlib.Path,
) -> Iterator[tuple[int, dict[str, Any] | None, str]]:
    """(line number, record or None, error text) for each non-empty line."""
    with open(path, encoding="utf-8", errors="replace") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                record = json.loads(line, parse_constant=_no_constant)
            except (ValueError, RecursionError) as exc:
                yield number, None, f"not JSON: {exc}"
                continue
            if not isinstance(record, dict):
                yield number, None, "not a JSON object"
            elif _too_deep(record):
                yield number, None, "not JSON: nested too deeply"
            else:
                yield number, record, ""


class NdjsonWriter:
    """Appends one record per line; the file is valid NDJSON after each write."""

    def __init__(self, path: pathlib.Path) -> None:
        self.path = pathlib.Path(path)
        torn = False
        if self.path.is_file() and self.path.stat().st_size:
            with open(self.path, "rb") as stream:
                stream.seek(-1, os.SEEK_END)
                torn = stream.read(1) != b"\n"
        self._stream: Optional[IO[bytes]] = open(self.path, "ab")
        if torn:
            # A writer that died mid-line must not corrupt the next record.
            self._stream.write(b"\n")

    def write(self, record: Mapping[str, Any]) -> None:
        if self._stream is None:
            raise BenchError(f"{self.path} is closed")
        line = json.dumps(
            record, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        )
        self._stream.write(line.encode("ascii") + b"\n")
        self._stream.flush()
        os.fsync(self._stream.fileno())

    def close(self) -> None:
        if self._stream is not None:
            self._stream.close()
            self._stream = None

    def __enter__(self) -> NdjsonWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()
