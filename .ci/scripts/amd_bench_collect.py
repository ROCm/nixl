#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Collector of the AMD nixlbench lanes: lane artifacts in, one checked set out.

    amd_bench_collect.py [--matrix DIR] --input DIR [--input DIR]... --out DIR
        [--run-meta FILE] [--expect LANE=ROCM[,ROCM...]]... [--zstd auto|never]
        [--summary-out FILE]

Reads every records.ndjson under the inputs (one per lane job, with logs/
beside it), drops what is not a valid schema v1 record or looks like a secret,
and adds a 'blocked' or 'not-run' case record for each case the matrix expects
and no lane reported. A lane named by --expect that sent nothing gets a run
record with status 'missing'. Writes to --out:

    bench-<stamp>-<slug>-<bench_run_id>.a<attempt>.ndjson.gz   (and .zst)
    bench-<stamp>-<slug>-<bench_run_id>.a<attempt>.logs.tar.gz
    matrix-<matrix_sha256>.json, collect.json, summary.md

Exits 0 when clean, 1 when a required case did not pass, 2 on bad usage,
matrix or run-meta (or nothing to collect), 3 when a record or log was
rejected, a run used another matrix, or the collector itself failed.
Standard library only.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tarfile
import traceback
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping, Optional

try:
    import amd_bench_lib as lib
except ImportError:
    sys.exit("amd_bench_lib.py must sit beside this script")

IDENTITY_KEYS = (
    "stamp",
    "nightly_run_id",
    "bench_run_id",
    "run_attempt",
    "reason",
    "repo",
    "branch",
    "slug",
    "sha",
    "upstream_main_sha",
    "nixl_version",
    "nixlbench_version",
    "features",
    "merge_tree",
    "baseline_sha",
    "tier",
)
SUMMARY_CASES = 300
EXCERPT_CHARS = 120


@dataclass
class Run:
    record: dict[str, Any]
    source: Optional[pathlib.Path] = None
    cases: list[dict[str, Any]] = field(default_factory=list)
    tests: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Intake:
    """Everything read, before the records are linked to each other."""

    runs: dict[str, Run] = field(default_factory=dict)
    cases: dict[str, tuple[dict[str, Any], str, int]] = field(default_factory=dict)
    tests: list[tuple[dict[str, Any], str, int]] = field(default_factory=list)
    rows: dict[str, list[tuple[dict[str, Any], str, int]]] = field(default_factory=dict)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def reject(self, file: str, line: int, kind: str, reason: str) -> None:
        self.rejected.append(
            {"file": file, "line": line, "kind": kind, "reason": reason}
        )


def safe(text: Any) -> str:
    return re.sub(r"[^A-Za-z0-9._+-]", "_", str(text))


def warn(message: str) -> None:
    sys.stderr.write(f"amd_bench_collect.py: {message}\n")


def log_prefix(run: Mapping[str, Any]) -> str:
    return f"{safe(run['lane'])}-{safe(run['rocm_version'])}"


def count_statuses(run: Run) -> dict[str, int]:
    counts = dict.fromkeys(lib.CASE_STATUSES, 0)
    for case in run.cases:
        counts[case["status"]] += 1
    return counts


def files_under(root: pathlib.Path) -> Iterator[pathlib.Path]:
    """Regular files under root, sorted; symbolic links are not followed."""
    for directory, names, files in os.walk(root, followlinks=False):
        names.sort()
        for name in sorted(files):
            path = pathlib.Path(directory, name)
            if path.is_file() and not path.is_symlink():
                yield path


def read_records(
    path: pathlib.Path, label: str, schema: Mapping[str, Any], intake: Intake
) -> None:
    redacted = 0
    for number, record, _error in lib.read_ndjson(path):
        if record is None:
            intake.reject(label, number, "", "json")
            continue
        kind = record.get("kind")
        kind = kind if isinstance(kind, str) and kind in lib.FIELDS else ""
        for name in ("cmdline", "error_excerpt"):
            if isinstance(record.get(name), str):
                clean = lib.redact_text(record[name])
                redacted += clean != record[name]
                # Masking can lengthen an excerpt that was cut inside a value.
                record[name] = lib.clip(clean) if name == "error_excerpt" else clean
        secrets = lib.find_secrets(record)
        if secrets:
            intake.reject(label, number, kind, f"secret:{secrets[0][1]}")
            continue
        errors = lib.validate_record(record, schema)
        if errors:
            intake.reject(label, number, kind, f"schema:{errors[0]}")
            continue
        record = {name: record[name] for name in lib.FIELDS[kind]}
        if kind == "run":
            uid = record["run_uid"]
            old = intake.runs.get(uid)
            if old is not None and old.source not in (None, path.parent):
                intake.warnings.append(
                    f"run {uid} is in more than one artifact; kept {label}"
                )
            intake.runs[uid] = Run(record, path.parent)
        elif kind == "case":
            uid = record["case_uid"]
            if uid in intake.cases:
                same = intake.cases[uid][0] == record
                intake.warnings.append(
                    f"{'duplicate' if same else 'conflicting'} case record for "
                    f"{record['case_key']} ({uid}) at {label}:{number}; last kept"
                )
                # The rows of the superseded record are not the kept one's.
                intake.rows.pop(uid, None)
            intake.cases[uid] = (record, label, number)
        elif kind == "test":
            intake.tests.append((record, label, number))
        else:
            intake.rows.setdefault(record["case_uid"], []).append(
                (record, label, number)
            )
    if redacted:
        intake.warnings.append(f"{redacted} field(s) in {label} needed redaction")


