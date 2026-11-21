#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Publisher of the AMD nixlbench results: collected artifacts in, site data out.

    amd_bench_publish.py inspect --input DIR [--input DIR]... --out PLAN.json
        [--stage DIR] [--summary-out FILE] [--max-bytes N] [--repo OWNER/NAME]
    amd_bench_publish.py publish --site DIR [--input DIR]... [--heal DIR]...
        [--assets FILE] [--default-branch NAME] [--rerender] [--now ISO]
        [--report FILE] [--output FILE] [--warn-bytes N] [--max-bytes N]
        [--stale-days N] [--repo OWNER/NAME]
    amd_bench_publish.py check-staged --site DIR [--rerender] [--output FILE]
    amd_bench_publish.py pending --manifest FILE|- --assets FILE [--days N]
        [--limit N] [--now ISO]
    amd_bench_publish.py served --base-url URL --generation N
        [--expect PATH=SHA256]... [--timeout S] [--interval S]

An artifact is a directory as amd_bench_collect.py writes it: one or more
bench-<stamp>-<slug>-<run>.a<attempt>.ndjson.gz, the matrix-<sha256>.json they
ran, and optionally a logs tarball and a collect file. 'inspect' checks each
directory on its own and lays the files of the valid ones out for the raw
archive. 'publish' is a reconciler over a checkout of the site: it adds each
artifact the manifest does not list as a shard under data/bench/v1/, and
rewrites the manifest, latest.json and the pages under bench/. Publishing the
same artifact again changes nothing. 'check-staged' is the guard run before
the commit: nothing but this tool's own paths may be staged, and nothing
published may be modified or deleted. 'pending' names the archived artifacts
the manifest lacks, and 'served' waits until the site serves a generation.

