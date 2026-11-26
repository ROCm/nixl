#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""A small copy of the results site, for the browser tests of .ci/site.

    amd_site_fixture.py --site DIR

DIR gets what gh-pages holds, written by the publishers themselves, so that a
publisher that changes the shape of its data is tested against the app that
reads it:

  data/nightly/, classic.html  amd_results_site.py, from the nights below
  data/bench/v1/, bench/       amd_bench_publish.py, from the bench fixture
  wheels/                      amd_wheel_index.py, from wheel files made here
  site.json                    a placeholder for what the site job writes

The nights are made here, in the shape the report job of amd-nightly.yml gives
nightly-summary.json. That job assembles the summary inline, so its shape is
copied, not shared: a change to it belongs here as well. The nights cover a
branch that passes, one night on which it fails, a branch that cannot build
ROCm wheels, and a branch that is no longer built.

Nothing in DIR depends on the clock. Standard library only.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import subprocess
import sys
import tempfile
import zipfile
from typing import Any, Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import amd_results_site as results_site  # noqa: E402

HERE = pathlib.Path(__file__).resolve().parent
BENCH_INPUT = HERE / "tests" / "fixtures" / "bench_publish" / "collected"
BENCH_NOW = "2026-11-21T00:10:00Z"
REPOSITORY = "ROCm/nixl"
WHEELS_BRANCH = "for-upstream/feat/rocm-wheels"
WHEELS_SLUG = "for-upstream-feat-rocm-wheels"
RETIRED_BRANCH = "main-amd"
UNSUPPORTED = "the branch cannot build ROCm wheels yet"
# The night the wheels branch fails: a transfer check of its rocjitsu test.
FAILED_NIGHT = 2
NIGHTS = 5
ROCM = ["7.14.1", "10.1.0", "10.2.0"]


class FixtureError(Exception):
    pass


def stamp(night: int) -> str:
    return f"202611{16 + night:02d}T033000Z"


def sha(text: str) -> str:
    return (text.encode("ascii").hex() * 40)[:40]


def check(
    name: str,
    status: str = "PASS",
    detail: str = "",
    duration: float = 0.1,
    size: Optional[int] = None,
    path: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "detail": detail,
        "duration_s": duration,
        "bytes": size,
        "path": path,
    }


def wheel(version: str, python: str) -> dict[str, Any]:
    tag = "cp" + python.replace(".", "")
    return {
        "file": f"rocm_nixl-{version}-{tag}-{tag}-manylinux_2_28_x86_64.whl",
        "bytes": 74_000_000 + 250_000 * int(version[-2:]) + int(python[-2:]),
        "sha256": sha(f"wheel-{version}-{python}") + sha(python)[:24],
        "python": python,
        "rocm": ["7.14", "10.1", "10.2"],
    }


def built(night: int) -> dict[str, Any]:
    """results.json of a branch that builds ROCm wheels, as rocm-wheels.yml has it."""
    failed = night == FAILED_NIGHT
    version = f"1.6.0.dev202611{16 + night:02d}"
    transfer = check(
        "transfer",
        "FAIL" if failed else "PASS",
        "data mismatch at offset 4096" if failed else "64 MiB written and read back",
        2.0 + night / 4,
        None if failed else 64 * 1024 * 1024,
        None if failed else "ucx/rc",
    )
    tests = [
        {
            "job": "test",
            "rocm": rocm,
            "script": "rocm_wheel_smoke_test.py",
            "checks": [
                check("import", detail="'import nixl' selected the ROCm build"),
                check("no-torch", detail="rocm-nixl has no torch requirement"),
                check("agent", duration=0.4 + night / 10),
            ],
        }
        for rocm in ROCM
    ]
    tests.append(
        {
            "job": "rocjitsu",
            "rocm": "10.2.0",
            "script": "rocm_wheel_smoke_test.py",
            "checks": [
                check("import"),
                check("device", detail="1 emulated device"),
                transfer,
                check("vram", "SKIP", "the emulated device has no VRAM path", 0.0),
            ],
        }
    )
    jobs = [
        ("plan", "success", 6.0 + night),
        ("build", "success", 1500.0 + 90 * night),
        ("merge", "success", 70.0 + night),
        ("test", "success", 60.0 + 2 * night),
        ("rocjitsu", "failure" if failed else "success", 190.0 + 5 * night),
        ("rocjitsu-2vm", "success", 380.0 - 4 * night),
    ]
    return {
        "schema": 1,
        "ref": WHEELS_BRANCH,
        "sha": sha(f"wheels-{night}"),
        "version": version,
        "workflow_run_id": 45_000_000_000 + night,
        "supported": True,
        "jobs": [
            {"name": name, "result": result, "duration_s": duration}
            for name, result, duration in jobs
        ],
        "wheels": [wheel(version, python) for python in ("3.10", "3.12")],
        "tests": tests,
        "nixlbench": {
            "required": False,
            "supported": True,
            "enabled": True,
            "result": "success",
            "artifact": f"rocm-nixlbench-wheel-{WHEELS_SLUG}",
            "wheel": {
                "file": f"rocm_nixlbench-{version}-py3-none-manylinux_2_28_x86_64.whl",
                "bytes": 1_811_490,
                "sha256": sha(f"bench-{version}") + sha("bench")[:24],
            },
            "rocm": ROCM,
        },
    }