def read_inputs(inputs: list[pathlib.Path], schema: Mapping[str, Any]) -> Intake:
    intake = Intake()
    for root in inputs:
        if not root.is_dir():
            intake.warnings.append(f"input {root} is not a directory")
            continue
        for path in files_under(root):
            if path.name != "records.ndjson":
                continue
            label = (pathlib.PurePath(root.name) / path.relative_to(root)).as_posix()
            try:
                read_records(path, label, schema, intake)
            except OSError as exc:
                intake.reject(label, 0, "", f"unreadable:{exc.strerror}")
    return intake


def link(intake: Intake) -> dict[str, list[dict[str, Any]]]:
    """Attach cases and tests to their runs; returns the rows per case_uid."""
    kept: dict[str, list[dict[str, Any]]] = {}
    for uid, (record, label, number) in intake.cases.items():
        run = intake.runs.get(record["run_uid"])
        if run is None:
            intake.reject(label, number, "case", "orphan")
        else:
            run.cases.append(record)
            kept[uid] = []
    for record, label, number in intake.tests:
        run = intake.runs.get(record["run_uid"])
        if run is None:
            intake.reject(label, number, "test", "orphan")
        else:
            run.tests.append(record)
    for uid, rows in intake.rows.items():
        for record, label, number in rows:
            if uid in kept:
                kept[uid].append(record)
            else:
                intake.reject(label, number, record["kind"], "orphan")
    return kept


def load_meta(path: Optional[pathlib.Path]) -> dict[str, Any]:
    if path is None:
        return {}
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise lib.BenchError(f"cannot read --run-meta {path}: {exc}") from exc
    if not isinstance(meta, dict):
        raise lib.BenchError(f"--run-meta {path} is not a JSON object")
    unknown = sorted(str(key) for key in meta if key not in lib.META_KEYS)
    if unknown:
        raise lib.BenchError(f"--run-meta {path}: unknown key(s) {', '.join(unknown)}")
    if lib.find_secrets(meta):
        raise lib.BenchError(f"--run-meta {path} holds what looks like a secret")
    return meta


def parse_expect(values: list[str], matrix: lib.Matrix) -> list[tuple[str, str]]:
    expected = []
    for value in values:
        lane, _, versions = value.partition("=")
        rocm = [version for version in versions.split(",") if version]
        if lane not in matrix.lanes or not rocm:
            raise lib.BenchError(
                f"--expect {value}: want LANE=ROCM[,ROCM...] with a lane of the matrix"
            )
        expected += [(lane, version) for version in rocm]
    return list(dict.fromkeys(expected))


def run_order(run: Run) -> tuple[str, str, str]:
    record = run.record
    return record["lane"], record["rocm_version"], record["run_uid"]


def identity(
    meta: Mapping[str, Any], runs: list[Run], warnings: list[str]
) -> dict[str, Any]:
    """Who this collection belongs to: --run-meta first, then the first run."""
    first = min(runs, key=run_order).record if runs else {}
    found = {
        key: (meta if key in meta else first)[key]
        for key in IDENTITY_KEYS
        if key in meta or key in first
    }
    if "stamp" not in found or "bench_run_id" not in found:
        raise lib.BenchError(
            "no run record was read, so --run-meta must give 'stamp' and "
            "'bench_run_id'"
        )
    if "tier" not in found:
        warnings.append("no tier in --run-meta and no run read; assumed nightly")
    return {"repo": "local", "run_attempt": 1, "tier": "nightly", **found}


