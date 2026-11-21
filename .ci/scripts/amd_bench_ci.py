#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Job-side helper of the AMD bench workflows: the plan, env files, run-meta, E1.

    amd_bench_ci.py plan [--matrix DIR] --run FILE --artifacts FILE
        --manifests DIR [--summary FILE] [--lanes "LANE..."] [--tier TIER]
        [--only "GLOB..."] [--event NAME] [--guest-rocm X.Y]
        [--max-branches N] --output FILE
    amd_bench_ci.py env [--matrix DIR] --lane LANE --out FILE.json
        [--set PATH=VALUE]... [--withhold NEED]...
    amd_bench_ci.py meta --base JSON --out FILE [--set KEY=VALUE]...
        [--set-int KEY=N]...
    amd_bench_ci.py vram-timing --records FILE --out FILE
    amd_bench_ci.py guest-cpu --lane LANE --guest NAME --facts FILE
        [--cpuinfo FILE] [--notes FILE]

'plan' reads what a finished "AMD Nightly" or "ROCm Wheels" run left behind
and appends the job matrices of amd-nightly-bench.yml to --output, in the
form of $GITHUB_OUTPUT. 'env' renders the template of a lane's env class to
the JSON env file of amd_bench_run.py. 'meta' writes a --run-meta file.
'vram-timing' lists the wall time of every case that used GPU memory.
'guest-cpu' states the CPU a guest printed with 'amd_bench_guest.sh facts'
as a notice, and warns when the guest's kernel would refuse peer-to-peer DMA
on it.

'plan', 'env' and 'meta' exit 0, or 2 when what they were given cannot be
used; 'vram-timing' and 'guest-cpu' always exit 0. Run it as 'python3
amd_bench_ci.py', never with -I: it needs amd_bench_lib.py beside it and the
standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import tomllib
from dataclasses import dataclass
from typing import Any, Iterator, Mapping, Sequence

try:
    import amd_bench_lib as lib
except ImportError:
    sys.exit("amd_bench_lib.py must sit beside this script")

# The lanes amd-nightly-bench.yml has a job for, in the order they are planned.
LANES = ("bench-container", "vm1-local", "vm2-net", "vm1-loop", "vm1-fabric")
CONTAINER_LANE = "bench-container"
# A failure of any other lane is reported and does not fail its job.
HARD_LANES = ("bench-container", "vm1-local")
# The nightly runs 'nightly'; the others are started by hand.
PLAN_TIERS = lib.TIERS
JOB_VALUE = "@job"
NIXL_ARTIFACT = "rocm-nixl-wheels"
NIXLBENCH_ARTIFACT = "rocm-nixlbench-wheel"
MANIFEST = "nixlbench-build.json"
ARTIFACTS_FORMAT = (
    "one JSON object per line with 'id', 'name' and 'expired', as written by "
    "gh api --paginate .../actions/runs/RUN/artifacts "
    "--jq '.artifacts[] | {id, name, expired, created_at} | @json'"
)

SUFFIX_RE = re.compile(r"(-[A-Za-z0-9._-]+)?")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")
VERSION_RE = re.compile(r"[0-9][A-Za-z0-9.+!]{0,63}")
ROCM_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
ROCM_XY_RE = re.compile(r"[0-9]+\.[0-9]+")
ARTIFACT_RE = re.compile(r"[A-Za-z0-9._-]+")
REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
WHEEL_RE = re.compile(r"rocm_nixlbench-[A-Za-z0-9.+!_]+-py3-none-[A-Za-z0-9._]+\.whl")
STAMP_RE = re.compile(r"\d{8}T\d{6}Z")
CREATED_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)(?:\.\d+)?Z")
# No case key starts with a dash, and the runner would take such a glob for an
# option.
ONLY_RE = re.compile(r"(?!-)[A-Za-z0-9._+/*?\[\]-]+")
INT_RE = re.compile(r"-?[0-9]+")
NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
HOST_RE = re.compile(r"[A-Za-z0-9_-]+")
LANE_RE = re.compile(r"[a-z0-9][a-z0-9-]*")
EXPECT_RE = re.compile(
    r"[a-z0-9-]+=[0-9.]+(,[0-9.]+)*( [a-z0-9-]+=[0-9.]+(,[0-9.]+)*)*"
)