def unsupported(ref: str, night: int) -> dict[str, Any]:
    """results.json of a branch whose plan job found it cannot build ROCm wheels."""
    skipped = ("build", "merge", "test", "rocjitsu", "rocjitsu-2vm")
    jobs = [{"name": "plan", "result": "success", "duration_s": 8.0}]
    jobs += [{"name": name, "result": "skipped", "duration_s": 0.0} for name in skipped]
    return {
        "schema": 1,
        "ref": ref,
        "sha": sha(f"{ref}-{night}"),
        "version": "1.6.0",
        "workflow_run_id": 45_000_000_000 + night,
        "supported": False,
        "jobs": jobs,
        "wheels": [],
        "tests": [],
        "nixlbench": {
            "required": False,
            "supported": False,
            "enabled": False,
            "result": "skipped",
            "artifact": None,
            "wheel": None,
            "rocm": [],
        },
    }


def branch(
    name: str, slug: str, results: dict[str, Any], reason: Optional[str]
) -> dict[str, Any]:
    return {
        "branch": name,
        "slug": slug,
        "sha": results["sha"],
        "passed": reason is None,
        "reason": reason,
        "wheels_artifact": f"rocm-nixl-wheels-{slug}",
        "results": results,
    }


def summary(night: int) -> dict[str, Any]:
    """One night's nightly-summary.json."""
    failed = night == FAILED_NIGHT
    branches = [
        branch("main", "main", unsupported("main", night), UNSUPPORTED),
        branch(
            WHEELS_BRANCH,
            WHEELS_SLUG,
            built(night),
            "failed: rocjitsu" if failed else None,
        ),
    ]
    if night == 0:
        retired = dict(built(night), ref=RETIRED_BRANCH)
        branches.append(branch(RETIRED_BRANCH, RETIRED_BRANCH, retired, None))
    return {
        "schema": 1,
        "run_id": 45_000_000_000 + night,
        "run_attempt": 1,
        "repository": REPOSITORY,
        "stamp": stamp(night),
        "upstream_main_sha": sha(f"upstream-{night}"),
        "sync": "success",
        "branches": branches,
    }


def publish_wheels(site: pathlib.Path, ref: str, slug: str, night: int) -> None:
    """The pip index of a branch, written by amd_wheel_index.py from wheel files."""
    results = built(night)
    version = results["version"]
    with tempfile.TemporaryDirectory(prefix="amd-site-fixture-") as scratch:
        files = []
        for entry in results["wheels"]:
            path = pathlib.Path(scratch) / entry["file"]
            with zipfile.ZipFile(path, "w") as archive:
                # A fixed date: the index records the digest of the file.
                archive.writestr(
                    zipfile.ZipInfo(
                        f"rocm_nixl-{version}.dist-info/METADATA",
                        (2026, 11, 16, 0, 0, 0),
                    ),
                    "Metadata-Version: 2.4\nName: rocm-nixl\n"
                    f"Version: {version}\nRequires-Python: >=3.10\n",
                )
            files.append(str(path))
        done = subprocess.run(
            [
                sys.executable,
                "-B",
                str(HERE / "amd_wheel_index.py"),
                "update",
                "--site",
                str(site),
                "--branch",
                slug,
                "--ref",
                ref,
                "--sha",
                results["sha"],
                "--date",
                f"2026-11-{16 + night:02d}T04:12:40Z",
                "--base-url",
                f"https://github.com/{REPOSITORY}/releases/download/nightly-{slug}",
                *files,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    if done.returncode != 0:
        raise FixtureError(f"amd_wheel_index.py update failed:\n{done.stderr}")


def build(site: pathlib.Path) -> None:
    site.mkdir(parents=True, exist_ok=True)
    if any(site.iterdir()):
        raise FixtureError(f"{site} is not empty")
    with tempfile.TemporaryDirectory(prefix="amd-site-fixture-") as scratch:
        for night in range(NIGHTS):
            path = pathlib.Path(scratch) / f"{stamp(night)}.json"
            path.write_text(json.dumps(summary(night), indent=1) + "\n")
            results_site.add(site, path)
    done = subprocess.run(
        [
            sys.executable,
            "-B",
            str(HERE / "amd_bench_publish.py"),
            "publish",
            "--site",
            str(site),
            "--input",
            str(BENCH_INPUT),
            "--now",
            BENCH_NOW,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if done.returncode != 0:
        raise FixtureError(f"amd_bench_publish.py publish failed:\n{done.stderr}")
    (site / ".nojekyll").write_text("")
    (site / results_site.APP_MARKER).write_text('{"schema": 1, "source_sha": ""}\n')
    # The branch that used to be published first: its index stays, older.
    publish_wheels(site, RETIRED_BRANCH, RETIRED_BRANCH, 0)
    publish_wheels(site, WHEELS_BRANCH, WHEELS_SLUG, NIGHTS - 1)
    name = results_site.render(site)
    if name != "classic.html":
        raise FixtureError(f"the page was rendered as {name}, not classic.html")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--site", required=True, type=pathlib.Path, metavar="DIR")
    args = parser.parse_args()
    try:
        build(args.site)
    except (FixtureError, results_site.SiteError, OSError) as exc:
        sys.stderr.write(f"amd_site_fixture.py: {exc}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