def synthetic_run(
    lane: lib.Lane,
    rocm_version: str,
    status: str,
    ident: Mapping[str, Any],
    matrix_sha: str,
    schema: Mapping[str, Any],
) -> Run:
    record = lib.blank_record("run")
    record.update(ident)
    record.update(
        status=status,
        lane=lane.name,
        env_class=lane.env_class,
        timing_trust=lane.timing_trust,
        rocm_version=rocm_version,
        matrix_sha256=matrix_sha,
    )
    try:
        record["run_uid"] = lib.run_uid(
            record["repo"],
            record["bench_run_id"],
            record["run_attempt"],
            lane.name,
            rocm_version,
        )
    except (TypeError, ValueError) as exc:
        raise lib.BenchError(f"--run-meta cannot name a run: {exc}") from exc
    record["env_fp"] = lib.env_fingerprint(record)
    errors = lib.validate_record(record, schema)
    if errors:
        raise lib.BenchError(f"--run-meta gives an invalid run record: {errors[0]}")
    return Run(record)


def add_expected_cases(run: Run, lane: lib.Lane, matrix: lib.Matrix) -> None:
    """A blocked or not-run record for each expected case the run lacks."""
    have = {
        case["case_key"]
        for case in run.cases
        if case["variant"] == "head" and case["round"] == 0
    }
    # A blocked lane runs in no tier, so its run stands for all of its cases.
    blocked = run.record["status"] == "blocked"
    expected = lane.cases if blocked else lib.select_cases(lane, run.record["tier"])
    for case in expected:
        if case.case_key not in have:
            run.cases.append(
                lib.case_record(
                    case, matrix.profiles[case.profile], run.record["run_uid"]
                )
            )


def synthesise(
    runs: list[Run],
    matrix: lib.Matrix,
    expected: list[tuple[str, str]],
    ident: Mapping[str, Any],
    matrix_sha: str,
    schema: Mapping[str, Any],
) -> list[str]:
    """Adds the expected records nothing reported; returns the unseen lanes."""
    seen: set[tuple[str, str]] = set()
    for run in list(runs):
        record = run.record
        seen.add((record["lane"], record["rocm_version"]))
        lane = matrix.lanes.get(record["lane"])
        if lane is not None:
            add_expected_cases(run, lane, matrix)
    for name, rocm_version in expected:
        if (name, rocm_version) in seen:
            continue
        lane = matrix.lanes[name]
        status = "blocked" if lane.status == "blocked" else "missing"
        run = synthetic_run(lane, rocm_version, status, ident, matrix_sha, schema)
        add_expected_cases(run, lane, matrix)
        runs.append(run)
        seen.add((name, rocm_version))
    lanes_seen = {name for name, _ in seen}
    unseen = []
    for name, lane in matrix.lanes.items():
        if name in lanes_seen:
            continue
        if lane.status != "blocked":
            unseen.append(name)
            continue
        run = synthetic_run(lane, "none", "blocked", ident, matrix_sha, schema)
        add_expected_cases(run, lane, matrix)
        runs.append(run)
    return unseen


def case_order(case: Mapping[str, Any]) -> tuple[bool, int, str, str, int]:
    order = case["order"]
    return order is None, order or 0, case["case_key"], case["variant"], case["round"]


def ordered_records(
    runs: list[Run], rows: Mapping[str, list[dict[str, Any]]]
) -> Iterator[dict[str, Any]]:
    for run in sorted(runs, key=run_order):
        yield run.record
        for case in sorted(run.cases, key=case_order):
            yield case
            mine = rows.get(case["case_uid"], [])
            yield from sorted(
                (row for row in mine if row["kind"] == "sample"),
                key=lambda row: (row["block_size"], row["batch_size"]),
            )
            yield from sorted(
                (row for row in mine if row["kind"] == "counter"),
                key=lambda row: row["name"],
            )
        yield from run.tests


def write_logs(path: pathlib.Path, runs: list[Run], intake: Intake) -> None:
    """The logs of every run in one tarball, less any file with a secret in it."""
    buffer = io.BytesIO()
    members: set[str] = set()
    with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT) as tar:
        for run in sorted(runs, key=run_order):
            if run.source is None or not (run.source / "logs").is_dir():
                continue
            prefix = log_prefix(run.record)
            for file in files_under(run.source / "logs"):
                name = f"{prefix}/{file.relative_to(run.source).as_posix()}"
                if name in members:
                    continue
                members.add(name)
                data = file.read_bytes()
                secrets = lib.find_secrets(data.decode("utf-8", errors="replace"))
                if secrets:
                    intake.reject(name, 0, "log", f"secret:{secrets[0][1]}")
                    continue
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
    path.write_bytes(gzip.compress(buffer.getvalue(), compresslevel=9, mtime=0))


