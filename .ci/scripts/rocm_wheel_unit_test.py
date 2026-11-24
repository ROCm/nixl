#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run the nixl C++ unit tests shipped in the nixl-tests artifact.

Each test binary is run as a subprocess with a timeout.  Results are written as
JSON using the same Reporter format as rocm_wheel_smoke_test.py so the existing
rocm-wheels results pipeline picks them up without changes.

    rocm_wheel_unit_test.py --tests-dir DIR [--json-out FILE] [--timeout S]
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from typing import Any, Callable, Optional


class Reporter:
    """Identical contract to the one in rocm_wheel_smoke_test.py."""

    def __init__(
        self,
        script: str = "",
        expect_rocm: str = "",
        json_out: Optional[str] = None,
    ) -> None:
        self.failed = False
        self.json_out = json_out
        self.begun = self.mark = time.monotonic()
        self.pending: Optional[dict[str, Any]] = None
        self.record: dict[str, Any] = {
            "script": script,
            "expect_rocm": expect_rocm,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_s": 0.0,
            "checks": [],
        }
        if json_out:
            try:
                self._write()
            except OSError as exc:
                sys.exit(f"cannot write --json-out {json_out}: {exc}")

    def _write(self) -> None:
        if not self.json_out:
            return
        self.record["duration_s"] = round(time.monotonic() - self.begun, 6)
        scratch = f"{self.json_out}.tmp"
        with open(scratch, "w", encoding="utf-8") as fh:
            json.dump(self.record, fh, indent=1)
            fh.write("\n")
        os.replace(scratch, self.json_out)

    def _save(self) -> None:
        try:
            self._write()
        except OSError as exc:
            sys.stderr.write(f"cannot write --json-out {self.json_out}: {exc}\n")

    def line(self, status: str, name: str, detail: str) -> None:
        if status == "FAIL":
            self.failed = True
        print(f"{status} {name}: {detail}", flush=True)
        now = time.monotonic()
        if self.pending is not None and self.pending["name"] == name:
            self.record["checks"].remove(self.pending)
            self.pending = None
        self.record["checks"].append(
            {
                "name": name,
                "status": status,
                "detail": str(detail),
                "duration_s": round(now - self.mark, 6),
                "bytes": None,
                "path": None,
            }
        )
        self.mark = now
        self._save()

    def run(self, name: str, check: Callable[[], str]) -> bool:
        self.pending = {
            "name": name,
            "status": "FAIL",
            "detail": "started but did not finish",
            "duration_s": 0.0,
            "bytes": None,
            "path": None,
        }
        self.record["checks"].append(self.pending)
        self._save()
        try:
            detail = check()
            self.line("PASS", name, detail)
            return True
        except CheckFailed as exc:
            self.line("FAIL", name, str(exc))
            return False
        except Exception as exc:  # noqa: BLE001
            self.line("FAIL", name, f"unexpected error: {exc}")
            return False


class CheckFailed(Exception):
    pass


def run_binary(
    path: str,
    args: Optional[list[str]] = None,
    env: Optional[dict[str, str]] = None,
    timeout: int = 120,
) -> str:
    """Run a test binary and return a brief result string; raise CheckFailed on failure."""
    cmd = [path] + (args or [])
    merged_env = {**os.environ, **(env or {})}
    try:
        result = subprocess.run(
            cmd,
            env=merged_env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        raise CheckFailed(f"timed out after {timeout}s")
    if result.returncode != 0:
        tail = (result.stdout + result.stderr).strip().splitlines()
        snippet = " | ".join(tail[-5:]) if tail else "(no output)"
        raise CheckFailed(f"exit {result.returncode}: {snippet}")
    lines = result.stdout.strip().splitlines()
    return lines[-1] if lines else "exit 0"


def check_ucx_backend_test(tests_dir: str, timeout: int) -> str:
    return run_binary(
        os.path.join(tests_dir, "bin", "ucx_backend_test"), timeout=timeout
    )


def check_ucx_backend_multi(tests_dir: str, timeout: int) -> str:
    return run_binary(
        os.path.join(tests_dir, "bin", "ucx_backend_multi"), timeout=timeout
    )


def check_ucx_worker_test(tests_dir: str, timeout: int) -> str:
    return run_binary(
        os.path.join(tests_dir, "bin", "ucx_worker_test"), timeout=timeout
    )


def check_gtest_ucx(tests_dir: str, timeout: int) -> str:
    env = {"UCX_ROCM_COPY_SIGPOOL_MAX_ELEMS": "32768"}
    return run_binary(
        os.path.join(tests_dir, "bin", "gtest"),
        args=["--gtest_filter=ucx*/TestTransfer.*:ucx*/TestTransferTelemetry.*"],
        env=env,
        timeout=timeout,
    )


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--tests-dir",
        required=True,
        help="Directory produced by contrib/Dockerfile.tests (contains bin/ and lib/).",
    )
    p.add_argument(
        "--json-out",
        metavar="FILE",
        help="Write JSON results here (updated after each check).",
    )
    p.add_argument(
        "--timeout",
        type=int,
        default=120,
        help="Per-binary timeout in seconds (default: 120).",
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    tests_dir = os.path.abspath(args.tests_dir)

    reporter = Reporter(
        script=os.path.basename(__file__),
        json_out=args.json_out,
    )

    checks: list[tuple[str, Callable[[], str]]] = [
        ("ucx_backend_test", lambda: check_ucx_backend_test(tests_dir, args.timeout)),
        ("ucx_backend_multi", lambda: check_ucx_backend_multi(tests_dir, args.timeout)),
        ("ucx_worker_test", lambda: check_ucx_worker_test(tests_dir, args.timeout)),
        ("gtest_ucx_transfer", lambda: check_gtest_ucx(tests_dir, args.timeout)),
    ]

    for name, fn in checks:
        reporter.run(name, fn)

    return 1 if reporter.failed else 0


if __name__ == "__main__":
    sys.exit(main())
