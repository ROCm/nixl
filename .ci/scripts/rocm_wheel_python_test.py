#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run NIXL's Python tests and examples against an installed rocm-nixl wheel.

The Python part of upstream's .gitlab/test_python.sh, without its etcd cases.
The tests and examples come from the source tree the wheel was built from.
Two interpreters are used: one whose environment has the wheel alone, for
the tests that need nothing else, and one that also has AMD's PyTorch, as the
wheel's rocm<X>.<Y> extra installs it. Results are written in the format of
rocm_wheel_unit_test.py, which has to be beside this script.

    rocm_wheel_python_test.py --source-dir DIR --python PY --torch-python PY
                              [--json-out FILE] [--timeout S]
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import subprocess
import sys
import tempfile
import time

from rocm_wheel_unit_test import CheckFailed, Reporter, run_binary

# Passes only where nixl is not installed, which is not this environment.
DESELECTED = ["test/python/test_nixlbench_wheel.py::test_launcher_without_nixl"]

TORCH_GPU = """
import torch
assert torch.cuda.is_available(), "torch sees no GPU"
tensor = torch.zeros(1 << 20, dtype=torch.uint8, device="cuda:0")
tensor[:] = 7
assert int(tensor.cpu()[12345]) == 7, "data read back from the GPU is wrong"
print(f"torch {torch.__version__} on {torch.cuda.get_device_name(0)}")
"""


def pytest_summary(output: str) -> str:
    lines = [line for line in output.splitlines() if re.search(r"\d+ passed", line)]
    return lines[-1].strip("= ") if lines else "no summary line"


def check_pytest(python: str, source_dir: str, targets: list[str], timeout: int) -> str:
    command = [python, "-m", "pytest", "-q", "-p", "no:cacheprovider"]
    command += ["--timeout", str(timeout), *targets]
    for test in DESELECTED:
        command += ["--deselect", test]
    try:
        done = subprocess.run(
            command, cwd=source_dir, capture_output=True, text=True, timeout=4 * timeout
        )
    except subprocess.TimeoutExpired:
        raise CheckFailed(f"timed out after {4 * timeout}s")
    if done.returncode != 0:
        failed = [
            line for line in done.stdout.splitlines() if line.startswith("FAILED")
        ]
        raise CheckFailed(
            f"exit {done.returncode}: {pytest_summary(done.stdout)}; "
            + " | ".join(failed[:5])
        )
    return pytest_summary(done.stdout)


def check_example(python: str, examples: str, command: list[str], timeout: int) -> str:
    # partial_md_example.py removes the variable without checking it is set.
    env = {"NIXL_ETCD_ENDPOINTS": "http://127.0.0.1:1"}
    return run_binary(
        python, [os.path.join(examples, command[0]), *command[1:]], env, timeout
    )


def check_two_peers(python: str, examples: str, script: str, timeout: int) -> str:
    """A target on a port of its own choosing, then an initiator against it."""
    path = os.path.join(examples, script)
    with tempfile.TemporaryFile(mode="w+") as log:
        target = subprocess.Popen(
            [python, path, "--mode=target", "--ip=127.0.0.1", "--port=0"],
            env={**os.environ, "NIXL_LOG_LEVEL": "INFO"},
            stdout=subprocess.DEVNULL,
            stderr=log,
        )
        try:
            port = None
            deadline = time.monotonic() + 60
            while port is None and time.monotonic() < deadline:
                if target.poll() is not None:
                    raise CheckFailed(f"target exited {target.returncode}")
                log.seek(0)
                found = re.search(r"MD listener is listening on port (\d+)", log.read())
                port = found.group(1) if found else None
                time.sleep(0.5)
            if port is None:
                raise CheckFailed("target did not report its port within 60s")
            detail = run_binary(
                python,
                [path, "--mode=initiator", "--ip=127.0.0.1", f"--port={port}"],
                timeout=timeout,
            )
            try:
                code = target.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                raise CheckFailed(f"target still running after {timeout}s")
            if code != 0:
                raise CheckFailed(f"target exit {code}")
            return detail or f"both peers exit 0 (port {port})"
        finally:
            if target.poll() is None:
                target.kill()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--source-dir", required=True, help="The wheel's source tree.")
    p.add_argument("--python", required=True, help="Interpreter with the wheel alone.")
    p.add_argument(
        "--torch-python", required=True, help="Interpreter with the wheel and PyTorch."
    )
    p.add_argument("--json-out", metavar="FILE", help="Write JSON results here.")
    p.add_argument(
        "--timeout", type=int, default=300, help="Per-test timeout in seconds."
    )
    return p.parse_args()


def main() -> int:
    args = parse_args()
    source = os.path.abspath(args.source_dir)
    examples = os.path.join(source, "examples", "python")
    # Absolute, since pytest runs from the source tree; not resolved, since an
    # environment's interpreter is a link that has to be run by that name.
    python = os.path.abspath(args.python)
    torch_python = os.path.abspath(args.torch_python)
    reporter = Reporter(script=os.path.basename(__file__), json_out=args.json_out)

    reporter.run(
        "pytest_without_torch",
        functools.partial(
            check_pytest,
            python,
            source,
            ["test/python/test_nixl_bindings.py"],
            args.timeout,
        ),
    )
    reporter.run(
        "torch_gpu",
        functools.partial(
            run_binary, torch_python, ["-c", TORCH_GPU], None, args.timeout
        ),
    )
    reporter.run(
        "pytest",
        functools.partial(
            check_pytest, torch_python, source, ["test/python"], args.timeout
        ),
    )
    for name, command in [
        (
            "partial_md_example",
            ["partial_md_example.py", "--init-port", "18611", "--target-port", "18612"],
        ),
        ("query_mem_example", ["query_mem_example.py"]),
    ]:
        reporter.run(
            name,
            functools.partial(
                check_example, torch_python, examples, command, args.timeout
            ),
        )
    for script in ["basic_two_peers.py", "expanded_two_peers.py"]:
        reporter.run(
            script.removesuffix(".py"),
            functools.partial(
                check_two_peers, torch_python, examples, script, args.timeout
            ),
        )
    return 1 if reporter.failed else 0


if __name__ == "__main__":
    sys.exit(main())