def failed_required(case: Mapping[str, Any]) -> bool:
    if not case["required"]:
        return False
    if case["status"] == "skip":
        return bool(case["skip_reason"] != "filter")
    return case["status"] in ("fail", "timeout", "crash", "not-run")


def cell(text: Any) -> str:
    return str(text).replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")


def summary(
    name: str,
    runs: list[Run],
    unseen: list[str],
    intake: Intake,
    matrix_sha: str,
) -> str:
    lines = [f"## nixlbench collection {name}", ""]
    lines.append("| Lane | ROCm | Run | " + " | ".join(lib.CASE_STATUSES) + " |")
    lines.append("|---|---|---|" + "---:|" * len(lib.CASE_STATUSES))
    failures = []
    for run in sorted(runs, key=run_order):
        record = run.record
        counts = count_statuses(run)
        lines.append(
            f"| {cell(record['lane'])} | {cell(record['rocm_version'])} "
            f"| {record['status']} | "
            + " | ".join(str(counts[status]) for status in lib.CASE_STATUSES)
            + " |"
        )
        for case in sorted(run.cases, key=case_order):
            if case["status"] in ("pass", "blocked"):
                continue
            label = case["case_key"]
            if case["variant"] != "head" or case["round"]:
                label += f" ({case['variant']}, round {case['round']})"
            if case["status"] == "skip":
                reason = case["skip_reason"] or ""
            elif case["status"] == "not-run":
                reason = "no record"
            else:
                reason = case["error_class"]
            failures.append(
                f"| {cell(label)} | {cell(record['rocm_version'])} | {case['status']}"
                f"{' (required)' if failed_required(case) else ''} | {cell(reason)} "
                f"| {cell(case['error_excerpt'][:EXCERPT_CHARS])} |"
            )
    if failures:
        lines += ["", "### Cases that did not pass", ""]
        lines += [
            "| Case | ROCm | Status | Reason | Excerpt |",
            "|---|---|---|---|---|",
        ]
        lines += failures[:SUMMARY_CASES]
        if len(failures) > SUMMARY_CASES:
            lines += ["", f"{len(failures) - SUMMARY_CASES} more are in the records."]
    lines += ["", f"Rejected records and logs: {len(intake.rejected)}"]
    lines += [
        f"- `{cell(item['file'])}:{item['line']}` {cell(item['reason'])}"
        for item in intake.rejected[:20]
    ]
    if unseen:
        lines += ["", "Lanes neither reported nor expected: " + ", ".join(unseen)]
    if intake.warnings:
        lines += ["", "Warnings:"]
        lines += [f"- {cell(warning)}" for warning in intake.warnings]
    lines += ["", f"Matrix: `{matrix_sha}`", ""]
    return "\n".join(lines)


def review_runs(
    runs: list[Run],
    matrix: lib.Matrix,
    matrix_sha: str,
    ident: Mapping[str, Any],
    warnings: list[str],
) -> bool:
    """Warns of what was not expected, re-roots log_ref; True on another matrix."""
    known = {case.case_key for lane in matrix.lanes.values() for case in lane.cases}
    mismatch = False
    names = ("stamp", "bench_run_id", "run_attempt")
    for run in sorted(runs, key=run_order):
        record = run.record
        where = f"{record['lane']} ROCm {record['rocm_version']}"
        if record["matrix_sha256"] != matrix_sha:
            mismatch = True
            warnings.append(
                f"matrix-mismatch: {where} ran matrix {record['matrix_sha256']}"
            )
        if any(record[name] != ident[name] for name in names):
            warnings.append(f"{where} belongs to another bench run or attempt")
        if record["lane"] not in matrix.lanes:
            warnings.append(f"unexpected: lane of {where} is not in the matrix")
        for case in sorted(run.cases, key=case_order):
            if case["case_key"] not in known:
                warnings.append(f"unexpected: {case['case_key']} in {where}")
            if case["log_ref"] is not None:
                case["log_ref"] = f"{log_prefix(record)}/{case['log_ref']}"
    instances = [run_order(run)[:2] for run in runs]
    for lane, rocm_version in sorted(set(instances)):
        if instances.count((lane, rocm_version)) > 1:
            warnings.append(f"more than one run of {lane} ROCm {rocm_version}")
    return mismatch


