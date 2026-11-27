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

import hashlib
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
    docker.write_text('#!/bin/sh\necho "DOCKER $*"\nexit "${DOCKER_RC:-0}"\n')
    docker.chmod(docker.stat().st_mode | stat.S_IXUSR)

    def run(*args, env=None):
        full_env = dict(os.environ)
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
    # A caller that also drives scripts that had the option tells them apart by
    # searching for its name: it must not come back, even in a message.
    assert "--gpu-stack" not in (CONTRIB_DIR / "build-container.sh").read_text()


def test_explicit_wheel_base_is_kept_for_rocm(run_script):
    result = run_script(
        *MANYLINUX, "--rocm-version", "10.1.0", "--wheel-base", "manylinux_2_34"
    )
    assert result.returncode == 0, result.stderr
    assert "WHL_PLATFORM=manylinux_2_34_x86_64" in _docker_build_args(result)


@pytest.mark.parametrize(
    "args, message",
    [
        (["--rocm-version"], "--rocm-version requires an argument"),
        (["--rocm-version", "10.1"], "invalid --rocm-version '10.1'"),
        (["--rocm-version", "10.1.0", "--arch", "aarch64"], "only supported on x86_64"),
        (
            ["--rocm-version", "10.1.0", "--torch-versions", "2.11.0"],
            "--torch-versions",
        ),
        (["--rocm-version", "10.1.0", "--build-nixl-ep"], "--build-nixl-ep"),
        (
            ["--rocm-version", "10.1.0", "--proxy-ca-cert", "/nonexistent/ca.pem"],
            "--proxy-ca-cert file not found",
        ),
        (["--proxy-ca-cert"], "--proxy-ca-cert requires an argument"),
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


ROCM = [*MANYLINUX, "--rocm-version", "10.1.0"]


@pytest.fixture
def proxy_ca(tmp_path_factory):
    path = tmp_path_factory.mktemp("proxy-ca") / "ca.pem"
    path.write_text("not a real certificate\n")
    return path


def test_proxy_ca_cert_is_passed_as_a_secret(run_script, proxy_ca):
    result = run_script(*ROCM, "--proxy-ca-cert", str(proxy_ca))
    assert result.returncode == 0, result.stderr
    args = _docker_build_args(result)
    assert f"id=proxy_ca,src={proxy_ca.resolve()}" in args
    digest = hashlib.sha256(proxy_ca.read_bytes()).hexdigest()
    assert f"PROXY_CA_SHA256={digest}" in args


def test_no_proxy_ca_cert_leaves_the_build_unchanged(run_script):
    args = _docker_build_args(run_script(*ROCM))
    assert not [a for a in args if a.startswith(("id=proxy_ca", "PROXY_CA_SHA256="))]


def test_proxy_ca_cert_needs_a_dockerfile_that_consumes_it(run_script, proxy_ca):
    result = run_script(
        "--dockerfile", "contrib/Dockerfile", "--proxy-ca-cert", str(proxy_ca)
    )
    assert result.returncode != 0
    assert "requires a dockerfile that consumes the proxy_ca secret" in result.stderr
    assert "DOCKER build" not in result.stdout


def test_wheel_output_dir_needs_a_dockerfile_with_a_wheel_dist_stage(run_script):
    result = run_script(
        "--dockerfile", "contrib/Dockerfile", "--wheel-output-dir", "/tmp/wheels"
    )
    assert result.returncode != 0
    assert "requires a dockerfile with a wheel_dist stage" in result.stderr
    assert "DOCKER build" not in result.stdout


def test_nixlbench_wheel_output_dir(run_script, tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("nixlbench-wheels")
    result = run_script(*ROCM, "--nixlbench-wheel-output-dir", str(output_dir))
    assert result.returncode == 0, result.stderr
    args = _docker_build_args(result)
    assert args[args.index("--target") + 1] == "nixlbench_wheel_dist"
    assert args[args.index("--output") + 1] == f"type=local,dest={output_dir}"
    assert args.count("--target") == 1
    assert not [a for a in args if a.startswith("nixlbench_base=")]
    assert f"nixlbench wheel: {output_dir}" in result.stdout


@pytest.mark.parametrize("output_dir", ["dist-nixlbench", ".", "contrib/../out"])
def test_nixlbench_wheel_output_dir_must_be_outside_the_context(
    run_script, tmp_path, output_dir
):
    result = run_script(*ROCM, "--nixlbench-wheel-output-dir", output_dir)
    assert result.returncode != 0
    assert (
        "--nixlbench-wheel-output-dir must be outside the build context "
        f"({tmp_path.resolve()})" in result.stderr
    )
    assert "DOCKER build" not in result.stdout


def test_nixlbench_base_image(run_script, tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("nixlbench-wheels")
    result = run_script(
        *ROCM,
        "--nixlbench-wheel-output-dir",
        str(output_dir),
        "--nixlbench-base-image",
        "nixl:wheel-rocm10.1.0",
    )
    assert result.returncode == 0, result.stderr
    args = _docker_build_args(result)
    assert "nixlbench_base=nixl:wheel-rocm10.1.0" in args
    assert args[args.index("--target") + 1] == "nixlbench_wheel_dist"


@pytest.mark.parametrize(
    "args, message",
    [
        (
            [*MANYLINUX, "--nixlbench-wheel-output-dir", "/tmp/out"],
            "--nixlbench-wheel-output-dir requires --rocm-version",
        ),
        (
            [
                *ROCM,
                "--nixlbench-wheel-output-dir",
                "/tmp/out",
                "--wheel-output-dir",
                "/tmp/wheels",
            ],
            "cannot be used with --wheel-output-dir",
        ),
        (
            [*ROCM, "--nixlbench-base-image", "nixl:wheel"],
            "--nixlbench-base-image requires --nixlbench-wheel-output-dir",
        ),
        (
            [
                "--dockerfile",
                "contrib/Dockerfile",
                "--nixlbench-wheel-output-dir",
                "/tmp/out",
            ],
            "requires a dockerfile with a nixlbench_wheel_dist stage",
        ),
        (["--nixlbench-wheel-output-dir"], "requires an argument"),
        (
            [*ROCM, "--nixlbench-nixl-wheel-dir", "/tmp"],
            "--nixlbench-nixl-wheel-dir requires --nixlbench-wheel-output-dir",
        ),
        (
            [
                *ROCM,
                "--nixlbench-wheel-output-dir",
                "/tmp/out",
                "--nixlbench-nixl-wheel-dir",
                "/nonexistent/nixl-wheels",
            ],
            "--nixlbench-nixl-wheel-dir: /nonexistent/nixl-wheels is not a directory",
        ),
    ],
)
def test_rejected_nixlbench_combinations(run_script, args, message):
    result = run_script(*args)
    assert result.returncode != 0
    assert message in result.stderr
    assert "DOCKER build" not in result.stdout


@pytest.fixture
def exported(tmp_path_factory):
    """A NIXL wheel directory and the nixlbench output directory built from it."""
    nixl_dir = tmp_path_factory.mktemp("nixl-wheels")
    output_dir = tmp_path_factory.mktemp("nixlbench-wheels")
    wheel = nixl_dir / "nixl_rocm10_1-1.6.0-cp312-cp312-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(b"the NIXL build nixlbench was linked against")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    (output_dir / "nixl-wheels.sha256").write_text(f"{digest}  {wheel.name}\n")
    return nixl_dir, output_dir, wheel


def _verify_args(nixl_dir, output_dir):
    return [
        *ROCM,
        "--nixlbench-wheel-output-dir",
        str(output_dir),
        "--nixlbench-nixl-wheel-dir",
        str(nixl_dir),
    ]


def test_nixlbench_nixl_wheel_dir_accepts_the_wheels_built_against(
    run_script, exported
):
    nixl_dir, output_dir, _ = exported
    result = run_script(*_verify_args(nixl_dir, output_dir))
    assert result.returncode == 0, result.stderr
    assert f"was built against the NIXL wheels in {nixl_dir}" in result.stdout
    assert str(nixl_dir) not in " ".join(_docker_build_args(result))


@pytest.mark.parametrize("change", ["rebuilt", "missing"])
def test_nixlbench_nixl_wheel_dir_refuses_other_wheels(run_script, exported, change):
    nixl_dir, output_dir, wheel = exported
    if change == "rebuilt":
        wheel.write_bytes(b"another NIXL build")
    else:
        wheel.unlink()
    result = run_script(*_verify_args(nixl_dir, output_dir))
    assert result.returncode != 0
    assert "DOCKER build" in result.stdout
    assert (
        f"the nixlbench wheel in {output_dir} was not built against the NIXL "
        f"wheels in {nixl_dir}" in result.stderr
    )


def test_nixlbench_nixl_wheel_dir_is_not_checked_after_a_failed_build(
    run_script, exported
):
    nixl_dir, output_dir, wheel = exported
    wheel.write_bytes(b"another NIXL build")
    result = run_script(*_verify_args(nixl_dir, output_dir), env={"DOCKER_RC": "7"})
    assert result.returncode == 7
    assert "was not built against" not in result.stderr


def test_a_failed_build_fails_the_script(run_script):
    result = run_script(*ROCM, env={"DOCKER_RC": "7"})
    assert result.returncode == 7


def test_nixlbench_nixl_wheel_dir_in_the_context(run_script, tmp_path, exported):
    _, output_dir, wheel = exported
    in_context = tmp_path / "dist"
    in_context.mkdir()
    (in_context / wheel.name).write_bytes(wheel.read_bytes())
    args = _verify_args("dist", output_dir)
    result = run_script(*args)
    assert result.returncode != 0
    assert (
        "--nixlbench-nixl-wheel-dir is inside the build context "
        f"({tmp_path.resolve()})" in result.stderr
    )
    assert "DOCKER build" not in result.stdout

    # A pre-built wheel image does not depend on the context.
    result = run_script(*args, "--nixlbench-base-image", "nixl:wheel-rocm10.1.0")
    assert result.returncode == 0, result.stderr


def test_wheel_output_dir_does_not_build_nixlbench(run_script, tmp_path_factory):
    output_dir = tmp_path_factory.mktemp("wheels")
    result = run_script(*ROCM, "--wheel-output-dir", str(output_dir))
    assert result.returncode == 0, result.stderr
    args = _docker_build_args(result)
    assert args[args.index("--target") + 1] == "wheel_dist"
    assert "nixlbench" not in " ".join(args)
