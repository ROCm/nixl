# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""GPU-stack option handling of contrib/build-container.sh.

Runs the script from a copy of contrib/ with a stand-in for docker on PATH, so
no image is built and neither docker nor network access is needed.
"""

import os
import pathlib
import shutil
import stat
import subprocess

import pytest

REPO_DIR = pathlib.Path(__file__).resolve().parents[2]
CONTRIB_DIR = REPO_DIR / "contrib"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or not (CONTRIB_DIR / "build-container.sh").is_file(),
    reason="needs bash and the contrib/ directory of the source tree",
)

MANYLINUX = [
    "--dockerfile",
    "contrib/Dockerfile.manylinux",
    "--python-versions",
    "3.12",
]


@pytest.fixture
def run_script(tmp_path):
    # A copy keeps the script's "delete the build directory" check, which looks
    # beside contrib/, away from a build directory in the source tree.
    shutil.copytree(CONTRIB_DIR, tmp_path / "contrib")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    docker = bin_dir / "docker"
    docker.write_text('#!/bin/sh\necho "DOCKER $*"\n')
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    def run(*args, env=None):
        full_env = {
            k: v
            for k, v in os.environ.items()
            if k not in ("CUDA_VERSION", "TORCH_CUDA_INDEX")
        }
        full_env["PATH"] = f"{bin_dir}{os.pathsep}{full_env['PATH']}"
        full_env.update(env or {})
        return subprocess.run(
            ["bash", "contrib/build-container.sh", *args],
            cwd=tmp_path,
            env=full_env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    return run


def _docker_build_args(result):
    lines = [
        line for line in result.stdout.splitlines() if line.startswith("DOCKER build")
    ]
    assert len(lines) == 1, result.stdout + result.stderr
    return lines[0].split()


def test_default_is_cuda(run_script):
    result = run_script(*MANYLINUX)
    assert result.returncode == 0, result.stderr
    args = _docker_build_args(result)
    assert not [a for a in args if a.startswith("GPU_STACK=")]
    assert not [a for a in args if a.startswith("ROCM_VERSION=")]


@pytest.mark.parametrize("version", ["10.2.0", "10.1.0", "7.14.1"])
def test_rocm_version_selects_rocm(run_script, version):
    result = run_script(*MANYLINUX, "--rocm-version", version)
    assert result.returncode == 0, result.stderr
    args = _docker_build_args(result)
    assert "GPU_STACK=rocm" in args
    assert f"ROCM_VERSION={version}" in args
    assert "WHL_PLATFORM=manylinux_2_28_x86_64" in args


def test_gpu_stack_is_not_an_option(run_script):
    result = run_script(*MANYLINUX, "--gpu-stack", "rocm")
    assert result.returncode != 0
    assert "DOCKER build" not in result.stdout


@pytest.mark.parametrize(
    "args, message",
    [
        (["--rocm-version", "10.1"], "invalid --rocm-version '10.1'"),
        (["--rocm-version", "10.1.0", "--arch", "aarch64"], "only supported on x86_64"),
        (["--rocm-version", "10.1.0", "--cuda-version", "12.9"], "--cuda-version"),
        (
            ["--rocm-version", "10.1.0", "--torch-cuda-index", "cu129"],
            "--torch-cuda-index",
        ),
        (
            ["--rocm-version", "10.1.0", "--torch-versions", "2.11.0"],
            "--torch-versions",
        ),
        (["--rocm-version", "10.1.0", "--build-nixl-ep"], "--build-nixl-ep"),
        (
            ["--rocm-version", "10.1.0", "--proxy-ca-cert", "/nonexistent/ca.pem"],
            "--proxy-ca-cert file not found",
        ),
    ],
)
def test_rejected_combinations(run_script, args, message):
    result = run_script(*MANYLINUX, *args)
    assert result.returncode != 0
    assert message in result.stderr
    assert "DOCKER build" not in result.stdout


def test_rocm_needs_a_dockerfile_that_supports_it(run_script):
    result = run_script(
        "--dockerfile", "contrib/Dockerfile", "--rocm-version", "10.1.0"
    )
    assert result.returncode != 0
    assert "requires a dockerfile that supports ROCm" in result.stderr


def test_cuda_version_in_the_environment_is_rejected_for_rocm(run_script):
    result = run_script(
        *MANYLINUX, "--rocm-version", "10.1.0", env={"CUDA_VERSION": "12.9"}
    )
    assert result.returncode != 0
    assert "--cuda-version" in result.stderr