def collect(args: argparse.Namespace) -> int:
    matrix = lib.load_matrix(args.matrix)
    schema = lib.load_schema()
    manifest = lib.matrix_manifest(matrix)
    matrix_sha = lib.matrix_sha256(manifest)
    expected = parse_expect(args.expect, matrix)
    meta = load_meta(args.run_meta)

    intake = read_inputs(args.input, schema)
    rows = link(intake)
    runs = list(intake.runs.values())
    if not runs and not meta:
        for item in intake.rejected:
            warn(f"rejected {item['file']}:{item['line']}: {item['reason']}")
        raise lib.BenchError("no run record was read and there is no --run-meta")
    ident = identity(meta, runs, intake.warnings)

    mismatch = review_runs(runs, matrix, matrix_sha, ident, intake.warnings)
    unseen = synthesise(runs, matrix, expected, ident, matrix_sha, schema)

    name = (
        f"bench-{safe(ident['stamp'])}-{safe(ident.get('slug') or 'local')}"
        f"-{safe(ident['bench_run_id'])}.a{safe(ident['run_attempt'])}"
    )
    out: pathlib.Path = args.out
    out.mkdir(parents=True, exist_ok=True)
    data = "".join(
        json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
        for record in ordered_records(runs, rows)
    ).encode("ascii")
    outputs = [f"{name}.ndjson.gz", f"{name}.logs.tar.gz", f"matrix-{matrix_sha}.json"]
    (out / outputs[0]).write_bytes(gzip.compress(data, compresslevel=9, mtime=0))
    write_logs(out / outputs[1], runs, intake)
    (out / outputs[2]).write_text(lib.canonical_json(manifest), encoding="ascii")
    zstd = shutil.which("zstd") if args.zstd == "auto" else None
    if zstd:
        packed = subprocess.run(
            [zstd, "-19", "-q", "-c"], input=data, capture_output=True, check=False
        )
        if packed.returncode == 0:
            (out / f"{name}.ndjson.zst").write_bytes(packed.stdout)
            outputs.append(f"{name}.ndjson.zst")
        else:
            intake.warnings.append(f"zstd failed with status {packed.returncode}")

    required = sorted(
        {
            case["case_key"]
            for run in runs
            for case in run.cases
            if failed_required(case)
        }
    )
    text = summary(name, runs, unseen, intake, matrix_sha)
    (out / "summary.md").write_text(text, encoding="utf-8")
    outputs.append("summary.md")
    if args.summary_out is not None:
        with open(args.summary_out, "a", encoding="utf-8") as stream:
            stream.write(text)
    report = {
        "schema": lib.SCHEMA,
        "matrix_sha256": matrix_sha,
        "outputs": sorted(outputs),
        "runs": [
            {
                "lane": run.record["lane"],
                "rocm_version": run.record["rocm_version"],
                "status": run.record["status"],
                "counts": count_statuses(run),
            }
            for run in sorted(runs, key=run_order)
        ],
        "required_failures": required,
        "rejected": intake.rejected,
        "warnings": intake.warnings,
    }
    (out / "collect.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    for item in intake.rejected:
        warn(f"rejected {item['file']}:{item['line']}: {item['reason']}")
    for warning in intake.warnings:
        warn(warning)
    lines = data.count(b"\n")
    print(f"{out / outputs[0]}: {lines} record(s) of {len(runs)} run(s)")
    if intake.rejected or mismatch:
        return 3
    if required:
        warn(f"{len(required)} required case(s) did not pass")
        return 1
    return 0


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--matrix",
        type=pathlib.Path,
        default=pathlib.Path(__file__).resolve().parent.parent / "bench",
        help="directory with profiles/ and lanes/ (default: ../bench)",
    )
    parser.add_argument(
        "--input",
        type=pathlib.Path,
        action="append",
        required=True,
        metavar="DIR",
        help="directory searched for records.ndjson files; may be repeated",
    )
    parser.add_argument("--out", type=pathlib.Path, required=True, metavar="DIR")
    parser.add_argument(
        "--run-meta",
        type=pathlib.Path,
        metavar="FILE",
        help="JSON object naming the bench run, for lanes that reported nothing",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        metavar="LANE=ROCM[,ROCM...]",
        help="a lane job that should have reported; may be repeated",
    )
    parser.add_argument("--zstd", choices=("auto", "never"), default="auto")
    parser.add_argument(
        "--summary-out",
        type=pathlib.Path,
        metavar="FILE",
        help="file the Markdown summary is appended to",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return collect(args)
    except lib.BenchError as exc:
        for line in str(exc).splitlines():
            warn(line)
        return 2
    except OSError as exc:
        warn(f"{exc}")
        return 2
    except Exception:
        # Not 1: that says a required case did not pass.
        traceback.print_exc()
        warn("internal error")
        return 3


if __name__ == "__main__":
    sys.exit(main())