# What each value of a matrix element must look like. 'allow-failure' is the
# one value that is not a string, and 'meta' and 'lane' are checked apart.
ELEMENT_RES: dict[str, re.Pattern[str]] = {
    "slug": SLUG_RE,
    "suffix": SUFFIX_RE,
    "branch": BRANCH_RE,
    "sha": SHA_RE,
    "version": VERSION_RE,
    "rocm": ROCM_RE,
    "nixl-artifact": ARTIFACT_RE,
    "nixlbench-artifact": ARTIFACT_RE,
    "expect": EXPECT_RE,
}


class CiError(Exception):
    """What the helper was given cannot be used."""


class Unusable(Exception):
    """Why one branch of the source run gets no bench job."""


def clean(text: object) -> str:
    """text on one line, with nothing in it that starts a workflow command."""
    return " ".join(str(text).replace("::", ": :").split())


def say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def notice(title: str, text: str) -> None:
    say(f"::notice title={clean(title)}::{clean(text)}")


def warning(title: str, text: str) -> None:
    say(f"::warning title={clean(title)}::{clean(text)}")


def read_json(path: pathlib.Path, what: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CiError(f"cannot read {what} {path}: {exc}") from exc


def write_text(path: pathlib.Path, text: str) -> None:
    """Writes path whole, or not at all."""
    scratch = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        scratch.write_text(text, encoding="utf-8")
        os.replace(scratch, path)
    except OSError as exc:
        raise CiError(f"cannot write {path}: {exc}") from exc


def compact(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=True, sort_keys=True)


def is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


# --- plan -----------------------------------------------------------------


@dataclass(frozen=True)
class Branch:
    slug: str
    suffix: str
    branch: str
    sha: str
    version: str
    rocm: tuple[str, ...]
    nixl_artifact: str
    nixlbench_artifact: str
    nixlbench_sha256: str


def parse_lanes(text: str) -> list[str]:
    chosen: set[str] = set()
    for token in text.split() or ["all"]:
        if token == "all":
            chosen.update(LANES)
        elif token in LANES:
            chosen.add(token)
        else:
            # A rejected name is never echoed.
            raise CiError(
                "--lanes names a lane the bench workflow does not have; it "
                f"takes 'all' or any of: {' '.join(LANES)}"
            )
    return [name for name in LANES if name in chosen]


def parse_tier(text: str) -> str:
    tier = text.strip() or "nightly"
    if tier not in PLAN_TIERS:
        raise CiError(f"--tier must be one of: {' '.join(PLAN_TIERS)}")
    return tier


def parse_only(text: str) -> list[str]:
    globs = text.split()
    if not all(ONLY_RE.fullmatch(glob) for glob in globs):
        raise CiError(
            "--only takes case-key globs separated by spaces, made of letters, "
            "digits and . _ + / * ? [ ] -, none starting with a dash"
        )
    return globs


def stamp_of(created_at: Any) -> str | None:
    found = CREATED_RE.fullmatch(created_at) if isinstance(created_at, str) else None
    if found is None:
        return None
    year, month, day, hour, minute, second = found.groups()
    return f"{year}{month}{day}T{hour}{minute}{second}Z"


def read_run(path: pathlib.Path) -> tuple[int, str, str | None]:
    """(run id, repository, stamp of its creation) of the source run."""
    run = read_json(path, "the run")
    if not isinstance(run, dict):
        raise CiError(f"the run {path} is not a JSON object")
    run_id = run.get("id")
    if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id < 1:
        raise CiError(f"the run {path} has no numeric 'id': it is not a workflow run")
    repository = run.get("repository")
    repo = repository.get("full_name") if isinstance(repository, dict) else None
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not isinstance(repo, str) or not REPO_RE.fullmatch(repo):
        raise CiError(f"the run {path} does not name its repository")
    return run_id, repo, stamp_of(run.get("created_at"))


def read_artifacts(path: pathlib.Path) -> dict[str, dict[str, Any]]:
    """{name: entry} of the run's artifacts; of a name's entries, the newest."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CiError(f"cannot read the artifact list {path}: {exc}") from exc
    newest: dict[str, dict[str, Any]] = {}
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        usable = (
            isinstance(entry, dict)
            and isinstance(entry.get("name"), str)
            and is_int(entry.get("id"))
            and isinstance(entry.get("expired", False), bool)
        )
        if not usable:
            raise CiError(
                f"{path}:{number} is not an artifact entry; the file must hold "
                f"{ARTIFACTS_FORMAT}"
            )
        assert isinstance(entry, dict)
        # A re-run attempt uploads a name again: the highest id is the one a
        # download by name gets.
        if entry["name"] not in newest or entry["id"] > newest[entry["name"]]["id"]:
            newest[entry["name"]] = entry
    return newest


def listed(artifacts: Mapping[str, Mapping[str, Any]], name: str) -> bool:
    return name in artifacts and not artifacts[name].get("expired", False)


def manifest_path(manifests: pathlib.Path, name: str) -> pathlib.Path:
    """Where the action put the manifest of the artifact called name."""
    if (manifests / name).is_dir():
        return manifests / name / MANIFEST
    # A lone matching artifact may be extracted without its directory.
    return manifests / MANIFEST


def text_field(manifest: Mapping[str, Any], key: str, pattern: re.Pattern[str]) -> str:
    value = manifest.get(key)
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Unusable(f"its manifest has no usable '{key}'")
    return value


def read_branch(
    name: str,
    artifacts: Mapping[str, Mapping[str, Any]],
    manifests: pathlib.Path,
    run_id: int,
) -> Branch:
    """The branch whose nixlbench wheel is the artifact name, or Unusable."""
    suffix = name[len(NIXLBENCH_ARTIFACT) :]
    if not listed(artifacts, name):
        raise Unusable("the artifact has expired")
    path = manifest_path(manifests, name)
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        reason = exc.strerror if isinstance(exc, OSError) else "it is not JSON"
        raise Unusable(f"its manifest could not be read ({reason})") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != 1:
        raise Unusable("its manifest is not a schema 1 nixlbench-build.json")
    if manifest.get("artifact_suffix") != suffix:
        raise Unusable("its manifest was written for another artifact suffix")
    if not is_int(manifest.get("workflow_run_id")):
        raise Unusable("its manifest has no 'workflow_run_id'")
    if manifest["workflow_run_id"] != run_id:
        raise Unusable("its manifest was written by another workflow run")
    branch = text_field(manifest, "ref", BRANCH_RE)
    if ".." in branch:
        raise Unusable("its manifest has no usable 'ref'")
    text_field(manifest, "wheel", WHEEL_RE)
    rocm = manifest.get("rocm")
    if (
        not isinstance(rocm, list)
        or not rocm
        or not all(isinstance(v, str) and ROCM_RE.fullmatch(v) for v in rocm)
    ):
        raise Unusable("its manifest lists no ROCm version")
    nixl = NIXL_ARTIFACT + suffix
    if manifest.get("nixl_wheels_artifact", nixl) != nixl:
        raise Unusable("its manifest names another NIXL wheel artifact")
    if not listed(artifacts, nixl):
        raise Unusable(f"the run has no artifact {nixl}, or it has expired")
    slug = suffix[1:] if suffix else branch.replace("/", "-")
    if not SLUG_RE.fullmatch(slug):
        raise Unusable("its branch name gives no usable slug")
    return Branch(
        slug,
        suffix,
        branch,
        text_field(manifest, "sha", SHA_RE),
        text_field(manifest, "version", VERSION_RE),
        tuple(dict.fromkeys(rocm)),
        nixl,
        name,
        text_field(manifest, "sha256", SHA256_RE),
    )


def select_branches(
    artifacts: Mapping[str, Mapping[str, Any]],
    manifests: pathlib.Path,
    run_id: int,
    limit: int,
) -> list[Branch]:
    found: dict[str, Branch] = {}
    for name in sorted(artifacts):
        if not name.startswith(NIXLBENCH_ARTIFACT):
            continue
        if not SUFFIX_RE.fullmatch(name[len(NIXLBENCH_ARTIFACT) :]):
            continue
        try:
            branch = read_branch(name, artifacts, manifests, run_id)
        except Unusable as exc:
            notice("Bench branch not run", f"{name}: {exc}")
            continue
        if branch.slug in found:
            notice("Bench branch not run", f"{name}: its slug is already planned")
            continue
        found[branch.slug] = branch
    branches = [found[slug] for slug in sorted(found)]
    for branch in branches[limit:]:
        notice(
            "Bench branch not run",
            f"{branch.slug}: one run benches at most {limit} branches",
        )
    return branches[:limit]


def usable_lanes(matrix: lib.Matrix, wanted: Sequence[str], tier: str) -> list[str]:
    """The wanted lanes that have something to run in the tier."""
    kept = []
    for name in wanted:
        lane = matrix.lanes.get(name)
        if lane is None:
            why = "is not in the matrix"
        elif lane.status != "active":
            why = f"is {lane.status} in the matrix"
        elif not any(c.status == "active" for c in lib.select_cases(lane, tier)):
            why = f"has no active case in the {tier} tier"
        else:
            kept.append(name)
            continue
        notice("Bench lane not run", f"{name} {why}")
    return kept


def read_summary(
    path: pathlib.Path | None, run_id: int
) -> tuple[str | None, str | None]:
    """(stamp, upstream main sha) of the nightly's summary, where it has one."""
    if path is None:
        return None, None
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        notice("Bench plan", "the nightly summary could not be read and is not used")
        return None, None
    if not isinstance(summary, dict) or summary.get("run_id") != run_id:
        notice("Bench plan", "the nightly summary is of another run and is not used")
        return None, None
    stamp = summary.get("stamp")
    upstream = summary.get("upstream_main_sha")
    return (
        stamp if isinstance(stamp, str) and STAMP_RE.fullmatch(stamp) else None,
        upstream if isinstance(upstream, str) and SHA_RE.fullmatch(upstream) else None,
    )


def checked(element: dict[str, Any]) -> dict[str, Any]:
    """element, once every value of it is what a workflow may be handed."""
    for key, value in element.items():
        if key == "allow-failure":
            usable = isinstance(value, bool)
        elif key == "lane":
            usable = value in LANES
        elif key == "meta":
            usable = isinstance(value, str) and isinstance(json.loads(value), dict)
        else:
            usable = isinstance(value, str) and bool(ELEMENT_RES[key].fullmatch(value))
        if not usable:
            raise CiError(f"internal: the matrix value '{key}' is not usable")
    return element


def cmd_plan(args: argparse.Namespace) -> int:
    lanes = parse_lanes(args.lanes)
    tier = parse_tier(args.tier)
    only = parse_only(args.only)
    if not ROCM_XY_RE.fullmatch(args.guest_rocm):
        raise CiError("--guest-rocm must be X.Y")
    if args.max_branches < 1:
        raise CiError("--max-branches must be at least 1")
    try:
        matrix = lib.load_matrix(args.matrix)
    except lib.BenchError as exc:
        raise CiError(f"the matrix {args.matrix} cannot be used:\n{exc}") from exc
    run_id, repo, created = read_run(args.run)
    artifacts = read_artifacts(args.artifacts)
    summary_stamp, upstream = read_summary(args.summary, run_id)
    stamp = summary_stamp or created
    if stamp is None:
        raise CiError(f"the run {args.run} has no usable 'created_at'")

    lanes = usable_lanes(matrix, lanes, tier)
    vm_lanes = [name for name in lanes if name != CONTAINER_LANE]
    container: list[dict[str, Any]] = []
    vm: list[dict[str, Any]] = []
    collect: list[dict[str, Any]] = []
    branches = select_branches(artifacts, args.manifests, run_id, args.max_branches)
    for branch in branches if lanes else []:
        meta = compact(
            {
                "stamp": stamp,
                "nightly_run_id": str(run_id),
                "reason": "nightly" if args.event == "workflow_run" else "dispatch",
                "tier": tier,
                "repo": repo,
                "branch": branch.branch,
                "slug": branch.slug,
                "sha": branch.sha,
                "upstream_main_sha": upstream,
                "nixl_version": branch.version,
                "nixlbench_version": branch.version,
                "nixlbench_sha256": branch.nixlbench_sha256,
                "features": [],
            }
        )
        common = {
            "slug": branch.slug,
            "suffix": branch.suffix,
            "branch": branch.branch,
            "sha": branch.sha,
            "version": branch.version,
            "nixl-artifact": branch.nixl_artifact,
            "nixlbench-artifact": branch.nixlbench_artifact,
            "meta": meta,
        }
        expect = []
        if CONTAINER_LANE in lanes:
            expect.append(f"{CONTAINER_LANE}={','.join(branch.rocm)}")
            for rocm in branch.rocm:
                container.append(
                    checked(
                        {
                            **common,
                            "rocm": rocm,
                            "lane": CONTAINER_LANE,
                            "allow-failure": False,
                        }
                    )
                )
        guest = [v for v in branch.rocm if v.rsplit(".", 1)[0] == args.guest_rocm]
        if vm_lanes and not guest:
            notice(
                "Bench VM lanes not run",
                f"{branch.slug}: its nixlbench wheel has no ROCm {args.guest_rocm} "
                "backend, which is the only one the guest image can run",
            )
        for name in vm_lanes if guest else []:
            expect.append(f"{name}={guest[0]}")
            vm.append(
                checked(
                    {
                        **common,
                        "rocm": guest[0],
                        "lane": name,
                        "allow-failure": name not in HARD_LANES,
                    }
                )
            )
        if not expect:
            continue
        collect.append(
            checked(
                {
                    "slug": branch.slug,
                    "suffix": branch.suffix,
                    "branch": branch.branch,
                    "meta": meta,
                    "expect": " ".join(expect),
                }
            )
        )
        say(f"{branch.slug} ({branch.branch} at {branch.sha[:8]}): {' '.join(expect)}")

    if not collect:
        notice(
            "Nothing to bench",
            f"run {run_id} has no nixlbench wheel a selected lane can run, so no "
            "bench job is started",
        )
    lines = [
        f"tier={tier}",
        f"only={' '.join(only)}",
        f"count={len(collect)}",
        f"container={compact({'include': container})}",
        f"vm={compact({'include': vm})}",
        f"collect={compact({'include': collect})}",
    ]
    try:
        with open(args.output, "a", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    except OSError as exc:
        raise CiError(f"cannot write {args.output}: {exc}") from exc
    say(
        f"tier {tier}; {len(collect)} branch(es), {len(container)} container "
        f"job(s), {len(vm)} VM job(s)"
    )
    return 0


# --- env ------------------------------------------------------------------


def read_toml(path: pathlib.Path, what: str) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CiError(f"cannot read {what} {path}: {exc}") from exc


def split_assignment(option: str, item: str) -> tuple[str, str]:
    name, equals, value = item.partition("=")
    if not equals or not name:
        raise CiError(f"{option} wants NAME=VALUE, got '{clean(item)}'")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise CiError(f"{option} {name}: the value holds a control character")
    return name, value


def set_value(template: dict[str, Any], path: str, value: str) -> None:
    """Sets one of the three kinds of key a job may set, if the template has it."""
    parts = path.split(".")
    keys: list[str] | None = None
    if len(parts) == 2 and parts[0] == "vars" and NAME_RE.fullmatch(parts[1]):
        keys = parts
    elif len(parts) >= 3 and parts[0] == "hosts" and HOST_RE.fullmatch(parts[1]):
        if parts[2:] == ["ssh_config"]:
            keys = parts
        elif len(parts) == 4 and parts[2] == "vars" and NAME_RE.fullmatch(parts[3]):
            keys = parts
    if keys is None:
        raise CiError(
            f"--set {clean(path)}: a path is vars.NAME, hosts.HOST.ssh_config or "
            "hosts.HOST.vars.NAME"
        )
    table: Any = template
    for key in keys[:-1]:
        table = table.get(key) if isinstance(table, dict) else None
    if not isinstance(table, dict) or keys[-1] not in table:
        raise CiError(f"--set {path}: the template has no such key")
    table[keys[-1]] = value


def job_values(value: Any, where: str = "") -> Iterator[str]:
    """The paths of the values a job was to set and did not."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from job_values(item, f"{where}.{key}" if where else str(key))
    elif isinstance(value, list):
        for number, item in enumerate(value):
            yield from job_values(item, f"{where}[{number}]")
    elif value == JOB_VALUE:
        yield where


def cmd_env(args: argparse.Namespace) -> int:
    if not LANE_RE.fullmatch(args.lane):
        raise CiError("--lane is not a lane name")
    if args.out.suffix != ".json":
        raise CiError("--out must end in .json, which is how the runner reads it")
    lane = read_toml(args.matrix / "lanes" / f"{args.lane}.toml", "the lane")
    env_class = lane.get("env_class")
    if not isinstance(env_class, str) or not LANE_RE.fullmatch(env_class):
        raise CiError(f"lane {args.lane} has no usable 'env_class'")
    source = args.matrix / "envs" / f"{env_class}.toml"
    template = read_toml(source, "the env template")
    if template.get("env_class") != env_class:
        raise CiError(f"{source}: 'env_class' is not '{env_class}'")
    provides = template.get("provides", [])
    if not isinstance(provides, list):
        raise CiError(f"{source}: 'provides' is not a list")
    for item in args.set:
        set_value(template, *split_assignment("--set", item))
    unknown = sorted(set(args.withhold) - set(lib.NEEDS))
    if unknown:
        raise CiError(
            f"--withhold takes a need ({', '.join(lib.NEEDS)}), not "
            f"{', '.join(clean(name) for name in unknown)}"
        )
    template["provides"] = [need for need in provides if need not in args.withhold]
    left = list(job_values(template))
    if left:
        raise CiError(
            f"{source}: the job set no value for {', '.join(left)}; each is "
            f"still '{JOB_VALUE}'"
        )
    write_text(args.out, json.dumps(template, indent=1) + "\n")
    withheld = sorted(set(args.withhold).intersection(provides))
    say(
        f"{args.out}: env class {env_class}; provides "
        f"{', '.join(template['provides']) or 'nothing'}; withheld "
        f"{', '.join(withheld) or 'nothing'}"
    )
    return 0


# --- meta -----------------------------------------------------------------


def cmd_meta(args: argparse.Namespace) -> int:
    try:
        meta = json.loads(args.base)
    except ValueError as exc:
        raise CiError(f"--base is not JSON: {exc}") from exc
    if not isinstance(meta, dict):
        raise CiError("--base is not a JSON object")
    updates: list[tuple[str, Any]] = []
    for item in args.set:
        updates.append(split_assignment("--set", item))
    for item in args.set_int:
        key, value = split_assignment("--set-int", item)
        if not INT_RE.fullmatch(value):
            raise CiError(f"--set-int {key}: '{clean(value)}' is not an integer")
        updates.append((key, int(value)))
    meta.update(updates)
    unknown = sorted(str(key) for key in meta if key not in lib.META_KEYS)
    if unknown:
        raise CiError(
            f"not run-meta key(s): {', '.join(clean(key) for key in unknown)}"
        )
    write_text(args.out, json.dumps(meta, indent=1, sort_keys=True) + "\n")
    say(f"{args.out}: {', '.join(sorted(meta))}")
    return 0


# --- vram-timing ----------------------------------------------------------

WALL = "proc_wall_s."
ROLES = ("init", "tgt")


def vram_report(records: pathlib.Path) -> str:
    cases: list[dict[str, Any]] = []
    walls: dict[str, dict[str, Any]] = {}
    for _, record, _ in lib.read_ndjson(records):
        if record is None:
            continue
        if record.get("kind") == "case":
            if "VRAM" in (record.get("init_mem"), record.get("tgt_mem")):
                cases.append(record)
        elif record.get("kind") == "counter":
            name = record.get("name")
            if isinstance(name, str) and name.startswith(WALL):
                uid = str(record.get("case_uid"))
                walls.setdefault(uid, {})[name[len(WALL) :]] = record.get("value")

    def wall(case: Mapping[str, Any], role: str) -> str:
        value = walls.get(str(case.get("case_uid")), {}).get(role)
        return f"{value:.3f}" if isinstance(value, (int, float)) else "-"

    asio = sum(case.get("error_class") == "asio" for case in cases)
    ran = [case for case in cases if str(case.get("case_uid")) in walls]
    lines = [
        "E1: wall time of the cases that used GPU memory (VRAM)",
        f"records: {records}",
        f"VRAM cases: {len(cases)}; launched: {len(ran)}; error_class asio: {asio}",
    ]
    if ran:
        first = ran[0]
        lines.append(
            f"first VRAM case launched: {first.get('case_key')}: status "
            f"{first.get('status')}, proc_wall_s init {wall(first, 'init')} tgt "
            f"{wall(first, 'tgt')}"
        )
    else:
        lines.append("first VRAM case launched: none")
    lines += [
        "A DV or VD case that ends with error_class asio after about 15 s of wall",
        "time is the ASIO wait running out while the other side allocates on the",
        "emulated GPU (E1), not a fault of the wheel.",
        "",
        "case_key\tvariant\tround\tstatus\terror_class\tskip_reason\t"
        + "\t".join(f"{WALL}{role}" for role in ROLES),
    ]
    for case in cases:
        fields = [
            case.get("case_key"),
            case.get("variant"),
            case.get("round"),
            case.get("status"),
            case.get("error_class") or "-",
            case.get("skip_reason") or "-",
            *(wall(case, role) for role in ROLES),
        ]
        lines.append("\t".join(clean(field) for field in fields))
    return "\n".join(lines) + "\n"


def cmd_vram_timing(args: argparse.Namespace) -> int:
    """Never fails its step: whatever happens, there is a report or a reason."""
    try:
        if args.records.is_file():
            text = vram_report(args.records)
        else:
            text = f"E1: no records at {args.records}, so no VRAM case to report\n"
    except Exception as exc:
        text = f"E1: the records at {args.records} could not be read: {exc!r}\n"
    try:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    except OSError as exc:
        sys.stderr.write(f"amd_bench_ci.py: cannot write {args.out}: {exc}\n")
    say(text.rstrip("\n"))
    return 0


# --- guest-cpu ------------------------------------------------------------

CPU_KEYS = ("CPU_VENDOR", "CPU_FAMILY", "CPU_MODEL", "CPU_MODEL_NAME")
PLAIN_RE = re.compile(r"[A-Za-z0-9_.-]+")
# cpu_supports_p2pdma() in the kernel's drivers/pci/p2pdma.c: across a host
# bridge, peer-to-peer DMA is allowed on an AMD CPU of family Zen or later.
P2PDMA_VENDOR = "AuthenticAMD"
P2PDMA_FAMILY = 23
P2PDMA_REFUSED = (
    "its kernel is expected to refuse peer-to-peer DMA between the GPU and "
    "another device, so the cases that move data between VRAM and the NIC or "
    "an NVMe drive are expected to fail on this runner. No case is skipped "
    "for it."
)


def guest_cpu(text: str) -> dict[str, str]:
    """The last plain value a guest printed for each CPU_ key."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        key, equals, value = line.partition("=")
        if equals and key in CPU_KEYS and PLAIN_RE.fullmatch(value):
            found[key] = value
    return {key: value for key, value in found.items() if value != "unknown"}


def runner_cpu(text: str) -> str:
    """Vendor and model name of the first processor of a cpuinfo file."""
    first: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            break
        key, _, value = line.partition(":")
        first.setdefault(key.strip(), value.strip())
    parts = [first.get("vendor_id", ""), first.get("model name", "")]
    return ", ".join(part for part in parts if part) or "unknown"


def cpu_messages(guest: str, cpu: Mapping[str, str], runner: str) -> tuple[str, str]:
    """(what the guest's CPU is, why peer-to-peer DMA is in doubt or nothing)."""
    vendor, family = cpu.get("CPU_VENDOR"), cpu.get("CPU_FAMILY")
    stated = (
        f"{guest} reports the CPU vendor {vendor or 'unknown'}, family "
        f"{family or 'unknown'}, model {cpu.get('CPU_MODEL', 'unknown')} "
        f"({cpu.get('CPU_MODEL_NAME', 'unknown')}); the runner's CPU is {runner}"
    )
    if vendor is None or family is None or not family.isascii() or not family.isdigit():
        doubt = (
            f"the CPU of {guest} could not be read, so whether its kernel allows "
            "peer-to-peer DMA between the GPU and another device is not known"
        )
    elif vendor != P2PDMA_VENDOR:
        doubt = (
            f"the CPU vendor of {guest} is {vendor}, not {P2PDMA_VENDOR}: "
            f"{P2PDMA_REFUSED}"
        )
    elif int(family) < P2PDMA_FAMILY:
        doubt = (
            f"the CPU family of {guest} is {family}, not {P2PDMA_FAMILY} (Zen) "
            f"or later: {P2PDMA_REFUSED}"
        )
    else:
        doubt = ""
    return stated, doubt


def cmd_guest_cpu(args: argparse.Namespace) -> int:
    """Never fails its step: it records and warns, and gates nothing."""
    lane = args.lane if LANE_RE.fullmatch(args.lane) else "lane"
    guest = args.guest if HOST_RE.fullmatch(args.guest) else "the guest"
    texts = []
    for path in (args.facts, args.cpuinfo):
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            texts.append("")
    stated, doubt = cpu_messages(guest, guest_cpu(texts[0]), runner_cpu(texts[1]))
    title = f"AMD bench {lane} guest CPU"
    notice(title, stated)
    if doubt:
        warning(title, doubt)
    if args.notes is not None:
        try:
            with open(args.notes, "a", encoding="utf-8") as stream:
                stream.write(
                    "".join(f"{clean(text)}\n" for text in (stated, doubt) if text)
                )
        except OSError as exc:
            sys.stderr.write(f"amd_bench_ci.py: cannot write {args.notes}: {exc}\n")
    return 0


# --- command line ---------------------------------------------------------


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    default_matrix = pathlib.Path(__file__).resolve().parent.parent / "bench"
    path = pathlib.Path

    plan = commands.add_parser("plan")
    plan.set_defaults(handler=cmd_plan)
    plan.add_argument("--matrix", type=path, default=default_matrix, metavar="DIR")
    plan.add_argument("--run", type=path, required=True, metavar="FILE")
    plan.add_argument("--artifacts", type=path, required=True, metavar="FILE")
    plan.add_argument("--manifests", type=path, required=True, metavar="DIR")
    plan.add_argument("--summary", type=path, metavar="FILE")
    plan.add_argument("--lanes", default="")
    plan.add_argument("--tier", default="")
    plan.add_argument("--only", default="")
    plan.add_argument("--event", default="workflow_dispatch")
    plan.add_argument("--guest-rocm", default="10.1", metavar="X.Y")
    plan.add_argument("--max-branches", type=int, default=4, metavar="N")
    plan.add_argument("--output", type=path, required=True, metavar="FILE")

    env = commands.add_parser("env")
    env.set_defaults(handler=cmd_env)
    env.add_argument("--matrix", type=path, default=default_matrix, metavar="DIR")
    env.add_argument("--lane", required=True)
    env.add_argument("--out", type=path, required=True, metavar="FILE.json")
    env.add_argument("--set", action="append", default=[], metavar="PATH=VALUE")
    env.add_argument("--withhold", action="append", default=[], metavar="NEED")

    meta = commands.add_parser("meta")
    meta.set_defaults(handler=cmd_meta)
    meta.add_argument("--base", required=True, metavar="JSON")
    meta.add_argument("--out", type=path, required=True, metavar="FILE")
    meta.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    meta.add_argument("--set-int", action="append", default=[], metavar="KEY=N")

    timing = commands.add_parser("vram-timing")
    timing.set_defaults(handler=cmd_vram_timing)
    timing.add_argument("--records", type=path, required=True, metavar="FILE")
    timing.add_argument("--out", type=path, required=True, metavar="FILE")

    cpu = commands.add_parser("guest-cpu")
    cpu.set_defaults(handler=cmd_guest_cpu)
    cpu.add_argument("--lane", required=True)
    cpu.add_argument("--guest", required=True, metavar="NAME")
    cpu.add_argument("--facts", type=path, required=True, metavar="FILE")
    cpu.add_argument("--cpuinfo", type=path, default=path("/proc/cpuinfo"))
    cpu.add_argument("--notes", type=path, metavar="FILE")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        code: int = args.handler(args)
    except CiError as exc:
        for line in str(exc).splitlines():
            sys.stderr.write(f"amd_bench_ci.py: {line}\n")
        say(f"::error title=amd_bench_ci.py {args.command}::{clean(exc)}")
        return 2
    return code


if __name__ == "__main__":
    sys.exit(main())