Exits 0 when done or when there was nothing to do, 1 on a staged-path
violation, 2 on bad usage or an unusable site, 3 when an artifact is invalid,
4 on a conflict with published data, 5 on an internal error, 6 when the site
did not serve in time. Standard library only.
"""

from __future__ import annotations

import argparse
import collections
import contextlib
import copy
import datetime
import gzip
import hashlib
import io
import json
import math
import os
import pathlib
import re
import shutil
import statistics
import subprocess
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.error
import urllib.request
import zlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

try:
    import amd_bench_lib as lib
    import amd_bench_pages as pages
except ImportError:
    sys.exit("amd_bench_lib.py and amd_bench_pages.py must sit beside this script")

DATA_ROOT = "data/bench/v1"
PAGES_ROOT = "bench"
MANIFEST = f"{DATA_ROOT}/manifest.json"
LATEST = f"{DATA_ROOT}/latest.json"
OWN_PREFIXES = (f"{DATA_ROOT}/", f"{PAGES_ROOT}/")
DEFAULT_BRANCH = "main-amd"

# ASCII classes only: \d would take any Unicode digit into a path or a tag.
STAMP = r"[0-9]{8}T[0-9]{6}Z"
SLUG = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}"
RUN = r"[0-9]{1,20}"
ATT = r"[0-9]{1,4}"
HEX64 = r"[0-9a-f]{64}"
_SHARD = rf"({STAMP})-({SLUG})-({RUN})\.a({ATT})"
SHARD_ID_RE = re.compile(_SHARD)
RECORDS_RE = re.compile(rf"bench-({_SHARD})\.ndjson\.gz")
RUN_ID_RE = re.compile(RUN)
SHA1_RE = re.compile(r"[0-9a-f]{40}")
HEX64_RE = re.compile(HEX64)
MATRIX_NAME_RE = re.compile(rf"matrix-({HEX64})\.json")
TAG_RE = re.compile(r"bench-raw-[0-9]{4}-[0-9]{2}")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
LANE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
BRANCH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"
NOW_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

_NIGHT = rf"{DATA_ROOT}/(night|weekly)/{STAMP}/{SLUG}/{RUN}\.a{ATT}"
KIND_FILES = (
    ("run", "runs"),
    ("case", "cases"),
    ("sample", "samples"),
    ("counter", "counters"),
    ("test", "tests"),
)
_DATA_NAMES = "|".join(stem for _, stem in KIND_FILES)
STAGED_RE = re.compile(
    rf"{DATA_ROOT}/(manifest|latest)\.json"
    rf"|{DATA_ROOT}/matrix/{HEX64}\.json"
    rf"|{_NIGHT}/(index\.json|({_DATA_NAMES})\.ndjson\.gz)"
    rf"|{PAGES_ROOT}/index\.html"
    rf"|{PAGES_ROOT}/runs/{STAMP}-{SLUG}-{RUN}\.a{ATT}\.html"
)
MUTABLE = (MANIFEST, LATEST, f"{PAGES_ROOT}/index.html")
DERIVED_RE = re.compile(rf"{_NIGHT}/index\.json|{PAGES_ROOT}/runs/[^/]+\.html")
SHARD_PATH_RE = re.compile(rf"(night|weekly)/({STAMP})/({SLUG})/({RUN})\.a({ATT})")

MIB = 1 << 20
RAW_MAX = 64 * MIB
LOGS_MAX_FILE = 64 * MIB
LOGS_MAX_MEMBERS = 20000
LOGS_MAX_MEMBER = 8 * MIB
LOGS_MAX_TOTAL = 256 * MIB
# The secret scan is quadratic in the length of a line.
LOGS_MAX_LINE = 8 << 10
LOGS_MAX_SECONDS = 60.0
MATRIX_MAX = MIB
INT_MAX = (1 << 63) - 1
SMALL_FILE_MAX = 16 * MIB
SERVED_MAX = 32 * MIB
TIER_RANK = {"pr": 0, "nightly": 1, "weekly": 2}
SEVERITY = ("crash", "fail", "timeout", "not-run", "skip")
FAILING = ("crash", "fail", "timeout", "not-run")
LATEST_ATTENTION = 100
LATEST_TESTS = 20
LATEST_EXCERPT = 200
VERIFY_PAGES = 10
COLLECT_ITEMS = 50
COLLECT_CHARS = 300
LOGS_NOTE = "logs tarball not archived: "
COLLECT_NOTE = "collect file not archived: "
ATTENTION_FIELDS = (
    "case_uid case_key variant round order status reason required role tier "
    "profile backend engine transport_req transport_obs init_mem tgt_mem op "
    "attach consistency exit_init exit_tgt error_class skip_reason "
    "error_excerpt duration_s rows_expected rows_seen cmdline log_ref started_at"
).split()
SLIM_FIELDS = (
    "lane rocm_version case_key variant round status reason required error_excerpt"
).split()
LANE_FIELDS = (
    "run_uid status env_class timing_trust n tests filtered lane_blocked_by "
    "calib_gbps degraded runner_image guest_kernel started_at finished_at"
).split()
# Where git would otherwise look instead of at --site, as inside a hook.
GIT_LOCATION = (
    "GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_OBJECT_DIRECTORY "
    "GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_COMMON_DIR GIT_NAMESPACE GIT_PREFIX"
).split()
CONTRACT_URLS = (
    ("", False),
    ("data/nightly/index.json", True),
    ("wheels/index.html", False),
    (f"{PAGES_ROOT}/index.html", False),
    (LATEST, True),
)


class Invalid(Exception):
    """An artifact that must not be published. The text never quotes a value."""


class Duplicate(ValueError):
    """A JSON object with a key twice: a parser keeps one and hides the other."""


class Conflict(Exception):
    """What is asked for contradicts what is already published."""


class Unusable(Exception):
    """Bad usage, or a site this tool cannot work on."""


def printable(text: str) -> str:
    """text on one line of printable ASCII."""
    return re.sub(r"[^ -~]", "?", text)


def say(message: str) -> None:
    # One line: text about data this tool does not trust must not be able to
    # start a workflow command in a step log.
    sys.stderr.write(f"amd_bench_publish.py: {printable(message)}\n")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def plain(text: Any, limit: int = 200) -> str:
    """text as it may go into a message or a Markdown cell."""
    return re.sub(r"[^A-Za-z0-9._+/ :-]", "_", str(text))[:limit]


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def regular(path: pathlib.Path) -> bool:
    return path.is_file() and not path.is_symlink()


def parse_now(text: Optional[str]) -> datetime.datetime:
    if text is None:
        return datetime.datetime.now(datetime.timezone.utc).replace(
            microsecond=0, tzinfo=None
        )
    try:
        return datetime.datetime.strptime(text, NOW_FORMAT)
    except ValueError as exc:
        raise Unusable(f"--now {plain(text)}: want YYYY-MM-DDTHH:MM:SSZ") from exc


def stamp_time(stamp: str) -> datetime.datetime:
    return datetime.datetime.strptime(stamp, STAMP_FORMAT)


# --- identity -------------------------------------------------------------


@dataclass(frozen=True)
class Name:
    """What the name of a collected records file says about its shard."""

    id: str
    stamp: str
    slug: str
    run: str
    attempt: int

    @property
    def tag(self) -> str:
        return f"bench-raw-{self.stamp[:4]}-{self.stamp[4:6]}"

    @property
    def order(self) -> tuple[int, int]:
        return int(self.run), self.attempt

    @property
    def records(self) -> str:
        return f"bench-{self.id}.ndjson.gz"

    @property
    def logs(self) -> str:
        return f"bench-{self.id}.logs.tar.gz"

    @property
    def collect(self) -> str:
        return f"bench-{self.id}.collect.json"


def parse_id(text: str) -> Optional[Name]:
    """The shard named by text, or None. Checked before any path is built."""
    match = SHARD_ID_RE.fullmatch(text)
    if match is None:
        return None
    stamp, slug, run, attempt = match.groups()
    try:
        stamp_time(stamp)
    except ValueError:
        return None
    # One spelling per number: '07' and '7' would be two shards of one order.
    if run != str(int(run)) or attempt != str(int(attempt)) or int(attempt) < 1:
        return None
    return Name(text, stamp, slug, run, int(attempt))


def shard_path(name: Name, tier: str) -> str:
    """The shard directory, relative to the data root."""
    root = "weekly" if tier == "weekly" else "night"
    return f"{root}/{name.stamp}/{name.slug}/{name.run}.a{name.attempt}"


def lane_key(run: Mapping[str, Any]) -> str:
    return f"{run['lane']}/{run['rocm_version']}"


def entry_order(entry: Mapping[str, Any]) -> tuple[int, int]:
    return int(entry["bench_run_id"]), int(entry["run_attempt"])


# --- loading an artifact --------------------------------------------------


@dataclass
class Shard:
    """One collected records file with everything published from it."""

    name: Name
    source: pathlib.Path
    raw: bytes
    records: dict[str, list[dict[str, Any]]]
    files: dict[str, bytes]
    matrix_sha: str
    matrix: dict[str, Any]
    matrix_bytes: bytes
    matrix_path: Optional[pathlib.Path]
    logs: Optional[pathlib.Path] = None
    collect_path: Optional[pathlib.Path] = None
    collect: Optional[dict[str, Any]] = None
    notes: list[str] = field(default_factory=list)
    budget: int = 0

    @property
    def raw_sha256(self) -> str:
        return sha256(self.raw)

    @property
    def repo(self) -> str:
        return str(self.records["run"][0]["repo"])


def gunzip(data: bytes, limit: int) -> bytes:
    """data inflated, refusing more than limit bytes before making them."""
    out = bytearray()
    rest = data
    if not rest:
        raise Invalid("empty file")
    while rest:
        # No file name, comment or extra field: those would be archived unread.
        if rest[:2] == b"\x1f\x8b" and len(rest) > 3 and rest[3] != 0:
            raise Invalid("gzip header with optional fields")
        inflater = zlib.decompressobj(wbits=31)
        try:
            out += inflater.decompress(rest, limit + 1 - len(out))
        except zlib.error as exc:
            raise Invalid("not a gzip file") from exc
        if len(out) > limit:
            raise Invalid(f"inflates to more than {limit} bytes")
        if not inflater.eof:
            raise Invalid("truncated gzip file")
        rest = inflater.unused_data
    return bytes(out)


def read_small(path: pathlib.Path, what: str) -> bytes:
    if path.stat().st_size > SMALL_FILE_MAX:
        raise Invalid(f"{path.name}: {what} larger than {SMALL_FILE_MAX} bytes")
    return path.read_bytes()


def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found = dict(pairs)
    if len(found) != len(pairs):
        raise Duplicate("a key is given twice")
    return found


def json_object(data: bytes) -> Optional[dict[str, Any]]:
    """data as a JSON object with no key given twice, else None."""
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=unique)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def out_of_range(value: Any) -> bool:
    """Whether value holds an integer that no 64-bit reader could take."""
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif is_int(item) and abs(item) > INT_MAX:
            return True
    return False


def encode(records: Iterable[Mapping[str, Any]]) -> bytes:
    return "".join(
        json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
        + "\n"
        for record in records
    ).encode("ascii")


def pack(records: Iterable[Mapping[str, Any]]) -> bytes:
    return gzip.compress(encode(records), compresslevel=9, mtime=0)


def read_records(
    name: str, data: bytes, schema: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """The records of an inflated NDJSON file per kind, every one checked."""
    records: dict[str, list[dict[str, Any]]] = {kind: [] for kind, _ in KIND_FILES}
    # The raw file is archived as it is, so a line must say nothing that the
    # parsed record does not: the reader below keeps the last of two keys.
    lines = io.StringIO(data.decode("utf-8", errors="replace"), newline=None)
    for number, line in enumerate(lines, 1):
        try:
            json.loads(line, object_pairs_hook=unique)
        except Duplicate as exc:
            raise Invalid(f"{name}:{number}: a key is given twice") from exc
        except (ValueError, RecursionError):
            pass
    with tempfile.TemporaryDirectory(prefix="amd-bench-publish-") as scratch:
        path = pathlib.Path(scratch) / "records.ndjson"
        path.write_bytes(data)
        for number, record, error in lib.read_ndjson(path):
            where = f"{name}:{number}"
            if record is None:
                raise Invalid(f"{where}: {error.split(':')[0]}")
            secrets = lib.find_secrets(record)
            if secrets:
                raise Invalid(f"{where}: secret:{secrets[0][1]}")
            errors = lib.validate_record(record, schema)
            if errors:
                raise Invalid(f"{where}: schema: {printable(errors[0])}")
            if out_of_range(record):
                raise Invalid(f"{where}: an integer beyond 64 bits")
            kind = record["kind"]
            for key in ("cmdline", "error_excerpt"):
                text = record.get(key)
                if isinstance(text, str) and lib.redact_text(text) != text:
                    raise Invalid(f"{where}: '{key}' is not redaction-stable")
            records[kind].append({key: record[key] for key in lib.FIELDS[kind]})
    return records


def check_links(name: str, records: Mapping[str, list[dict[str, Any]]]) -> None:
    if not records["run"]:
        raise Invalid(f"{name}: no run record")
    runs = {run["run_uid"] for run in records["run"]}
    cases = {case["case_uid"] for case in records["case"]}
    for kind, key, known in (
        ("case", "run_uid", runs),
        ("test", "run_uid", runs),
        ("sample", "case_uid", cases),
        ("counter", "case_uid", cases),
    ):
        orphans = sum(record[key] not in known for record in records[kind])
        if orphans:
            raise Invalid(f"{name}: {orphans} {kind} record(s) with an unknown {key}")


def check_identity(name: Name, runs: Sequence[Mapping[str, Any]]) -> None:
    """The run records are of one branch and one night: the one in the name."""
    file = name.records
    first = runs[0]
    for key in ("repo", "stamp", "slug", "branch", "sha", "tier"):
        if any(run[key] != first[key] for run in runs):
            raise Invalid(f"{file}: the run records disagree on '{key}'")
    if first["stamp"] != name.stamp:
        raise Invalid(f"{file}: the run records carry another stamp than the name")
    if first["slug"] != name.slug:
        raise Invalid(f"{file}: the run records carry another slug than the name")
    if not REPO_RE.fullmatch(first["repo"]):
        raise Invalid(f"{file}: 'repo' is not OWNER/NAME")
    for number, run in enumerate(runs, 1):
        for key in ("lane", "rocm_version"):
            if not LANE_RE.fullmatch(run[key]):
                raise Invalid(f"{file}: run {number}: '{key}' has a forbidden form")
        if not RUN_ID_RE.fullmatch(run["bench_run_id"]):
            raise Invalid(f"{file}: run {number}: 'bench_run_id' is not a number")


def find_matrix(
    directory: pathlib.Path, wanted: str, site_matrix: Optional[pathlib.Path]
) -> tuple[bytes, Optional[pathlib.Path]]:
    """The matrix file of that hash: beside the records, else on the site."""
    places: list[tuple[pathlib.Path, Optional[pathlib.Path]]] = []
    beside = directory / f"matrix-{wanted}.json"
    places.append((beside, beside))
    if site_matrix is not None:
        places.append((site_matrix / f"{wanted}.json", None))
    for path, origin in places:
        if not regular(path):
            continue
        data = read_small(path, "matrix")
        if sha256(data) != wanted:
            raise Invalid(f"{path.name}: its sha256 is not the hash in its name")
        return data, origin
    raise Invalid(f"matrix-{wanted}.json is neither beside the records nor published")


def check_logs(path: pathlib.Path) -> str:
    """Why a logs tarball must not become a public asset; empty when it may.

    The file is archived as it is, so what is checked is what a reader of the
    archive gets: plain text files and nothing else.
    """
    deadline = time.monotonic() + LOGS_MAX_SECONDS
    try:
        if path.stat().st_size > LOGS_MAX_FILE:
            return f"larger than {LOGS_MAX_FILE} bytes"
        with open(path, "rb") as opened:
            head = opened.read(4)
        if head[:2] == b"\x1f\x8b" and head[3:] != b"\0":
            return "gzip header with optional fields"
        total = 0
        with tarfile.open(path, mode="r:gz") as tar:
            for number, member in enumerate(tar, 1):
                if number > LOGS_MAX_MEMBERS:
                    return f"more than {LOGS_MAX_MEMBERS} members"
                name = member.name
                if lib.find_secrets(name) or lib.redact_text(name) != name:
                    return f"member {number}: its name looks like a secret"
                where = f"member '{plain(name)}'"
                if not member.isreg():
                    return f"{where}: not a regular file"
                if name.startswith("/") or ".." in name.split("/"):
                    return f"{where}: not a relative path inside the archive"
                if not name.endswith(".log"):
                    return f"{where}: not a .log file"
                if member.uname or member.gname or member.pax_headers:
                    return f"{where}: its header carries more than a name"
                if member.size > LOGS_MAX_MEMBER:
                    return f"{where}: larger than {LOGS_MAX_MEMBER} bytes"
                total += member.size
                if total > LOGS_MAX_TOTAL:
                    return f"more than {LOGS_MAX_TOTAL} bytes in all"
                stream = tar.extractfile(member)
                data = stream.read() if stream else b""
                if b"\0" in data:
                    return f"{where}: not text"
                if any(len(line) > LOGS_MAX_LINE for line in data.split(b"\n")):
                    return f"{where}: a line too long to check"
                if time.monotonic() > deadline:
                    return f"not checked within {LOGS_MAX_SECONDS:.0f} seconds"
                text = data.decode("utf-8", errors="replace")
                secrets = lib.find_secrets(text)
                if secrets:
                    return f"{where}: secret:{secrets[0][1]}"
                if lib.redact_text(text) != text:
                    return f"{where}: not redaction-stable"
            rest = tar.fileobj.read(MIB + 1) if tar.fileobj else b""
            if len(rest) > MIB or rest.strip(b"\0"):
                return "data after the end of the archive"
    except (OSError, EOFError, tarfile.TarError, zlib.error) as exc:
        return f"unreadable ({type(exc).__name__})"
    return ""


def collect_summary(doc: Mapping[str, Any]) -> dict[str, Any]:
    """The part of a collect file, or of an index made from one, that is kept."""

    def texts(value: Any) -> list[str]:
        items = value if isinstance(value, list) else []
        return [str(item)[:COLLECT_CHARS] for item in items[:COLLECT_ITEMS]]

    rejected = doc.get("rejected")
    return {
        "rejected": (
            len(rejected)
            if isinstance(rejected, list)
            else rejected if is_int(rejected) else 0
        ),
        "warnings": texts(doc.get("warnings")),
        "required_failures": texts(doc.get("required_failures")),
    }


def find_collect(
    directory: pathlib.Path, name: Name
) -> tuple[Optional[pathlib.Path], Optional[dict[str, Any]], str]:
    """(file, summary, why not) of the collect file of a shard, if any."""
    path = directory / name.collect
    doc: Optional[dict[str, Any]] = None
    try:
        if regular(path):
            doc = json_object(read_small(path, "collect file"))
        else:
            path = directory / "collect.json"
            if not regular(path):
                return None, None, ""
            doc = json_object(read_small(path, "collect file"))
            outputs = doc.get("outputs") if doc is not None else None
            if not isinstance(outputs, list) or name.records not in outputs:
                return None, None, ""
    except (OSError, Invalid):
        return None, None, f"{path.name} is unreadable"
    if doc is None:
        return None, None, f"{path.name} is not a JSON object with each key once"
    if lib.find_secrets(doc):
        return None, None, f"{path.name} holds what looks like a secret"
    return path, collect_summary(doc), ""


def load_shard(
    path: pathlib.Path,
    schema: Mapping[str, Any],
    site_matrix: Optional[pathlib.Path],
    max_bytes: int,
    branch: str = DEFAULT_BRANCH,
    repo: Optional[str] = None,
) -> Shard:
    """One records file, checked; Invalid says the file, the line and the rule."""
    match = RECORDS_RE.fullmatch(path.name)
    name = parse_id(match.group(1)) if match else None
    if name is None:
        raise Invalid(f"{plain(path.name)}: not a publishable records file name")
    try:
        if path.stat().st_size > RAW_MAX:
            raise Invalid(f"{path.name}: larger than {RAW_MAX} bytes")
        raw = path.read_bytes()
    except OSError as exc:
        raise Invalid(f"{path.name}: unreadable ({exc.strerror})") from exc
    try:
        data = gunzip(raw, RAW_MAX)
    except Invalid as exc:
        raise Invalid(f"{path.name}: {exc}") from exc
    records = read_records(path.name, data, schema)
    check_links(path.name, records)
    check_identity(name, records["run"])
    if repo is not None and records["run"][0]["repo"] != repo:
        raise Invalid(f"{path.name}: the records are of another repository")

    hashes = collections.Counter(run["matrix_sha256"] for run in records["run"])
    matrix_sha = hashes.most_common(1)[0][0]
    try:
        matrix_bytes, matrix_path = find_matrix(path.parent, matrix_sha, site_matrix)
    except OSError as exc:
        raise Invalid(f"{path.name}: matrix unreadable ({exc.strerror})") from exc
    matrix = json_object(matrix_bytes)
    if matrix is None:
        raise Invalid(f"matrix-{matrix_sha}.json: not a JSON object")
    # The matrix is published and archived byte for byte, like the records.
    if matrix_path is not None and len(matrix_bytes) > MATRIX_MAX:
        raise Invalid(f"matrix-{matrix_sha}.json: larger than {MATRIX_MAX} bytes")
    if lib.find_secrets(matrix):
        raise Invalid(f"matrix-{matrix_sha}.json: holds what looks like a secret")

    shard = Shard(
        name=name,
        source=path,
        raw=raw,
        records=records,
        files={f"{stem}.ndjson.gz": pack(records[kind]) for kind, stem in KIND_FILES},
        matrix_sha=matrix_sha,
        matrix=matrix,
        matrix_bytes=matrix_bytes,
        matrix_path=matrix_path,
    )
    logs = path.parent / name.logs
    if regular(logs):
        why = check_logs(logs)
        if why:
            shard.notes.append(f"{LOGS_NOTE}{why}")
        else:
            shard.logs = logs
    shard.collect_path, shard.collect, why = find_collect(path.parent, name)
    if why:
        shard.notes.append(f"{COLLECT_NOTE}{why}")

    entry = base_entry(shard, shard.logs is not None, shard.collect is not None)
    index = build_index(
        entry,
        shard.repo,
        docs_url(shard.repo, branch),
        build_view(records, name.run),
        matrix,
        shard.collect,
        shard.notes,
    )
    shard.budget = sum(map(len, shard.files.values())) + len(dump_json(index))
    if shard.budget > max_bytes:
        raise Invalid(f"{path.name}: over budget: {shard.budget} bytes")
    # What cannot be shown must not be archived: the heal would bring it back.
    try:
        pages.render_run(index)
    except pages.PageError as exc:
        raise Invalid(f"{path.name}: its run page cannot be rendered") from exc
    return shard


def records_files(directory: pathlib.Path) -> list[pathlib.Path]:
    try:
        return sorted(
            path
            for path in directory.iterdir()
            if path.name.endswith(".ndjson.gz") and regular(path)
        )
    except OSError:
        return []


def load_input(
    directory: pathlib.Path,
    schema: Mapping[str, Any],
    site_matrix: Optional[pathlib.Path],
    max_bytes: int,
    branch: str = DEFAULT_BRANCH,
    repo: Optional[str] = None,
) -> list[Shard]:
    """Every shard of one artifact directory; one failure fails the directory."""
    if not directory.is_dir():
        raise Invalid("not a directory")
    files = records_files(directory)
    if not files:
        raise Invalid("no records file (bench-*.ndjson.gz)")
    return [
        load_shard(path, schema, site_matrix, max_bytes, branch, repo) for path in files
    ]


# --- what a shard says ----------------------------------------------------


@dataclass
class View:
    """The records the summaries use: those of the kept run of each lane key."""

    primary: dict[str, Any]
    kept: list[dict[str, Any]]
    cases: dict[str, list[dict[str, Any]]]
    tests: dict[str, list[dict[str, Any]]]
    samples: dict[str, list[dict[str, Any]]]
    rows: int
    warnings: list[str]


def build_view(records: Mapping[str, list[dict[str, Any]]], run_id: str) -> View:
    runs = records["run"]
    best: dict[str, tuple[tuple[int, int, int], dict[str, Any]]] = {}
    doubles: set[str] = set()
    for position, run in enumerate(runs):
        key = lane_key(run)
        rank = (run["run_attempt"], int(run["bench_run_id"] == run_id), position)
        if key in best:
            doubles.add(key)
        if key not in best or rank > best[key][0]:
            best[key] = (rank, run)
    kept = [best[key][1] for key in sorted(best, key=lambda k: tuple(k.split("/")))]
    warnings = [
        f"more than one run of {key}; the summaries use attempt "
        f"{best[key][1]['run_attempt']} ({best[key][1]['run_uid']})"
        for key in sorted(doubles)
    ]
    matrices = collections.Counter(run["matrix_sha256"] for run in runs)
    majority = matrices.most_common(1)[0][0]
    warnings += [
        f"{lane_key(run)} ran another matrix ({run['matrix_sha256'][:12]})"
        for run in kept
        if run["matrix_sha256"] != majority
    ]
    uids = {run["run_uid"] for run in kept}
    cases: dict[str, list[dict[str, Any]]] = {uid: [] for uid in uids}
    tests: dict[str, list[dict[str, Any]]] = {uid: [] for uid in uids}
    for case in records["case"]:
        if case["run_uid"] in uids:
            cases[case["run_uid"]].append(case)
    for test in records["test"]:
        if test["run_uid"] in uids:
            tests[test["run_uid"]].append(test)
    samples: dict[str, list[dict[str, Any]]] = {}
    for sample in records["sample"]:
        samples.setdefault(sample["case_uid"], []).append(sample)
    own = [run for run in runs if run["bench_run_id"] == run_id]
    return View(
        primary=(own or runs)[0],
        kept=kept,
        cases=cases,
        tests=tests,
        samples=samples,
        rows=sum(len(items) for items in records.values()),
        warnings=warnings,
    )


def tally(names: Sequence[str], items: Iterable[Mapping[str, Any]]) -> list[int]:
    counts = dict.fromkeys(names, 0)
    for item in items:
        counts[item["status"]] += 1
    return [counts[name] for name in names]


def add(left: Sequence[int], right: Sequence[int]) -> list[int]:
    return [a + b for a, b in zip(left, right)]


def named(names: Sequence[str], counts: Sequence[int]) -> dict[str, int]:
    return dict(zip(names, counts))


def filtered(cases: Iterable[Mapping[str, Any]]) -> bool:
    return any(case["skip_reason"] == "filter" for case in cases)


def manifest_fields(view: View) -> dict[str, Any]:
    """The fields of a manifest entry that are derived from the records."""
    lanes: dict[str, Any] = {}
    total = [0] * len(lib.CASE_STATUSES)
    tests = [0] * len(lib.TEST_STATUSES)
    for run in view.kept:
        cases = view.cases[run["run_uid"]]
        counts = tally(lib.CASE_STATUSES, cases)
        lane: dict[str, Any] = {"status": run["status"], "n": counts}
        if filtered(cases):
            lane["filtered"] = True
        lanes[lane_key(run)] = lane
        total = add(total, counts)
        tests = add(tests, tally(lib.TEST_STATUSES, view.tests[run["run_uid"]]))
    return {"rows": view.rows, "n": total, "tests": tests, "lanes": lanes}


def base_entry(shard: Shard, logs: bool, collect: bool) -> dict[str, Any]:
    """The manifest entry of a new shard, less what only a publish knows."""
    name = shard.name
    view = build_view(shard.records, name.run)
    primary = view.primary
    files = [
        {
            "name": f"{stem}.ndjson.gz",
            "kind": kind,
            "format": "ndjson.gz",
            "rows": len(shard.records[kind]),
            "bytes": len(shard.files[f"{stem}.ndjson.gz"]),
            "sha256": sha256(shard.files[f"{stem}.ndjson.gz"]),
        }
        for kind, stem in KIND_FILES
    ]
    return {
        "id": name.id,
        "path": shard_path(name, primary["tier"]),
        "tier": primary["tier"],
        "stamp": name.stamp,
        "slug": name.slug,
        "branch": primary["branch"],
        "sha": primary["sha"],
        "bench_run_id": name.run,
        "run_attempt": name.attempt,
        "nightly_run_id": primary["nightly_run_id"],
        "reason": primary["reason"],
        "matrix_sha256": shard.matrix_sha,
        "generation": 0,
        "published_at": "",
        "authoritative": True,
        "bytes": 0,
        "files": files,
        "raw": {
            "tag": name.tag,
            "records": name.records,
            "bytes": len(shard.raw),
            "sha256": shard.raw_sha256,
            "logs": logs,
            "collect": collect,
        },
        "superseded_by": {},
        "supersedes": [],
        **manifest_fields(view),
    }


def set_index_file(entry: dict[str, Any], data: bytes) -> None:
    """Record the index.json of a shard in its entry and re-add the bytes."""
    files = [item for item in entry["files"] if item.get("name") != "index.json"]
    files.append(
        {
            "name": "index.json",
            "kind": "index",
            "format": "json",
            "rows": None,
            "bytes": len(data),
            "sha256": sha256(data),
        }
    )
    entry["files"] = files
    entry["bytes"] = sum(item["bytes"] for item in files if is_int(item.get("bytes")))


def attention_order(item: Mapping[str, Any]) -> tuple[Any, ...]:
    order = item.get("order")
    return (
        SEVERITY.index(item["status"]),
        item["lane"],
        item["rocm_version"],
        order is None,
        order or 0,
        item["case_key"],
        item["variant"],
        item["round"],
    )


def case_reason(case: Mapping[str, Any]) -> str:
    if case["status"] == "skip":
        return str(case["skip_reason"] or "")
    if case["status"] == "not-run":
        return "no record"
    return str(case["error_class"])


def pair_ratio(
    pair: Mapping[str, Any],
    cases: Sequence[Mapping[str, Any]],
    samples: Mapping[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    """One declared ratio of one run: the medians over rounds, point by point."""
    medians: dict[str, dict[tuple[int, int], float]] = {}
    state: dict[str, str] = {}
    for side in ("num", "den"):
        seen: dict[tuple[int, int], list[float]] = {}
        state[side] = "absent"
        for case in cases:
            if case["case_key"] != pair[side] or case["variant"] != "head":
                continue
            if case["round"] == 0:
                state[side] = case["status"]
            if case["status"] != "pass":
                continue
            for sample in samples.get(case["case_uid"], []):
                if sample["bw_gbps"] is not None:
                    point = (sample["block_size"], sample["batch_size"])
                    seen.setdefault(point, []).append(sample["bw_gbps"])
        medians[side] = {
            point: statistics.median(values) for point, values in seen.items()
        }
    points = []
    for block, batch in sorted(set(medians["num"]) & set(medians["den"])):
        num, den = medians["num"][block, batch], medians["den"][block, batch]
        try:
            value = float(num) / float(den)
        except (OverflowError, ZeroDivisionError):
            continue
        if den > 0 and math.isfinite(value):
            points.append([block, batch, round(num, 6), round(den, 6), round(value, 4)])
    ratio: dict[str, Any] = {key: pair[key] for key in ("key", "kind", "num", "den")}
    if points:
        ratio.update(status="ok", points=points)
        return ratio
    why = "no common rows"
    for side in ("num", "den"):
        if state[side] != "pass":
            why = f"{side} {state[side]}"
            break
    ratio.update(status="unavailable", why=why, points=[])
    return ratio


def matrix_lane(matrix: Mapping[str, Any], lane: str) -> Mapping[str, Any]:
    lanes = matrix.get("lanes")
    found = lanes.get(lane) if isinstance(lanes, dict) else None
    return found if isinstance(found, dict) else {}


def lane_pairs(matrix: Mapping[str, Any], lane: str) -> list[dict[str, Any]]:
    pairs = matrix_lane(matrix, lane).get("pairs")
    return [
        pair
        for pair in (pairs if isinstance(pairs, list) else [])
        if isinstance(pair, dict)
        and all(isinstance(pair.get(key), str) for key in ("key", "kind", "num", "den"))
    ]


def docs_url(repo: str, branch: str) -> str:
    return f"https://github.com/{repo}/blob/{branch}/.ci/docs/amd-bench.md"


def shard_links(entry: Mapping[str, Any], repo: str, docs: str) -> dict[str, Any]:
    base = f"https://github.com/{repo}"
    raw = entry["raw"]
    assets = f"{base}/releases/download/{raw['tag']}"
    sha, source = entry["sha"], entry["nightly_run_id"]
    return {
        "actions_run": (
            f"{base}/actions/runs/{entry['bench_run_id']}"
            f"/attempts/{entry['run_attempt']}"
        ),
        "source_run": (
            f"{base}/actions/runs/{source}"
            if isinstance(source, str) and RUN_ID_RE.fullmatch(source)
            else None
        ),
        "commit": (
            f"{base}/commit/{sha}"
            if isinstance(sha, str) and SHA1_RE.fullmatch(sha)
            else None
        ),
        "raw_release": f"{base}/releases/tag/{raw['tag']}",
        "raw_records": f"{assets}/{raw['records']}",
        "raw_logs": (
            f"{assets}/bench-{entry['id']}.logs.tar.gz" if raw["logs"] else None
        ),
        "raw_collect": (
            f"{assets}/bench-{entry['id']}.collect.json" if raw["collect"] else None
        ),
        "docs": docs,
        "data": entry["path"],
        "matrix": f"matrix/{entry['matrix_sha256']}.json",
    }


def build_index(
    entry: Mapping[str, Any],
    repo: str,
    docs: str,
    view: View,
    matrix: Mapping[str, Any],
    collect: Optional[Mapping[str, Any]],
    notes: Sequence[str],
) -> dict[str, Any]:
    """The index.json of a shard: what a page needs, with no file list in it."""
    primary = view.primary
    lanes: list[dict[str, Any]] = []
    attention: list[dict[str, Any]] = []
    tests_attention: list[dict[str, Any]] = []
    ratios: list[dict[str, Any]] = []
    groups: dict[tuple[str, str, tuple[str, ...]], list[str]] = {}
    total = [0] * len(lib.CASE_STATUSES)
    tests_total = [0] * len(lib.TEST_STATUSES)
    for run in view.kept:
        uid = run["run_uid"]
        cases, tests = view.cases[uid], view.tests[uid]
        where = {"lane": run["lane"], "rocm_version": run["rocm_version"]}
        counts = tally(lib.CASE_STATUSES, cases)
        test_counts = tally(lib.TEST_STATUSES, tests)
        total, tests_total = add(total, counts), add(tests_total, test_counts)
        blockers = matrix_lane(matrix, run["lane"]).get("blocked_by")
        lanes.append(
            {
                **where,
                "run_uid": uid,
                "status": run["status"],
                "env_class": run["env_class"],
                "timing_trust": run["timing_trust"],
                "n": counts,
                "tests": test_counts,
                "filtered": filtered(cases),
                "lane_blocked_by": (
                    [str(code) for code in blockers]
                    if isinstance(blockers, list)
                    else []
                ),
                **{
                    key: run[key]
                    for key in (
                        "calib_gbps",
                        "degraded",
                        "runner_image",
                        "guest_kernel",
                        "started_at",
                        "finished_at",
                    )
                },
            }
        )
        for case in cases:
            if case["status"] == "blocked":
                key = (run["lane"], run["rocm_version"], tuple(case["blocked_by"]))
                groups.setdefault(key, []).append(case["case_key"])
            elif case["status"] in SEVERITY and case["skip_reason"] != "filter":
                item = {**where, "run_uid": uid, "reason": case_reason(case)}
                item.update(
                    (key, case[key]) for key in ATTENTION_FIELDS if key != "reason"
                )
                attention.append(item)
        tests_attention += [
            {
                **where,
                "run_uid": uid,
                **{
                    key: test[key]
                    for key in (
                        "suite",
                        "test_id",
                        "status",
                        "error_excerpt",
                        "duration_s",
                    )
                },
            }
            for test in tests
            if test["status"] in ("fail", "timeout")
        ]
        ratios += [
            {**where, **pair_ratio(pair, cases, view.samples)}
            for pair in lane_pairs(matrix, run["lane"])
        ]
    started = [run["started_at"] for run in view.kept if run["started_at"]]
    finished = [run["finished_at"] for run in view.kept if run["finished_at"]]
    return {
        "schema": lib.SCHEMA,
        "schema_minor": lib.SCHEMA_MINOR,
        "shard": {
            **{
                key: entry[key]
                for key in (
                    "id",
                    "tier",
                    "stamp",
                    "slug",
                    "branch",
                    "sha",
                    "bench_run_id",
                    "run_attempt",
                    "nightly_run_id",
                    "reason",
                    "matrix_sha256",
                )
            },
            "repo": repo,
            "nixl_version": primary["nixl_version"],
            "nixlbench_version": primary["nixlbench_version"],
            "upstream_main_sha": primary["upstream_main_sha"],
            "features": primary["features"],
            "started_at": min(started) if started else None,
            "finished_at": max(finished) if finished else None,
        },
        "links": shard_links(entry, repo, docs),
        "totals": named(lib.CASE_STATUSES, total),
        "tests": named(lib.TEST_STATUSES, tests_total),
        "lanes": lanes,
        "attention": sorted(attention, key=attention_order),
        "tests_attention": sorted(
            tests_attention,
            key=lambda item: (
                item["lane"],
                item["rocm_version"],
                item["suite"],
                item["test_id"],
            ),
        ),
        "blocked": [
            {
                "lane": lane,
                "rocm_version": rocm,
                "blocked_by": list(codes),
                "cases": len(keys),
                "case_keys": sorted(keys),
            }
            for (lane, rocm, codes), keys in sorted(groups.items())
        ],
        "ratios": ratios,
        "collect": dict(collect) if collect is not None else None,
        "warnings": [*view.warnings, *notes],
    }


def dump_json(obj: Any) -> bytes:
    return (lib.canonical_json(obj) + "\n").encode("ascii")


# --- manifest -------------------------------------------------------------


def manifest_text(manifest: Mapping[str, Any]) -> bytes:
    """One line per shard, so that a publish changes few lines of the blob."""
    lines = ["{"]
    lines += [
        f"{json.dumps(key)}:{lib.canonical_json(manifest[key])},"
        for key in sorted(manifest)
        if key != "shards"
    ]
    lines.append('"shards":[')
    if manifest["shards"]:
        lines.append(",\n".join(lib.canonical_json(s) for s in manifest["shards"]))
    lines += ["]", "}"]
    return ("\n".join(lines) + "\n").encode("ascii")


def check_entry(entry: Any) -> str:
    """Why a manifest entry cannot be worked with; empty when it can."""
    if not isinstance(entry, dict):
        return "an entry is not an object"
    name = parse_id(entry["id"]) if isinstance(entry.get("id"), str) else None
    if name is None:
        return "an entry has no valid id"
    tier = entry.get("tier")
    if tier not in TIER_RANK or entry.get("path") != shard_path(name, tier):
        return f"{name.id}: tier or path does not fit the id"
    if (entry.get("stamp"), entry.get("slug")) != (name.stamp, name.slug) or (
        entry.get("bench_run_id"),
        entry.get("run_attempt"),
    ) != (name.run, name.attempt):
        return f"{name.id}: the identity fields do not fit the id"
    raw = entry.get("raw")
    if (
        not isinstance(raw, dict)
        or raw.get("tag") != name.tag
        or raw.get("records") != name.records
        or not isinstance(raw.get("sha256"), str)
        or not isinstance(raw.get("logs"), bool)
        or not isinstance(raw.get("collect"), bool)
    ):
        return f"{name.id}: bad 'raw'"
    sha = entry.get("matrix_sha256")
    if not isinstance(sha, str) or not HEX64_RE.fullmatch(sha):
        return f"{name.id}: bad 'matrix_sha256'"
    lanes = entry.get("lanes")
    if (
        not isinstance(lanes, dict)
        or not lanes
        or not all(
            isinstance(lane, dict)
            and lane.get("status") in lib.RUN_STATUSES
            and isinstance(lane.get("n"), list)
            and len(lane["n"]) == len(lib.CASE_STATUSES)
            and all(is_int(count) for count in lane["n"])
            for lane in lanes.values()
        )
    ):
        return f"{name.id}: bad 'lanes'"
    files = entry.get("files")
    if not isinstance(files, list) or not all(
        isinstance(item, dict)
        and isinstance(item.get("name"), str)
        and isinstance(item.get("sha256"), str)
        and is_int(item.get("bytes"))
        for item in files
    ):
        return f"{name.id}: bad 'files'"
    if not isinstance(entry.get("superseded_by"), dict):
        return f"{name.id}: bad 'superseded_by'"
    return ""


def check_manifest(doc: Any) -> dict[str, Any]:
    if not isinstance(doc, dict) or doc.get("schema") != lib.SCHEMA:
        raise Unusable(f"the manifest is not a schema {lib.SCHEMA} manifest")
    repo = doc.get("repo")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise Unusable("the manifest names no repository")
    if not is_int(doc.get("generation")) or doc["generation"] < 0:
        raise Unusable("the manifest has no generation")
    if not isinstance(doc.get("shards"), list):
        raise Unusable("the manifest has no shard list")
    quarantine = doc.get("quarantine", [])
    if not isinstance(quarantine, list) or not all(
        isinstance(item, dict) and isinstance(item.get("id"), str)
        for item in quarantine
    ):
        raise Unusable("the manifest has a bad quarantine list")
    seen: set[str] = set()
    for entry in doc["shards"]:
        why = check_entry(entry)
        if not why and entry["id"] in seen:
            why = f"{entry['id']}: listed twice"
        if why:
            raise Unusable(f"the manifest is damaged: {why}")
        seen.add(entry["id"])
    return doc


def supersede(entries: Sequence[dict[str, Any]]) -> None:
    """Sets superseded_by and supersedes: a pure function of each night."""
    nights: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for entry in entries:
        entry["superseded_by"] = {}
        entry["supersedes"] = []
        root = entry["path"].split("/")[0]
        nights.setdefault((root, entry["stamp"], entry["slug"]), []).append(entry)
    for night in nights.values():
        losers: dict[str, set[str]] = {}
        for key in sorted({key for entry in night for key in entry["lanes"]}):
            ranked = [
                (
                    (
                        int(entry["lanes"][key]["status"] != "missing"),
                        TIER_RANK[entry["tier"]],
                        int(not entry["lanes"][key].get("filtered")),
                    ),
                    entry,
                )
                for entry in night
                if key in entry["lanes"]
            ]
            best = max(rank for rank, _ in ranked)
            top = [entry for rank, entry in ranked if rank == best]
            winner = max(top, key=entry_order)
            if winner["lanes"][key]["status"] == "blocked":
                # The first run that found the lane blocked keeps it, so that
                # a later partial dispatch takes nothing from the full run.
                winner = min(
                    (
                        entry
                        for entry in top
                        if entry["lanes"][key]["status"] == "blocked"
                        and entry["matrix_sha256"] == winner["matrix_sha256"]
                    ),
                    key=entry_order,
                )
            for _, entry in ranked:
                if entry is not winner:
                    entry["superseded_by"][key] = winner["id"]
                    losers.setdefault(winner["id"], set()).add(entry["id"])
        for entry in night:
            entry["supersedes"] = sorted(losers.get(entry["id"], ()))


def manifest_order(entry: Mapping[str, Any]) -> tuple[Any, ...]:
    run, attempt = entry_order(entry)
    # Stamp descending, slug ascending, order descending.
    return (
        [-ord(char) for char in entry["stamp"]],
        entry["slug"],
        -run,
        -attempt,
        entry["id"],
    )


# --- latest ---------------------------------------------------------------


def winning(entry: Mapping[str, Any]) -> list[str]:
    return [key for key in entry["lanes"] if key not in entry["superseded_by"]]


def night_totals(night: Sequence[Mapping[str, Any]]) -> list[int]:
    total = [0] * len(lib.CASE_STATUSES)
    for entry in night:
        for key in winning(entry):
            total = add(total, entry["lanes"][key]["n"])
    return total


def failing(counts: Sequence[int]) -> int:
    return sum(counts[lib.CASE_STATUSES.index(status)] for status in FAILING)


def pick_headline(
    nights: Mapping[str, list[dict[str, Any]]],
) -> tuple[str, str]:
    """(stamp, 'nightly' or 'manual') of the night a branch is shown by."""
    nightly = [
        stamp
        for stamp, night in nights.items()
        if any(entry.get("reason") == "nightly" for entry in night)
    ]
    if nightly:
        return max(nightly), "nightly"

    def worth(stamp: str) -> tuple[bool, int, str]:
        lanes = [
            entry["lanes"][key] for entry in nights[stamp] for key in winning(entry)
        ]
        # Every lane the night holds counts, whatever became of it: a full
        # run that lost a lane is still the run to look at, and a run of a
        # few lanes still holds fewer.
        return (not any(lane.get("filtered") for lane in lanes), len(lanes), stamp)

    return max(nights, key=worth), "manual"


def fallback_lane(key: str, lane: Mapping[str, Any]) -> dict[str, Any]:
    name, _, rocm = key.partition("/")
    blank: dict[str, Any] = dict.fromkeys(LANE_FIELDS)
    blank.update(
        lane=name,
        rocm_version=rocm,
        status=lane["status"],
        n=list(lane["n"]),
        tests=[0] * len(lib.TEST_STATUSES),
        filtered=bool(lane.get("filtered")),
        lane_blocked_by=[],
    )
    return blank


def build_latest(
    entries: Sequence[dict[str, Any]],
    index_of: Callable[[dict[str, Any]], Optional[Mapping[str, Any]]],
    generation: int,
    stale_days: int,
) -> dict[str, Any]:
    newest = max((entry["stamp"] for entry in entries), default="")
    slugs: dict[str, dict[str, dict[str, list[dict[str, Any]]]]] = {}
    for entry in entries:
        root = entry["path"].split("/")[0]
        slugs.setdefault(entry["slug"], {}).setdefault(root, {}).setdefault(
            entry["stamp"], []
        ).append(entry)
    branches: dict[str, Any] = {}
    for slug, roots in slugs.items():
        nights = roots.get("night") or roots["weekly"]
        last = max(stamp for root in roots.values() for stamp in root)
        stamp, headline = pick_headline(nights)
        # The shard with most winning lanes speaks for the night.
        night = sorted(
            (entry for entry in nights[stamp] if winning(entry)),
            key=lambda entry: (-len(winning(entry)), [-n for n in entry_order(entry)]),
        )
        lanes: list[dict[str, Any]] = []
        attention: list[dict[str, Any]] = []
        tests_attention: list[dict[str, Any]] = []
        ratios: list[dict[str, Any]] = []
        tests = [0] * len(lib.TEST_STATUSES)
        for entry in night:
            index = index_of(entry) or {}
            keys = set(winning(entry))
            mark = {"shard": entry["id"]}

            def mine(item: Mapping[str, Any]) -> bool:
                return lane_key(item) in keys

            known = {lane_key(lane): lane for lane in index.get("lanes", [])}
            for key in sorted(keys, key=lambda k: tuple(k.split("/"))):
                lane = known.get(key) or fallback_lane(key, entry["lanes"][key])
                lanes.append({**lane, **mark})
                tests = add(tests, lane["tests"])
            attention += [
                {
                    **mark,
                    **{key: item[key] for key in SLIM_FIELDS},
                    "order": item["order"],
                }
                for item in index.get("attention", [])
                if mine(item) and item["status"] in FAILING
            ]
            tests_attention += [
                {
                    **mark,
                    **{
                        key: item[key]
                        for key in (
                            "lane",
                            "rocm_version",
                            "suite",
                            "test_id",
                            "status",
                        )
                    },
                }
                for item in index.get("tests_attention", [])
                if mine(item)
            ]
            ratios += [
                {**item, **mark} for item in index.get("ratios", []) if mine(item)
            ]
        attention.sort(key=attention_order)
        for item in attention:
            del item["order"]
            item["error_excerpt"] = lib.clip(item["error_excerpt"], LATEST_EXCERPT)
        lanes.sort(key=lambda lane: (lane["lane"], lane["rocm_version"]))
        tests_attention.sort(
            key=lambda item: (
                item["lane"],
                item["rocm_version"],
                item["suite"],
                item["test_id"],
            )
        )
        newer = None
        later = max((other for other in nights if other > stamp), default=None)
        if later is not None:
            counts = night_totals(nights[later])
            newer = {
                "stamp": later,
                "shards": [
                    entry["id"]
                    for entry in sorted(nights[later], key=manifest_order)
                    if winning(entry)
                ],
                "reasons": sorted(
                    {str(entry.get("reason") or "unknown") for entry in nights[later]}
                ),
                "totals": named(lib.CASE_STATUSES, counts),
                "failing": failing(counts),
            }
        lead = night[0]
        branches[slug] = {
            "branch": lead["branch"],
            "tier": lead["tier"],
            "stamp": stamp,
            "sha": lead["sha"],
            "headline": headline,
            # By the branch's newest run, which need not be the headline.
            "stale": stamp_time(newest) - stamp_time(last)
            > datetime.timedelta(days=stale_days),
            "newer": newer,
            "shards": [entry["id"] for entry in night],
            "totals": named(lib.CASE_STATUSES, night_totals(night)),
            "tests": named(lib.TEST_STATUSES, tests),
            "lanes": lanes,
            "attention": attention[:LATEST_ATTENTION],
            "attention_total": len(attention),
            "tests_attention": tests_attention[:LATEST_TESTS],
            "tests_attention_total": len(tests_attention),
            "ratios": ratios,
        }
    return {
        "schema": lib.SCHEMA,
        "schema_minor": lib.SCHEMA_MINOR,
        "generation": generation,
        "branches": branches,
    }


# --- the site -------------------------------------------------------------


def site_path(site: pathlib.Path, relative: str) -> pathlib.Path:
    """site/relative, refused when a symbolic link would lead elsewhere."""
    path = site
    for part in pathlib.PurePosixPath(relative).parts:
        path = path / part
        if path.is_symlink():
            raise Unusable(f"{relative}: a symbolic link is in the way")
    return path


def site_read(site: pathlib.Path, relative: str) -> Optional[bytes]:
    path = site_path(site, relative)
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError as exc:
        raise Unusable(f"cannot read {relative}: {exc.strerror}") from exc


def write_bytes(path: pathlib.Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    scratch = path.with_name(path.name + ".tmp")
    scratch.write_bytes(data)
    os.replace(scratch, path)


def tree_bytes(site: pathlib.Path) -> dict[str, int]:
    sizes = {}
    for prefix in (DATA_ROOT, PAGES_ROOT):
        total = 0
        for directory, _, names in os.walk(site / prefix):
            for name in names:
                path = pathlib.Path(directory, name)
                if regular(path):
                    total += path.stat().st_size
        sizes[prefix] = total
    return sizes


def site_records(
    site: pathlib.Path, entry: Mapping[str, Any]
) -> dict[str, list[dict[str, Any]]]:
    """The records of a published shard, each file checked against the manifest."""
    listed = {item["name"]: item for item in entry["files"]}
    records: dict[str, list[dict[str, Any]]] = {}
    for kind, stem in KIND_FILES:
        name = f"{stem}.ndjson.gz"
        data = site_read(site, f"{DATA_ROOT}/{entry['path']}/{name}")
        if data is None or name not in listed or sha256(data) != listed[name]["sha256"]:
            raise Conflict(f"{entry['id']}: {name} is not the file the manifest lists")
        try:
            lines = gunzip(data, RAW_MAX).decode("ascii").splitlines()
            records[kind] = [json.loads(line) for line in lines]
        except (Invalid, ValueError) as exc:
            raise Conflict(f"{entry['id']}: {name} is unreadable") from exc
    if not records["run"]:
        raise Conflict(f"{entry['id']}: runs.ndjson.gz holds no run")
    return records


def read_assets(path: Optional[pathlib.Path], notes: list[str]) -> dict[str, set[str]]:
    """The asset names of each release tag, from JSON lines of {tag, name}."""
    assets: dict[str, set[str]] = {}
    if path is None:
        return assets
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError) as exc:
        raise Unusable(f"cannot read --assets {path}: {exc}") from exc
    bad = 0
    for line in filter(str.strip, lines):
        try:
            item = json.loads(line)
        except ValueError:
            item = None
        if (
            isinstance(item, dict)
            and isinstance(item.get("tag"), str)
            and isinstance(item.get("name"), str)
        ):
            assets.setdefault(item["tag"], set()).add(item["name"])
        else:
            bad += 1
    if bad:
        notes.append(f"{bad} line(s) of --assets are not {{tag, name}} objects")
    return assets


def usable_index(index: Mapping[str, Any]) -> bool:
    """Whether an index.json from the site has what latest.json takes from it."""
    needs = {
        "lanes": ("tests",),
        "attention": (*SLIM_FIELDS, "order"),
        "tests_attention": ("suite", "test_id", "status"),
        "ratios": (),
    }
    for key, fields in needs.items():
        items = index.get(key)
        if not isinstance(items, list) or not all(
            isinstance(item, dict)
            and isinstance(item.get("lane"), str)
            and isinstance(item.get("rocm_version"), str)
            and all(name in item for name in fields)
            for item in items
        ):
            return False
    return all(
        isinstance(lane["tests"], list)
        and len(lane["tests"]) == len(lib.TEST_STATUSES)
        and all(is_int(count) for count in lane["tests"])
        for lane in index["lanes"]
    ) and all(
        item["status"] in SEVERITY
        and isinstance(item["error_excerpt"], str)
        and isinstance(item["case_key"], str)
        and isinstance(item["variant"], str)
        and is_int(item["round"])
        and (item["order"] is None or is_int(item["order"]))
        for item in index["attention"]
    )


@dataclass
class Plan:
    """Every file a publish wants on the site, in the order it is written."""

    site: pathlib.Path
    files: dict[str, tuple[int, bytes]] = field(default_factory=dict)

    def put(self, rank: int, relative: str, data: bytes) -> None:
        self.files[relative] = (rank, data)

    def differing(self) -> list[tuple[str, bytes]]:
        ordered = sorted(self.files.items(), key=lambda item: item[1][0])
        return [
            (relative, data)
            for relative, (_, data) in ordered
            if site_read(self.site, relative) != data
        ]


def git_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if key not in GIT_LOCATION}


def git(site: pathlib.Path, *args: str, stdin: Optional[bytes] = None) -> bytes:
    try:
        done = subprocess.run(
            ["git", "-C", str(site), *args],
            input=stdin,
            capture_output=True,
            env=git_env(),
            check=False,
        )
    except OSError as exc:
        raise Unusable(f"cannot run git: {exc.strerror}") from exc
    if done.returncode != 0:
        text = done.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise Unusable(f"git {args[0]} failed: {plain(text[-1] if text else '')}")
    return done.stdout


# --- publish --------------------------------------------------------------


@dataclass
class Publish:
    """The state of one 'publish': what is on the site and what was asked."""

    args: argparse.Namespace
    site: pathlib.Path
    now: str
    schema: dict[str, Any]
    manifest: Optional[dict[str, Any]] = None
    repo: str = ""
    entries: list[dict[str, Any]] = field(default_factory=list)
    new: dict[str, Shard] = field(default_factory=dict)
    loaded: dict[str, Shard] = field(default_factory=dict)
    healed: set[str] = field(default_factory=set)
    indexes: dict[str, dict[str, Any]] = field(default_factory=dict)
    plan: Plan = field(init=False)
    noop: list[str] = field(default_factory=list)
    rebuilt: list[str] = field(default_factory=list)
    skipped: list[dict[str, str]] = field(default_factory=list)
    quarantined: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    latest: dict[str, Any] = field(default_factory=dict)
    final: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.plan = Plan(self.site)

    @property
    def docs(self) -> str:
        return docs_url(self.repo, self.args.default_branch)

    def warn(self, text: str) -> None:
        self.warnings.append(text)
        say(f"warning: {text}")

    def skip(self, what: str, why: str) -> None:
        self.skipped.append({"what": what, "why": why})
        self.warn(f"skipped {what}: {why}")

    # -- intake

    def load(self) -> None:
        """Every input checked; nothing is written before this returns."""
        matrix_dir = site_path(self.site, f"{DATA_ROOT}/matrix")
        limits = (self.args.max_bytes, self.args.default_branch, self.args.repo)
        strict: dict[str, Shard] = {}
        for directory in self.args.input:
            try:
                shards = load_input(directory, self.schema, matrix_dir, *limits)
                for shard in shards:
                    old = strict.get(shard.name.id)
                    if old is not None and old.raw != shard.raw:
                        raise Invalid(
                            f"{shard.name.records}: another input holds other "
                            "bytes under this name"
                        )
                    strict.setdefault(shard.name.id, shard)
            except Invalid as exc:
                raise Invalid(f"--input {directory}: {exc}") from exc
        heal: dict[str, Shard] = {}
        for directory in self.args.heal:
            for path in records_files(directory):
                # A strict input of the same name is the one that counts.
                if path.name in {shard.name.records for shard in strict.values()}:
                    continue
                try:
                    shard = load_shard(path, self.schema, matrix_dir, *limits)
                except Invalid as exc:
                    self.skip(f"--heal {path}", str(exc))
                    continue
                except Exception as exc:
                    # An archived file this tool trips over costs only itself.
                    self.skip(
                        f"--heal {path}", f"internal error ({type(exc).__name__})"
                    )
                    continue
                heal.setdefault(shard.name.id, shard)
        self.healed = set(heal)
        self.loaded = {**heal, **strict}

        repos = {shard.repo for shard in strict.values()}
        if self.manifest is not None:
            self.repo = self.manifest["repo"]
        elif self.args.repo is not None:
            self.repo = self.args.repo
        elif repos:
            self.repo = sorted(repos)[0]
        elif heal:
            self.repo = next(iter(heal.values())).repo
        if repos - {self.repo}:
            raise Unusable(
                f"the site holds the results of {self.repo}; an input is of "
                "another repository"
            )

    def classify(self) -> None:
        listed = {entry["id"]: entry for entry in self.entries}
        quarantine = {
            item["id"] for item in (self.manifest or {}).get("quarantine", [])
        }
        for shard_id, shard in self.loaded.items():
            healed = shard_id in self.healed
            if healed and shard.repo != self.repo:
                self.skip(f"--heal {shard.source}", "it is of another repository")
            elif shard_id in quarantine:
                self.quarantined.append(shard_id)
                say(f"notice: {shard_id} is quarantined; not published")
            elif shard_id not in listed:
                self.new[shard_id] = shard
            elif listed[shard_id]["raw"]["sha256"] == shard.raw_sha256:
                self.noop.append(shard_id)
            elif healed:
                self.skip(f"--heal {shard.source}", "other bytes are published")
            else:
                raise Conflict(
                    f"{shard_id} is published with other raw records "
                    f"(sha256 {listed[shard_id]['raw']['sha256'][:12]})"
                )

    # -- shards

    @staticmethod
    def raw_flags(
        name: Name, assets: Mapping[str, set[str]]
    ) -> Optional[tuple[bool, bool]]:
        """(logs, collect) as the release listing has them, if it has the tag."""
        names = assets.get(name.tag)
        if names is None:
            return None
        return name.logs in names, name.collect in names

    def add_shard(self, shard: Shard, assets: Mapping[str, set[str]]) -> None:
        name = shard.name
        logs, collect = shard.logs is not None, shard.collect is not None
        notes = list(shard.notes)
        if name.id in self.healed:
            # The archive is what a healed shard links to, not the download.
            logs = False
            notes = [note for note in notes if not note.startswith(LOGS_NOTE)]
            logs, collect = self.raw_flags(name, assets) or (logs, collect)
        entry = base_entry(shard, logs, collect)
        view = build_view(shard.records, name.run)
        index = build_index(
            entry, self.repo, self.docs, view, shard.matrix, shard.collect, notes
        )
        data = dump_json(index)
        set_index_file(entry, data)
        page = pages.render_run(index).encode("utf-8")
        base = f"{DATA_ROOT}/{entry['path']}"
        files = [(0, f"{DATA_ROOT}/matrix/{shard.matrix_sha}.json", shard.matrix_bytes)]
        files += [
            (1, f"{base}/{file}", content) for file, content in shard.files.items()
        ]
        files.append((2, f"{base}/index.json", data))
        for rank, relative, content in files:
            if rank < 2 or not self.args.rerender:
                self.adopt(relative, content)
        # Nothing above changed this publish, so a shard that fails up to
        # here can be left out whole, as a healed one is.
        for rank, relative, content in files:
            self.plan.put(rank, relative, content)
        self.plan.put(4, pages.run_page_path(name.id), page)
        size = sum(map(len, shard.files.values()))
        if size > self.args.warn_bytes:
            self.warn(f"{name.id}: {size} bytes of shard data")
        for note in index["warnings"]:
            self.warn(f"{name.id}: {note}")
        self.indexes[name.id] = index
        self.entries.append(entry)

    def add_healed(self, shard: Shard, assets: Mapping[str, set[str]]) -> None:
        """add_shard for a shard of the archive: it may fail, the publish not."""
        try:
            self.add_shard(shard, assets)
        except Unusable:
            raise
        except Exception as exc:
            del self.new[shard.name.id]
            known = isinstance(exc, (Conflict, Invalid))
            why = str(exc) if known else f"internal error ({type(exc).__name__})"
            self.skip(f"--heal {shard.source}", why)

    def adopt(self, relative: str, data: bytes) -> None:
        """A file of an unlisted shard already on the site must be this one."""
        if site_read(self.site, relative) not in (None, data):
            raise Conflict(f"{relative} exists with other content")

    def site_matrix(self, sha: str) -> dict[str, Any]:
        relative = f"{DATA_ROOT}/matrix/{sha}.json"
        planned = self.plan.files.get(relative)
        data = planned[1] if planned else site_read(self.site, relative)
        matrix = json_object(data) if data is not None else None
        if data is None or sha256(data) != sha or matrix is None:
            raise Conflict(f"{relative} is missing or is not the matrix of its name")
        return matrix

    def old_index(self, entry: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        data = site_read(self.site, f"{DATA_ROOT}/{entry['path']}/index.json")
        return json_object(data) if data is not None else None

    def rebuild(
        self, entry: dict[str, Any], assets: Mapping[str, set[str]]
    ) -> dict[str, Any]:
        """index.json of a published shard anew, from its files; the entry too."""
        name = parse_id(entry["id"])
        assert name is not None
        view = build_view(site_records(self.site, entry), name.run)
        old = self.old_index(entry) or {}
        carried = old.get("collect")
        collect = collect_summary(carried) if isinstance(carried, dict) else None
        flags = self.raw_flags(name, assets)
        if flags is not None:
            entry["raw"]["logs"], entry["raw"]["collect"] = flags
        for directory in [*self.args.heal, *self.args.input]:
            _, found, _ = find_collect(directory, name)
            if found is not None:
                collect = found
                if flags is None and directory in self.args.heal:
                    entry["raw"]["collect"] = True
                break
        notes = [
            note
            for note in old.get("warnings", [])
            if isinstance(note, str)
            and (
                (note.startswith(LOGS_NOTE) and not entry["raw"]["logs"])
                or (note.startswith(COLLECT_NOTE) and not entry["raw"]["collect"])
            )
        ]
        entry.update(manifest_fields(view))
        return build_index(
            entry,
            self.repo,
            self.docs,
            view,
            self.site_matrix(entry["matrix_sha256"]),
            collect,
            notes,
        )

    def rerender(self, assets: Mapping[str, set[str]]) -> None:
        for entry in self.entries:
            if entry["id"] in self.new:
                continue
            index = self.rebuild(entry, assets)
            data = dump_json(index)
            relative = f"{DATA_ROOT}/{entry['path']}/index.json"
            size = len(data) + sum(
                item["bytes"] for item in entry["files"] if item["name"] != "index.json"
            )
            if size > self.args.max_bytes:
                self.warn(f"{entry['id']}: {size} bytes, over the shard budget")
            set_index_file(entry, data)
            self.plan.put(2, relative, data)
            self.indexes[entry["id"]] = index

    def index_of(self, entry: dict[str, Any]) -> Optional[Mapping[str, Any]]:
        """The index of a shard for latest.json: memory, the site, or rebuilt."""
        shard_id = entry["id"]
        if shard_id not in self.indexes:
            index = self.old_index(entry)
            if index is None or not usable_index(index):
                self.warn(
                    f"{shard_id}: index.json is unreadable or incomplete; "
                    "--rerender is due"
                )
                try:
                    index = self.rebuild(copy.deepcopy(entry), {})
                except (Conflict, Unusable, KeyError, TypeError) as exc:
                    self.warn(f"{shard_id}: cannot be summarised: {plain(exc)}")
                    index = {}
            self.indexes[shard_id] = index
        return self.indexes[shard_id]

    # -- documents

    def documents(self, generation: int, updated_at: str) -> tuple[bytes, bytes]:
        for shard_id in self.new:
            entry = next(item for item in self.entries if item["id"] == shard_id)
            entry["generation"] = generation
            entry["published_at"] = self.now
        old = self.manifest or {}
        minor: int = old["schema_minor"] if is_int(old.get("schema_minor")) else 0
        manifest = {
            **old,
            "schema": lib.SCHEMA,
            # A later tool may have raised it, as when it added Parquet files.
            "schema_minor": max(lib.SCHEMA_MINOR, minor),
            "generation": generation,
            "updated_at": updated_at,
            "repo": self.repo,
            "statuses": list(lib.CASE_STATUSES),
            "test_statuses": list(lib.TEST_STATUSES),
            "links": {"repo": f"https://github.com/{self.repo}", "docs": self.docs},
            "quarantine": old.get("quarantine", []),
            "shards": self.entries,
        }
        latest = build_latest(
            self.entries, self.index_of, generation, self.args.stale_days
        )
        self.latest, self.final = latest, manifest
        return manifest_text(manifest), dump_json(latest)

    def run(self) -> dict[str, Any]:
        if not BRANCH_NAME_RE.fullmatch(self.args.default_branch):
            raise Unusable("--default-branch has a forbidden form")
        wanted = self.args.repo
        if wanted is not None and not REPO_RE.fullmatch(wanted):
            raise Unusable("--repo is not OWNER/NAME")
        doc = site_read(self.site, MANIFEST)
        if doc is not None:
            parsed = json_object(doc)
            if parsed is None:
                raise Unusable("the manifest on the site is not a JSON object")
            self.manifest = check_manifest(parsed)
            if wanted is not None and self.manifest["repo"] != wanted:
                raise Unusable("the site holds the results of another repository")
        old_generation = self.manifest["generation"] if self.manifest else 0
        self.entries = copy.deepcopy(self.manifest["shards"]) if self.manifest else []
        self.load()
        self.classify()
        notes: list[str] = []
        assets = read_assets(self.args.assets, notes)
        for note in notes:
            self.warn(note)
        before = tree_bytes(self.site)
        for shard_id, shard in list(self.new.items()):
            if shard_id in self.healed:
                self.add_healed(shard, assets)
            else:
                self.add_shard(shard, assets)
        if self.args.rerender:
            self.rerender(assets)
        result = blank_result(old_generation)
        result.update(
            noop=self.noop,
            rebuilt=self.rebuilt,
            skipped=self.skipped,
            quarantined=self.quarantined,
            warnings=self.warnings,
            tree_bytes=before,
        )
        if not self.entries:
            return result
        if not REPO_RE.fullmatch(self.repo):
            raise Unusable("no repository is known for this site")

        supersede(self.entries)
        self.entries.sort(key=manifest_order)
        updated = self.manifest.get("updated_at", "") if self.manifest else ""
        manifest, latest = self.documents(old_generation, updated)
        self.plan.put(3, LATEST, latest)
        self.plan.put(6, MANIFEST, manifest)
        generation = old_generation
        if self.plan.differing():
            generation = old_generation + 1
            manifest, latest = self.documents(generation, self.now)
            self.plan.put(3, LATEST, latest)
            self.plan.put(6, MANIFEST, manifest)

        rendered = set(self.new) | (set(self.indexes) if self.args.rerender else set())
        for entry in self.entries:
            page = pages.run_page_path(entry["id"])
            if page in self.plan.files:
                continue
            if entry["id"] in rendered:
                text = pages.render_run(self.indexes[entry["id"]])
                self.plan.put(4, page, text.encode("utf-8"))
            elif not site_path(self.site, page).is_file():
                self.warn(f"{entry['id']}: its run page is missing; --rerender is due")
        landing = pages.render_landing(self.final, self.latest)
        self.plan.put(5, pages.LANDING, landing.encode("utf-8"))

        todo = self.plan.differing()
        for relative, _ in todo:
            if not relative.startswith(OWN_PREFIXES):
                raise RuntimeError(f"{relative} is outside the publisher's paths")
        written = []
        for relative, data in todo:
            write_bytes(site_path(self.site, relative), data)
            written.append(relative)
        after = tree_bytes(self.site)
        changed = set(written)
        if self.args.rerender:
            self.rebuilt += [
                entry["id"]
                for entry in self.entries
                if entry["id"] not in self.new
                and {
                    f"{DATA_ROOT}/{entry['path']}/index.json",
                    pages.run_page_path(entry["id"]),
                }
                & changed
            ]
        verify = [path for path in (pages.LANDING, LATEST) if path in changed]
        verify += sorted(
            path for path in changed if path.startswith(f"{PAGES_ROOT}/runs/")
        )[:VERIFY_PAGES]
        result.update(
            changed=bool(written),
            generation=generation,
            generation_moved=generation != old_generation,
            published=[
                {
                    "id": entry["id"],
                    "bytes": entry["bytes"],
                    "files": len(entry["files"]),
                }
                for entry in self.entries
                if entry["id"] in self.new
            ],
            bytes_added=sum(after.values()) - sum(before.values()),
            tree_bytes=after,
            superseded={
                entry["id"]: entry["superseded_by"]
                for entry in self.entries
                if entry["superseded_by"]
            },
            verify=[
                {"path": path, "sha256": sha256(self.plan.files[path][1])}
                for path in verify
            ],
        )
        return result


def blank_result(generation: int = 0) -> dict[str, Any]:
    return {
        "changed": False,
        "generation": generation,
        "generation_moved": False,
        "published": [],
        "noop": [],
        "rebuilt": [],
        "skipped": [],
        "quarantined": [],
        "warnings": [],
        "bytes_added": 0,
        "tree_bytes": {},
        "superseded": {},
        "verify": [],
    }


def write_outputs(args: argparse.Namespace, result: Mapping[str, Any]) -> None:
    """--report and --output, written anew whatever the publish did."""
    if args.report is not None:
        args.report.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    if args.output is not None:
        lines = {
            "changed": str(result["changed"]).lower(),
            "generation": str(result["generation"]),
            "generation_moved": str(result["generation_moved"]).lower(),
            "shards": " ".join(item["id"] for item in result["published"]),
            "bytes_added": str(result["bytes_added"]),
            "verify": " ".join(
                f"{item['path']}={item['sha256']}" for item in result["verify"]
            ),
        }
        args.output.write_text(
            "".join(f"{key}={value}\n" for key, value in lines.items()),
            encoding="utf-8",
        )


def cmd_publish(args: argparse.Namespace) -> int:
    try:
        site = args.site.resolve()
        if not site.is_dir():
            raise Unusable(f"--site {args.site} is not a directory")
        now = parse_now(args.now).strftime(NOW_FORMAT)
        state = Publish(args, site, now, lib.load_schema())
        result = state.run()
    except BaseException as exc:
        # A reader of the last attempt must not find what an earlier one wrote.
        with contextlib.suppress(OSError):
            write_outputs(args, {**blank_result(), "error": type(exc).__name__})
        raise
    write_outputs(args, result)
    ids = [item["id"] for item in result["published"]]
    print(
        f"{'published ' + ' '.join(ids) if ids else 'no new shard'}; "
        f"{'changed' if result['changed'] else 'nothing changed'}; "
        f"generation {result['generation']}; {result['bytes_added']:+d} tree bytes"
    )
    return 0


# --- inspect --------------------------------------------------------------


def shard_assets(shard: Shard) -> list[tuple[str, pathlib.Path]]:
    """(asset name, file) of what the raw archive keeps of a shard."""
    assets = [(shard.name.records, shard.source)]
    if shard.logs is not None:
        assets.append((shard.name.logs, shard.logs))
    if shard.collect_path is not None:
        assets.append((shard.name.collect, shard.collect_path))
    if shard.matrix_path is not None:
        assets.append((f"matrix-{shard.matrix_sha}.json", shard.matrix_path))
    return assets


def cmd_inspect(args: argparse.Namespace) -> int:
    schema = lib.load_schema()
    valid: list[dict[str, Any]] = []
    invalid: list[dict[str, str]] = []
    rows = []
    seen: dict[str, bytes] = {}
    if args.repo is not None and not REPO_RE.fullmatch(args.repo):
        raise Unusable("--repo is not OWNER/NAME")
    for directory in args.input:
        where = str(directory)
        # <run id>/<artifact name>, as the workflow lays the downloads out.
        label = "/".join(directory.parts[-2:])
        try:
            shards = load_input(
                directory, schema, None, args.max_bytes, DEFAULT_BRANCH, args.repo
            )
            for shard in shards:
                if seen.get(shard.name.id, shard.raw) != shard.raw:
                    raise Invalid(
                        f"{shard.name.records}: another input holds other bytes "
                        "under this name"
                    )
        except Exception as exc:
            # Whatever one artifact does to this tool, the others are judged.
            why = (
                str(exc)
                if isinstance(exc, Invalid)
                else f"internal error ({type(exc).__name__})"
            )
            invalid.append({"input": where, "why": why})
            rows.append((label, "-", "invalid", "-", "-", "-", why))
            say(f"invalid: {where}: {why}")
            continue
        items = []
        for shard in shards:
            if shard.name.id in seen:
                say(f"notice: {where}: {shard.name.id} was in an earlier input")
                continue
            seen[shard.name.id] = shard.raw
            fields = manifest_fields(build_view(shard.records, shard.name.run))
            assets = []
            for name, source in shard_assets(shard):
                path = source
                if args.stage is not None:
                    path = args.stage / shard.name.tag / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, path)
                data = path.read_bytes()
                assets.append(
                    {
                        "name": name,
                        "path": str(path),
                        "bytes": len(data),
                        "sha256": sha256(data),
                    }
                )
            for note in shard.notes:
                say(f"warning: {shard.name.id}: {note}")
            items.append(
                {
                    "id": shard.name.id,
                    "tag": shard.name.tag,
                    "counts": {
                        "records": {
                            kind: len(shard.records[kind]) for kind, _ in KIND_FILES
                        },
                        "cases": named(lib.CASE_STATUSES, fields["n"]),
                        "tests": named(lib.TEST_STATUSES, fields["tests"]),
                    },
                    "bytes": shard.budget,
                    "logs": shard.logs is not None,
                    "collect": shard.collect is not None,
                    "warnings": shard.notes,
                    "assets": assets,
                }
            )
            rows.append(
                (
                    label,
                    shard.name.id,
                    "valid",
                    "/".join(map(str, fields["n"])),
                    str(shard.budget),
                    "yes" if shard.logs is not None else "no",
                    "; ".join(shard.notes),
                )
            )
        valid.append({"input": where, "shards": items})
    args.out.write_text(
        json.dumps({"valid": valid, "invalid": invalid}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    if args.summary_out is not None:
        head = ("Artifact", "Shard", "Result", "Cases", "Bytes", "Logs", "Note")
        lines = ["## nixlbench artifacts inspected", ""]
        lines.append("| " + " | ".join(head) + " |")
        lines.append("|" + "---|" * len(head))
        lines += [
            "| " + " | ".join(plain(cell, 300) for cell in row) + " |" for row in rows
        ]
        lines += ["", "Cases are " + "/".join(lib.CASE_STATUSES) + ".", ""]
        with open(args.summary_out, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines))
    print(f"{len(valid)} valid and {len(invalid)} invalid artifact(s)")
    return 0 if valid else 3


# --- check-staged ---------------------------------------------------------


def cmd_check_staged(args: argparse.Namespace) -> int:
    site = args.site.resolve()
    if not site.is_dir():
        raise Unusable(f"--site {args.site} is not a directory")
    top = git(site, "rev-parse", "--show-toplevel").decode("utf-8", "replace")
    if pathlib.Path(top.rstrip("\n")).resolve() != site:
        raise Unusable(f"--site {args.site} is not the top level of a work tree")

    problems: list[str] = []
    staged: set[str] = set()
    tokens = git(site, "diff", "--cached", "--name-status", "--no-renames", "-z")
    fields = tokens.decode("utf-8", "replace").split("\0")
    for status, path in zip(fields[0::2], fields[1::2]):
        staged.add(path)
        if not STAGED_RE.fullmatch(path):
            problems.append(f"{status} {path}: not a path of the bench publisher")
        elif status == "A" or (
            status == "M"
            and (path in MUTABLE or (args.rerender and DERIVED_RE.fullmatch(path)))
        ):
            continue
        elif status == "M":
            problems.append(f"M {path}: published data is never modified")
        else:
            problems.append(f"{status} {path}: only additions are allowed")

    tokens = git(site, "status", "--porcelain", "-z", "--untracked-files=all")
    items = iter(tokens.decode("utf-8", "replace").split("\0"))
    for item in items:
        if len(item) < 4:
            continue
        state, path = item[:2], item[3:]
        if "R" in state or "C" in state:
            next(items, None)
        if not path.startswith(OWN_PREFIXES):
            problems.append(f"{state.strip()} {path}: outside the publisher's paths")
        elif state[1] != " ":
            problems.append(f"{state.strip()} {path}: not staged")
    # Neither of the two listings above shows a file that an ignore rule hides.
    tokens = git(
        site,
        *("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
        *("--", DATA_ROOT, PAGES_ROOT),
    )
    for path in filter(None, tokens.decode("utf-8", "replace").split("\0")):
        problems.append(f"{path}: ignored by git, so not staged")

    blobs: dict[str, str] = {}
    tokens = git(site, "ls-files", "-s", "-z", "--", DATA_ROOT, PAGES_ROOT)
    for item in tokens.decode("utf-8", "replace").split("\0"):
        meta, _, path = item.partition("\t")
        if path in staged and len(meta.split()) == 3:
            mode, blob, _ = meta.split()
            blobs[path] = blob
            if mode != "100644":
                problems.append(f"{path}: mode {mode} is not that of a plain file")
    disk = 0
    if blobs:
        sizes = git(
            site,
            "cat-file",
            "--batch-check=%(objectsize:disk)",
            stdin="".join(f"{blob}\n" for blob in sorted(set(blobs.values()))).encode(),
        )
        disk = sum(int(size) for size in sizes.split() if size.isdigit())

    if args.output is not None:
        args.output.write_text(
            f"staged={len(staged)}\ndisk_bytes={disk}\n", encoding="utf-8"
        )
    for problem in problems:
        say(f"violation: {problem}")
    if problems:
        return 1
    print(f"{len(staged)} staged path(s), {disk} bytes stored")
    return 0


# --- pending --------------------------------------------------------------


def cmd_pending(args: argparse.Namespace) -> int:
    if str(args.manifest) == "-":
        text = sys.stdin.read()
    else:
        try:
            text = args.manifest.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        except (OSError, ValueError) as exc:
            raise Unusable(f"cannot read --manifest {args.manifest}: {exc}") from exc
    known: set[str] = set()
    if text.strip():
        try:
            doc = json.loads(text)
        except ValueError as exc:
            raise Unusable("--manifest is not JSON") from exc
        if not isinstance(doc, dict):
            raise Unusable("--manifest is not a JSON object")
        for key in ("shards", "quarantine"):
            items = doc.get(key, [])
            known |= {
                item["id"]
                for item in (items if isinstance(items, list) else [])
                if isinstance(item, dict) and isinstance(item.get("id"), str)
            }
    notes: list[str] = []
    assets = read_assets(args.assets, notes)
    for note in notes:
        say(f"warning: {note}")
    oldest = parse_now(args.now) - datetime.timedelta(days=args.days)
    waiting: dict[str, Name] = {}
    for tag, names in assets.items():
        for asset in names:
            match = RECORDS_RE.fullmatch(asset)
            name = parse_id(match.group(1)) if match else None
            if (
                name is not None
                and name.tag == tag
                and name.id not in known
                and stamp_time(name.stamp) >= oldest
            ):
                waiting[name.id] = name
    chosen = sorted(waiting.values(), key=lambda name: name.id)
    chosen.sort(key=lambda name: name.stamp, reverse=True)
    chosen = chosen[: max(args.limit, 0)]
    lines = []
    for name in chosen:
        lines.append(f"{name.tag}\t{name.records}")
        if name.collect in assets[name.tag]:
            lines.append(f"{name.tag}\t{name.collect}")
    for tag in dict.fromkeys(name.tag for name in chosen):
        lines += [
            f"{tag}\t{asset}"
            for asset in sorted(assets[tag])
            if MATRIX_NAME_RE.fullmatch(asset)
        ]
    sys.stdout.write("".join(f"{line}\n" for line in lines))
    return 0


# --- served ---------------------------------------------------------------


def fetch(url: str) -> tuple[str, Optional[bytes]]:
    """(status, body) of one GET; the body only with a 200."""
    request = urllib.request.Request(
        url, headers={"Cache-Control": "no-cache", "User-Agent": "amd-bench-publish"}
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            status = getattr(response, "status", None) or 200
            body = response.read(SERVED_MAX + 1)
    except urllib.error.HTTPError as exc:
        return str(exc.code), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return type(exc).__name__, None
    if status != 200 or len(body) > SERVED_MAX:
        return str(status) if status != 200 else "too large", None
    return "200", body


def cmd_served(args: argparse.Namespace) -> int:
    base = args.base_url if args.base_url.endswith("/") else args.base_url + "/"
    local = base.startswith("file://")
    if not local and not base.startswith(("https://", "http://")):
        raise Unusable("--base-url must be an https, http or file URL")
    expected: dict[str, str] = {}
    for item in args.expect:
        path, _, digest = item.partition("=")
        if not STAGED_RE.fullmatch(path) or not HEX64_RE.fullmatch(digest):
            raise Unusable(f"--expect {plain(item)}: want PATH=SHA256 of an own path")
        expected[path] = digest

    def generation(body: bytes) -> str:
        doc = json_object(body)
        found = doc.get("generation") if doc else None
        if not is_int(found):
            return "no generation in it"
        return "" if found >= args.generation else f"generation {found}"

    def parses(body: bytes) -> str:
        try:
            json.loads(body.decode("utf-8"))
        except ValueError:
            return "not JSON"
        return ""

    def address(path: str) -> str:
        query = "" if local else f"?g={args.generation}&t={int(time.time())}"
        # A directory of a local tree has no index of its own.
        return f"{base}{path or ('index.html' if local else '')}{query}"

    def later() -> bool:
        """Whether the site already serves a publication after this one."""
        _, body = fetch(address(MANIFEST))
        doc = json_object(body) if body is not None else None
        found = doc.get("generation") if doc else None
        return is_int(found) and found > args.generation

    def content(path: str, want: str) -> Callable[[bytes], str]:
        def test(body: bytes) -> str:
            # A later publication rewrote the mutable files, and rightly so.
            if sha256(body) == want or (path in MUTABLE and later()):
                return ""
            return "other content"

        return test

    def anything(body: bytes) -> str:
        return ""

    checks: list[tuple[str, Callable[[bytes], str]]] = [(MANIFEST, generation)]
    checks += [(path, content(path, digest)) for path, digest in expected.items()]
    checks += [
        (path, parses if must_parse else anything) for path, must_parse in CONTRACT_URLS
    ]
    deadline = time.monotonic() + args.timeout
    last: dict[int, str] = {}
    waiting = list(enumerate(checks))
    while True:
        still = []
        for number, (path, test) in waiting:
            status, body = fetch(address(path))
            why = test(body) if body is not None else status
            if why:
                last[number] = f"{base}{path}: {why}"
                still.append((number, (path, test)))
        waiting = still
        left = deadline - time.monotonic()
        if not waiting or left <= 0:
            break
        time.sleep(min(args.interval, left))
    for number, _ in waiting:
        say(f"not served: {last[number]}")
    if waiting:
        return 6
    print(f"{base} serves generation {args.generation} and {len(expected)} page(s)")
    return 0


# --- command line ---------------------------------------------------------


def parse_args(argv: Optional[list[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    path = pathlib.Path

    sub = commands.add_parser("inspect", help="check artifacts, each on its own")
    sub.add_argument("--input", type=path, action="append", required=True)
    sub.add_argument("--out", type=path, required=True, metavar="PLAN.json")
    sub.add_argument("--stage", type=path, metavar="DIR")
    sub.add_argument("--summary-out", type=path, metavar="FILE")
    sub.add_argument("--max-bytes", type=int, default=4 * MIB)
    sub.add_argument("--repo", metavar="OWNER/NAME")
    sub.set_defaults(run=cmd_inspect)

    sub = commands.add_parser("publish", help="add artifacts to a site checkout")
    sub.add_argument("--site", type=path, required=True, metavar="DIR")
    sub.add_argument("--input", type=path, action="append", default=[])
    sub.add_argument("--heal", type=path, action="append", default=[])
    sub.add_argument("--assets", type=path, metavar="FILE")
    sub.add_argument("--default-branch", default=DEFAULT_BRANCH, metavar="NAME")
    sub.add_argument("--rerender", action="store_true")
    sub.add_argument("--now", metavar="ISO")
    sub.add_argument("--report", type=path, metavar="FILE")
    sub.add_argument("--output", type=path, metavar="FILE")
    sub.add_argument("--warn-bytes", type=int, default=MIB)
    sub.add_argument("--max-bytes", type=int, default=4 * MIB)
    sub.add_argument("--stale-days", type=int, default=14)
    sub.add_argument("--repo", metavar="OWNER/NAME")
    sub.set_defaults(run=cmd_publish)

    sub = commands.add_parser("check-staged", help="guard the commit to the site")
    sub.add_argument("--site", type=path, required=True, metavar="DIR")
    sub.add_argument("--rerender", action="store_true")
    sub.add_argument("--output", type=path, metavar="FILE")
    sub.set_defaults(run=cmd_check_staged)

    sub = commands.add_parser("pending", help="archived artifacts not yet published")
    sub.add_argument("--manifest", type=path, required=True, metavar="FILE|-")
    sub.add_argument("--assets", type=path, required=True, metavar="FILE")
    sub.add_argument("--days", type=int, default=14)
    sub.add_argument("--limit", type=int, default=20)
    sub.add_argument("--now", metavar="ISO")
    sub.set_defaults(run=cmd_pending)

    sub = commands.add_parser("served", help="wait until the site serves a publish")
    sub.add_argument("--base-url", required=True, metavar="URL")
    sub.add_argument("--generation", type=int, required=True, metavar="N")
    sub.add_argument("--expect", action="append", default=[], metavar="PATH=SHA256")
    sub.add_argument("--timeout", type=float, default=900)
    sub.add_argument("--interval", type=float, default=15)
    sub.set_defaults(run=cmd_served)
    return parser.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    try:
        return int(args.run(args))
    except Invalid as exc:
        say(f"invalid: {exc}")
        return 3
    except Conflict as exc:
        say(f"conflict: {exc}")
        return 4
    except (Unusable, lib.BenchError) as exc:
        say(f"{exc}")
        return 2
    except OSError as exc:
        say(f"{exc}")
        return 2
    except Exception:
        for line in traceback.format_exc().splitlines():
            say(line)
        say("internal error")
        return 5


if __name__ == "__main__":
    sys.exit(main())
