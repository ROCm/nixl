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

"""The nixlbench wheel: contrib/wheel_nixlbench.py, its launcher and dispatcher.

Uses fabricated NIXL wheels and a shell script in place of the nixlbench
binary, so it needs neither a built NIXL nor a GPU nor network access.
"""

import base64
import csv
import hashlib
import importlib.util
import io
import os
import pathlib
import platform
import shutil
import signal
import stat
import subprocess
import sys
import zipfile

import pytest

REPO_DIR = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO_DIR / "contrib" / "wheel_nixlbench.py"
WHEEL_DIR = REPO_DIR / "benchmark" / "nixlbench" / "wheel"
LAUNCHER = WHEEL_DIR / "launcher.py"
DISPATCHER = WHEEL_DIR / "dispatcher.py"

pytestmark = pytest.mark.skipif(
    platform.system() != "Linux", reason="the nixlbench wheel is Linux-only"
)

VERSION = "1.6.0"
PLATFORM = "manylinux_2_28_x86_64"
LICENSE_EXPRESSION = "Apache-2.0 AND BSL-1.0 AND MIT AND BSD-3-Clause"

# Stands in for nixlbench: reports how the launcher started it.
FAKE_BINARY = """#!/bin/sh
echo "BINARY:$0"
for arg in "$@"; do echo "ARG:$arg"; done
echo "LD_LIBRARY_PATH:${LD_LIBRARY_PATH-<unset>}"
echo "NIXL_PLUGIN_DIR:${NIXL_PLUGIN_DIR-<unset>}"
grep '^SigIgn' /proc/self/status
exit "${FAKE_EXIT:-0}"
"""


def _load_module():
    spec = importlib.util.spec_from_file_location("wheel_nixlbench", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wheel_nixlbench = _load_module()


def _sha256_b64(data):
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _write_zip(path, entries):
    """Write `entries` (path -> (bytes, mode)) as a zip file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as whl:
        for entry, (data, mode) in entries.items():
            info = zipfile.ZipInfo(entry, date_time=(2026, 1, 2, 3, 4, 6))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = mode << 16
            whl.writestr(info, data)
    return path


def _read_zip(path):
    with zipfile.ZipFile(path) as whl:
        return {
            info.filename: (whl.read(info), (info.external_attr >> 16) & 0o7777)
            for info in whl.infolist()
        }


def _make_nixl_wheel(
    directory,
    name="nixl-rocm10.1",
    version=VERSION,
    python="cp312",
    libnixl=True,
    requires_python=">=3.10",
):
    """Write a NIXL backend wheel as build-wheel.sh produces it."""
    package = name.replace("-", "_").replace(".", "_")
    dist_info = f"{package}-{version}.dist-info"
    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {name}",
        f"Version: {version}",
        "License-Expression: MIT AND Apache-2.0",
        "License-File: LICENSE",
        "License-File: licenses/NVIDIA-proprietary-LICENSE.txt",
        f"Requires-Python: {requires_python}",
        "Requires-Dist: numpy",
    ]
    entries = {
        f"{package}/__init__.py": (b"", 0o644),
        f".{package}.mesonpy.libs/plugins/libplugin_POSIX.so": (b"posix", 0o755),
        f"{dist_info}/METADATA": (
            "\n".join(metadata).encode() + b"\n\nreadme\n",
            0o644,
        ),
        f"{dist_info}/licenses/licenses/NVIDIA-proprietary-LICENSE.txt": (
            b"NVIDIA license\n",
            0o644,
        ),
    }
    if libnixl:
        entries[f".{package}.mesonpy.libs/libnixl.so"] = (b"libnixl", 0o755)
    return _write_zip(
        directory / f"{package}-{version}-{python}-{python}-{PLATFORM}.whl", entries
    )


def _build(tmp_path, nixl_wheel=None, platform_tag=PLATFORM, **nixl_kwargs):
    """Build a nixlbench backend wheel around the fake binary."""
    work = tmp_path / "build" / (nixl_kwargs.get("name") or "default")
    work.mkdir(parents=True, exist_ok=True)
    binary = work / "nixlbench"
    binary.write_text(FAKE_BINARY)
    license_file = work / "LICENSE"
    license_file.write_text("Apache license text\n")
    gflags = work / "COPYING.txt"
    gflags.write_text("gflags license text\n")
    if nixl_wheel is None:
        nixl_wheel = _make_nixl_wheel(work / "nixl", **nixl_kwargs)
    return pathlib.Path(
        wheel_nixlbench.build(
            str(binary),
            str(nixl_wheel),
            platform_tag,
            str(work / "out"),
            str(LAUNCHER),
            LICENSE_EXPRESSION,
            [("LICENSE", str(license_file)), ("gflags/COPYING.txt", str(gflags))],
        )
    )


def _modified(wheel, directory, add=None, remove=(), modes=None, metadata=None):
    """A copy of `wheel` with entries added, removed, re-moded or new METADATA."""
    entries = _read_zip(wheel)
    for name in remove:
        del entries[name]
    for name, data in (add or {}).items():
        entries[name] = (data, 0o644)
    for name, mode in (modes or {}).items():
        entries[name] = (entries[name][0], mode)
    if metadata is not None:
        (name,) = [n for n in entries if n.endswith(".dist-info/METADATA")]
        entries[name] = (metadata(entries[name][0].decode()).encode(), 0o644)
    return _write_zip(directory / wheel.name, entries)


def _headers(wheel):
    (name,) = [n for n in _read_zip(wheel) if n.endswith(".dist-info/METADATA")]
    return _read_zip(wheel)[name][0].decode().split("\n\n")[0].splitlines()


def _install(wheel, site):
    """Unpack a wheel into `site` as an installer would, with its file modes."""
    for name, (data, mode) in _read_zip(wheel).items():
        target = site / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(mode)


# --- build -------------------------------------------------------------------


@pytest.mark.parametrize(
    "nixl_name, dist_name, package",
    [
        ("nixl-rocm10.1", "nixlbench-rocm10.1", "nixlbench_rocm10_1"),
        ("nixl-cu13", "nixlbench-cu13", "nixlbench_cu13"),
    ],
)
def test_build_names(tmp_path, nixl_name, dist_name, package):
    wheel = _build(tmp_path, name=nixl_name)
    assert wheel.name == f"{package}-{VERSION}-py3-none-{PLATFORM}.whl"
    dist_info = f"{package}-{VERSION}.dist-info"
    entries = _read_zip(wheel)
    assert set(entries) == {
        f"{package}/__init__.py",
        f"{package}/bin/nixlbench",
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/RECORD",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/top_level.txt",
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/licenses/gflags/COPYING.txt",
    }
    assert list(entries)[-1] == f"{dist_info}/RECORD"
    assert entries[f"{package}/__init__.py"][0] == LAUNCHER.read_bytes()
    assert entries[f"{package}/bin/nixlbench"] == (FAKE_BINARY.encode(), 0o755)
    assert entries[f"{dist_info}/entry_points.txt"][0].decode() == (
        f"[console_scripts]\nnixlbench = {package}:main\n"
    )
    assert entries[f"{dist_info}/top_level.txt"][0].decode() == f"{package}\n"

    headers = _headers(wheel)
    assert headers[0] == "Metadata-Version: 2.4"
    assert f"Name: {dist_name}" in headers
    assert f"Version: {VERSION}" in headers
    assert "Requires-Python: >=3.10" in headers
    assert f"License-Expression: {LICENSE_EXPRESSION}" in headers
    assert [h for h in headers if h.startswith("License-File:")] == [
        "License-File: LICENSE",
        "License-File: gflags/COPYING.txt",
    ]
    assert [h for h in headers if h.startswith("Requires-Dist:")] == [
        f"Requires-Dist: {nixl_name}=={VERSION}"
    ]


def test_build_wheel_file_and_record(tmp_path):
    wheel = _build(tmp_path, platform_tag="linux_x86_64")
    dist_info = f"nixlbench_rocm10_1-{VERSION}.dist-info"
    entries = _read_zip(wheel)
    lines = entries[f"{dist_info}/WHEEL"][0].decode().splitlines()
    assert "Root-Is-Purelib: false" in lines
    assert [line for line in lines if line.startswith("Tag:")] == [
        "Tag: py3-none-linux_x86_64"
    ]

    record_path = f"{dist_info}/RECORD"
    rows = list(csv.reader(io.StringIO(entries[record_path][0].decode())))
    assert sorted(row[0] for row in rows) == sorted(entries)
    for name, digest, size in rows:
        if name == record_path:
            assert (digest, size) == ("", "")
            continue
        assert digest == f"sha256={_sha256_b64(entries[name][0])}"
        assert size == str(len(entries[name][0]))


def test_build_inherits_a_stamped_version(tmp_path):
    wheel = _build(tmp_path, version="1.6.0.dev20261119")
    assert wheel.name.startswith("nixlbench_rocm10_1-1.6.0.dev20261119-py3-none-")
    assert "Requires-Dist: nixl-rocm10.1==1.6.0.dev20261119" in _headers(wheel)


def test_build_refuses_the_meta_wheel(tmp_path):
    meta = _write_zip(
        tmp_path / f"nixl-{VERSION}-py3-none-any.whl",
        {
            f"nixl-{VERSION}.dist-info/METADATA": (
                f"Metadata-Version: 2.4\nName: nixl\nVersion: {VERSION}\n".encode(),
                0o644,
            )
        },
    )
    with pytest.raises(wheel_nixlbench.WheelError, match="not a NIXL backend wheel"):
        _build(tmp_path, nixl_wheel=meta)


def test_build_refuses_a_nixl_wheel_without_libnixl(tmp_path):
    with pytest.raises(wheel_nixlbench.WheelError, match=r"libnixl\.so is missing"):
        _build(tmp_path, libnixl=False)


def test_build_refuses_a_missing_license_file(tmp_path):
    nixl_wheel = _make_nixl_wheel(tmp_path / "nixl")
    binary = tmp_path / "nixlbench"
    binary.write_text(FAKE_BINARY)
    with pytest.raises(wheel_nixlbench.WheelError, match="cannot read license file"):
        wheel_nixlbench.build(
            str(binary),
            str(nixl_wheel),
            PLATFORM,
            str(tmp_path / "out"),
            str(LAUNCHER),
            LICENSE_EXPRESSION,
            [("LICENSE", str(tmp_path / "absent"))],
        )
    assert not (tmp_path / "out").exists()


# --- check -------------------------------------------------------------------


@pytest.fixture
def repaired(tmp_path):
    """A backend wheel with the libraries auditwheel vendors."""
    return _modified(
        _build(tmp_path),
        tmp_path / "repaired",
        add={
            "nixlbench_rocm10_1.libs/libgomp-a34b3233.so.1.0.0": b"gomp",
            "nixlbench_rocm10_1.libs/libgflags-71a4e4bd.so.2.1.2": b"gflags",
            f"nixlbench_rocm10_1-{VERSION}.dist-info/sboms/auditwheel.cdx.json": b"{}",
        },
    )


def test_check_passes(repaired):
    assert wheel_nixlbench.check(str(repaired), ["libgomp", "libgflags"]) == []


def test_check_command(repaired):
    command = [sys.executable, str(SCRIPT), "check", "--allow-vendored", "libgomp"]
    result = subprocess.run(
        [*command, "--allow-vendored", "libgflags", str(repaired)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    result = subprocess.run([*command, str(repaired)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "libgflags-71a4e4bd.so.2.1.2 is vendored but not allowed" in result.stderr


def test_check_rejects_a_foreign_vendored_library(repaired):
    (error,) = wheel_nixlbench.check(str(repaired), ["libgomp"])
    assert "libgflags-71a4e4bd.so.2.1.2 is vendored but not allowed" in error


def test_check_rejects_a_missing_license_file_and_expression(repaired, tmp_path):
    wheel = _modified(
        repaired,
        tmp_path / "bad",
        remove=[f"nixlbench_rocm10_1-{VERSION}.dist-info/licenses/LICENSE"],
        metadata=lambda text: text.replace(
            f"License-Expression: {LICENSE_EXPRESSION}\n", ""
        ),
    )
    errors = wheel_nixlbench.check(str(wheel), ["libgomp", "libgflags"])
    assert any("license file LICENSE is missing" in e for e in errors)
    assert any("no License-Expression" in e for e in errors)
    assert len(errors) == 2


def test_check_rejects_a_binary_that_is_not_executable(repaired, tmp_path):
    wheel = _modified(
        repaired, tmp_path / "bad", modes={"nixlbench_rocm10_1/bin/nixlbench": 0o644}
    )
    (error,) = wheel_nixlbench.check(str(wheel), ["libgomp", "libgflags"])
    assert "has mode 644, expected 755" in error


def test_check_rejects_a_second_binary_and_stray_files(repaired, tmp_path):
    wheel = _modified(
        repaired,
        tmp_path / "bad",
        add={"nixlbench_rocm10_1/bin/other": b"x", "nixl/__init__.py": b""},
    )
    errors = wheel_nixlbench.check(str(wheel), ["libgomp", "libgflags"])
    assert any("expected exactly nixlbench_rocm10_1/bin/nixlbench" in e for e in errors)
    assert any("unexpected file nixl/__init__.py" in e for e in errors)


@pytest.mark.parametrize(
    "requirement",
    ["rocm-nixl==1.6.0", "nixl-rocm10.1>=1.6.0", "nixl-rocm10.1==1.6.1", None],
)
def test_check_rejects_another_requirement(repaired, tmp_path, requirement):
    old = f"Requires-Dist: nixl-rocm10.1=={VERSION}\n"
    new = f"Requires-Dist: {requirement}\n" if requirement else ""
    wheel = _modified(
        repaired, tmp_path / "bad", metadata=lambda text: text.replace(old, new)
    )
    (error,) = wheel_nixlbench.check(str(wheel), ["libgomp", "libgflags"])
    assert "expected the single requirement nixl-rocm10.1==1.6.0" in error


def test_missing_symbols():
    needed = {"nixlAgent_ctor", "nixlAgent_dtor", "serdes_add"}
    assert wheel_nixlbench.missing_symbols(needed, needed | {"extra"}) == []
    assert wheel_nixlbench.missing_symbols(needed, {"serdes_add"}) == [
        "nixlAgent_ctor",
        "nixlAgent_dtor",
    ]
    assert wheel_nixlbench.missing_symbols(set(), set()) == []


# --- merge -------------------------------------------------------------------


def _backend(tmp_path, rocm, **kwargs):
    return _build(tmp_path / f"rocm{rocm}", name=f"nixl-rocm{rocm}", **kwargs)


def _merge(tmp_path, *backends, **kwargs):
    return pathlib.Path(
        wheel_nixlbench.merge(
            [str(b) for b in backends],
            str(tmp_path / "merged"),
            str(DISPATCHER),
            **kwargs,
        )
    )


def test_merge(tmp_path):
    backends = [_backend(tmp_path, rocm) for rocm in ("10.2", "7.14", "10.1")]
    backends[0] = _modified(
        backends[0],
        tmp_path / "with-sbom",
        add={
            "nixlbench_rocm10_2.libs/libgomp-a34b3233.so.1.0.0": b"gomp 10.2",
            f"nixlbench_rocm10_2-{VERSION}.dist-info/sboms/auditwheel.cdx.json": b"{}",
        },
    )
    merged = _merge(tmp_path, *backends)
    assert merged.name == f"rocm_nixlbench-{VERSION}-py3-none-{PLATFORM}.whl"

    dist_info = f"rocm_nixlbench-{VERSION}.dist-info"
    entries = _read_zip(merged)
    packages = ["nixlbench_rocm10_1", "nixlbench_rocm10_2", "nixlbench_rocm7_14"]
    expected = {
        "nixlbench/__init__.py",
        "nixlbench_rocm10_2.libs/libgomp-a34b3233.so.1.0.0",
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/RECORD",
        f"{dist_info}/entry_points.txt",
        f"{dist_info}/top_level.txt",
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/licenses/gflags/COPYING.txt",
    }
    for package in packages:
        expected |= {f"{package}/__init__.py", f"{package}/bin/nixlbench"}
    assert set(entries) == expected
    assert list(entries)[-1] == f"{dist_info}/RECORD"

    assert entries["nixlbench/__init__.py"][0] == DISPATCHER.read_bytes()
    for backend in backends:
        for name, entry in _read_zip(backend).items():
            if ".dist-info/" not in name:
                assert entries[name] == entry
    assert entries["nixlbench_rocm10_1/bin/nixlbench"][1] == 0o755
    assert entries[f"{dist_info}/entry_points.txt"][0].decode() == (
        "[console_scripts]\nnixlbench = nixlbench:main\n"
    )
    assert entries[f"{dist_info}/top_level.txt"][0].decode().split() == [
        "nixlbench",
        *packages,
    ]

    headers = _headers(merged)
    assert "Name: rocm-nixlbench" in headers
    assert f"Version: {VERSION}" in headers
    assert "Requires-Python: >=3.10" in headers
    assert f"License-Expression: {LICENSE_EXPRESSION}" in headers
    assert sum(h.startswith("Name:") for h in headers) == 1
    assert [h for h in headers if h.startswith("Requires-Dist:")] == [
        f"Requires-Dist: rocm-nixl=={VERSION}"
    ]

    record = list(csv.reader(io.StringIO(entries[f"{dist_info}/RECORD"][0].decode())))
    assert sorted(row[0] for row in record) == sorted(entries)
    for name, digest, _ in record:
        if digest:
            assert digest == f"sha256={_sha256_b64(entries[name][0])}"


def test_merge_allows_a_single_backend(tmp_path):
    merged = _merge(tmp_path, _backend(tmp_path, "10.1"))
    assert "nixlbench_rocm10_1/bin/nixlbench" in _read_zip(merged)
    assert f"Requires-Dist: rocm-nixl=={VERSION}" in _headers(merged)


def test_merge_name_and_requirement_can_be_set(tmp_path):
    merged = _merge(
        tmp_path, _backend(tmp_path, "10.1"), name="amd-nixlbench", requires_dist="x"
    )
    assert merged.name.startswith("amd_nixlbench-")
    assert "Name: amd-nixlbench" in _headers(merged)
    assert f"Requires-Dist: x=={VERSION}" in _headers(merged)


def test_merge_refusals(tmp_path):
    good = _backend(tmp_path, "10.1")
    other = _backend(tmp_path, "10.2")

    def refuses(match, *wheels):
        with pytest.raises(wheel_nixlbench.WheelError, match=match):
            _merge(tmp_path, *wheels)

    refuses(
        "disagree on version", good, _backend(tmp_path / "v", "10.2", version="1.7")
    )
    refuses(
        "disagree on platform tag",
        good,
        _backend(tmp_path / "p", "10.2", platform_tag="manylinux_2_39_x86_64"),
    )
    refuses(
        "disagree on Requires-Python",
        good,
        _backend(tmp_path / "r", "10.2", requires_python=">=3.11"),
    )
    refuses("duplicate backend", good, _modified(good, tmp_path / "copy"))
    refuses(
        "path collision: shared.py",
        _modified(good, tmp_path / "c1", add={"shared.py": b"x"}),
        _modified(other, tmp_path / "c2", add={"shared.py": b"x"}),
    )
    refuses(
        "path collision: nixlbench/__init__.py",
        _modified(good, tmp_path / "d", add={"nixlbench/__init__.py": b"x"}),
    )
    license_path = f"nixlbench_rocm10_2-{VERSION}.dist-info/licenses/LICENSE"
    refuses(
        "license file LICENSE differs",
        good,
        _modified(other, tmp_path / "l", add={license_path: b"other text\n"}),
    )
    refuses(
        "disagree on License-Expression",
        good,
        _modified(
            other, tmp_path / "e", metadata=lambda text: text.replace("BSL-1.0", "MIT")
        ),
    )
    refuses(
        "expected the single requirement nixl-rocm10.2==1.6.0",
        good,
        _modified(
            other,
            tmp_path / "q",
            metadata=lambda text: text.replace("nixl-rocm10.2==", "nixl-rocm10.1=="),
        ),
    )
    refuses(
        "not a nixlbench_rocm<X>_<Y> backend wheel", _build(tmp_path, name="nixl-cu13")
    )


# --- launcher ----------------------------------------------------------------


def _rocm(root, version="10.1.0"):
    """Fabricate a ROCm install prefix."""
    (root / ".info").mkdir(parents=True)
    (root / ".info" / "version").write_text(version + "\n")
    (root / "lib").mkdir()
    (root / "lib" / "libamdhip64.so.7").write_bytes(b"")
    return root


def _run(code, site, default_root, args=(), env=None):
    """Run launcher or dispatcher code in a fresh interpreter."""
    full_env = {
        k: v
        for k, v in os.environ.items()
        if k
        not in (
            "ROCM_PATH",
            "LD_LIBRARY_PATH",
            "NIXL_PLUGIN_DIR",
            "NIXLBENCH_WHEEL_DEBUG",
        )
    }
    full_env["PYTHONPATH"] = os.pathsep.join(str(s) for s in site)
    full_env["TEST_ROCM_ROOT"] = str(default_root)
    full_env.update(env or {})
    return subprocess.run(
        [sys.executable, "-c", code, *args],
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _launch(site, default_root, package="nixlbench_rocm10_1", **kwargs):
    code = (
        f"import os, {package} as backend\n"
        "backend._DEFAULT_ROCM_ROOT = os.environ['TEST_ROCM_ROOT']\n"
        "backend.main()\n"
    )
    return _run(
        code, site if isinstance(site, list) else [site], default_root, **kwargs
    )


def _value(result, key):
    (line,) = [ln for ln in result.stdout.splitlines() if ln.startswith(key + ":")]
    return line.split(":", 1)[1].strip()


@pytest.fixture
def site(tmp_path):
    """site-packages holding nixlbench-rocm10.1 and its nixl-rocm10.1."""
    directory = tmp_path / "site"
    _install(_build(tmp_path), directory)
    _install(_make_nixl_wheel(tmp_path / "nixl"), directory)
    return directory


@pytest.fixture
def no_rocm(tmp_path):
    directory = tmp_path / "no-rocm"
    directory.mkdir()
    return directory


def test_launcher_passes_arguments_and_exit_code(site, no_rocm):
    result = _launch(
        site, no_rocm, args=["--backend", "POSIX", "a b", ""], env={"FAKE_EXIT": "7"}
    )
    assert result.returncode == 7
    args = [ln[4:] for ln in result.stdout.splitlines() if ln.startswith("ARG:")]
    assert args == ["--backend", "POSIX", "a b", ""]
    assert _value(result, "BINARY") == str(site / "nixlbench_rocm10_1/bin/nixlbench")


def test_launcher_restores_ignored_signals(site, no_rocm):
    ignored = int(_value(_launch(site, no_rocm), "SigIgn"), 16)
    assert not ignored & (1 << (signal.SIGPIPE - 1))
    assert not ignored & (1 << (signal.SIGXFSZ - 1))


def test_launcher_uses_rocm_path(site, no_rocm, tmp_path):
    rocm = _rocm(tmp_path / "rocm")
    result = _launch(
        site, no_rocm, env={"ROCM_PATH": str(rocm), "LD_LIBRARY_PATH": "/existing"}
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert _value(result, "LD_LIBRARY_PATH") == f"{rocm}/lib:/existing"


def test_launcher_uses_the_default_root(site, tmp_path):
    rocm = _rocm(tmp_path / "rocm")
    result = _launch(site, rocm)
    assert result.stderr == ""
    assert _value(result, "LD_LIBRARY_PATH") == f"{rocm}/lib"


def test_launcher_finds_rocm_under_core(site, no_rocm, tmp_path):
    _rocm(tmp_path / "rocm" / "core")
    result = _launch(site, no_rocm, env={"ROCM_PATH": str(tmp_path / "rocm")})
    assert _value(result, "LD_LIBRARY_PATH") == f"{tmp_path}/rocm/core/lib"


def test_launcher_finds_a_single_versioned_core(site, tmp_path):
    root = tmp_path / "rocm"
    _rocm(root / "core-10.1")
    result = _launch(site, root)
    assert result.stderr == ""
    assert _value(result, "LD_LIBRARY_PATH") == f"{root}/core-10.1/lib"

    # As in an image that sets ROCM_PATH=/opt/rocm over the component packages.
    result = _launch(site, tmp_path / "elsewhere", env={"ROCM_PATH": str(root)})
    assert result.stderr == ""
    assert _value(result, "LD_LIBRARY_PATH") == f"{root}/core-10.1/lib"

    _rocm(root / "core-10.2", "10.2.0")
    result = _launch(site, root)
    assert f"no ROCm install found under {root} (set ROCM_PATH)" in result.stderr
    assert _value(result, "LD_LIBRARY_PATH") == "<unset>"

    result = _launch(site, tmp_path / "elsewhere", env={"ROCM_PATH": str(root)})
    assert f"no ROCm install found under {root}; relying on" in result.stderr
    assert _value(result, "LD_LIBRARY_PATH") == "<unset>"


def test_launcher_needs_a_version_and_the_hip_runtime(site, tmp_path):
    root = _rocm(tmp_path / "rocm")
    (root / "lib" / "libamdhip64.so.7").unlink()
    assert "no ROCm install found" in _launch(site, root).stderr
    (root / "lib" / "libamdhip64.so.7").write_bytes(b"")
    (root / ".info" / "version").write_text("unknown\n")
    assert "no ROCm install found" in _launch(site, root).stderr


def test_launcher_without_rocm_leaves_the_loader_path_alone(site, no_rocm):
    result = _launch(site, no_rocm)
    assert result.returncode == 0
    assert (
        f"nixlbench: no ROCm install found under {no_rocm} (set ROCM_PATH); "
        "relying on the dynamic loader" in result.stderr
    )
    assert _value(result, "LD_LIBRARY_PATH") == "<unset>"

    result = _launch(site, no_rocm, env={"LD_LIBRARY_PATH": "/a:/b"})
    assert _value(result, "LD_LIBRARY_PATH") == "/a:/b"


def test_launcher_warns_about_another_rocm_version(site, tmp_path):
    rocm = _rocm(tmp_path / "rocm", "10.2.0")
    result = _launch(site, rocm)
    assert result.returncode == 0
    assert f"found ROCm 10.2 at {rocm} but this nixlbench was built for ROCm 10.1" in (
        result.stderr
    )
    assert _value(result, "LD_LIBRARY_PATH") == f"{rocm}/lib"


def test_launcher_split_install_adds_the_nixl_libraries(tmp_path, no_rocm):
    bench_site, nixl_site = tmp_path / "bench-site", tmp_path / "nixl-site"
    _install(_build(tmp_path), bench_site)
    _install(_make_nixl_wheel(tmp_path / "nixl"), nixl_site)
    result = _launch([bench_site, nixl_site], no_rocm, env={"LD_LIBRARY_PATH": "/x"})
    assert result.returncode == 0
    assert _value(result, "LD_LIBRARY_PATH") == (
        f"{nixl_site}/.nixl_rocm10_1.mesonpy.libs:/x"
    )


def test_launcher_symlinked_binary_takes_the_fallback(site, tmp_path):
    # As with an installer that links files in from its cache: $ORIGIN in the
    # RPATH then names the cache.
    rocm = _rocm(tmp_path / "rocm")
    cache = tmp_path / "cache" / "nixlbench"
    cache.parent.mkdir()
    binary = site / "nixlbench_rocm10_1" / "bin" / "nixlbench"
    binary.rename(cache)
    binary.symlink_to(cache)
    result = _launch(site, rocm)
    assert result.returncode == 0
    assert _value(result, "LD_LIBRARY_PATH") == (
        f"{site}/.nixl_rocm10_1.mesonpy.libs:{rocm}/lib"
    )


def test_launcher_without_nixl(tmp_path, no_rocm):
    directory = tmp_path / "site"
    _install(_build(tmp_path), directory)
    result = _launch(directory, no_rocm)
    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        "nixlbench: nixl_rocm10_1 is not installed next to this nixlbench; "
        "install the matching NIXL wheel" in result.stderr
    )


@pytest.mark.parametrize("installed", ["1.6.1", "1.6.0.post1", "1.5.0"])
def test_launcher_refuses_another_nixl_version(tmp_path, no_rocm, installed):
    # What "pip install --no-deps" or upgrading the NIXL wheel alone leaves.
    directory = tmp_path / "site"
    _install(_build(tmp_path), directory)
    _install(_make_nixl_wheel(tmp_path / "other", version=installed), directory)
    result = _launch(directory, no_rocm)
    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        f"nixlbench: nixlbench-rocm10.1 {VERSION} requires nixl-rocm10.1=={VERSION}, "
        f"but nixl-rocm10.1 {installed} is installed" in result.stderr
    )


def test_launcher_runs_on_a_nixl_without_metadata(site, no_rocm):
    # A NIXL that was not installed from a wheel has no version to compare.
    shutil.rmtree(site / f"nixl_rocm10_1-{VERSION}.dist-info")
    assert _launch(site, no_rocm).returncode == 0


def test_launcher_warns_about_a_foreign_plugin_dir(site, no_rocm, tmp_path):
    wheel_plugins = site / ".nixl_rocm10_1.mesonpy.libs" / "plugins"
    result = _launch(site, no_rocm, env={"NIXL_PLUGIN_DIR": "/usr/local/nixl/plugins"})
    assert result.returncode == 0
    assert (
        "nixlbench: NIXL_PLUGIN_DIR is set: NIXL loads its plugins from "
        f"/usr/local/nixl/plugins, not from the NIXL wheel ({wheel_plugins})"
        in result.stderr
    )
    assert _value(result, "NIXL_PLUGIN_DIR") == "/usr/local/nixl/plugins"

    link = tmp_path / "plugins"
    link.symlink_to(wheel_plugins)
    result = _launch(site, no_rocm, env={"NIXL_PLUGIN_DIR": str(link)})
    assert "NIXL_PLUGIN_DIR" not in result.stderr
    assert _value(result, "NIXL_PLUGIN_DIR") == str(link)

    assert _value(_launch(site, no_rocm), "NIXL_PLUGIN_DIR") == "<unset>"


def test_launcher_debug(site, tmp_path):
    rocm = _rocm(tmp_path / "rocm")
    result = _launch(site, rocm, env={"NIXLBENCH_WHEEL_DEBUG": "1"})
    binary = site / "nixlbench_rocm10_1/bin/nixlbench"
    assert result.stderr.splitlines() == [
        "nixlbench: package: nixlbench_rocm10_1",
        f"nixlbench: binary: {binary}",
        f"nixlbench: binary realpath: {binary.resolve()}",
        "nixlbench: NIXL libraries: RPATH",
        f"nixlbench: NIXL plugins: {site}/.nixl_rocm10_1.mesonpy.libs/plugins",
        f"nixlbench: ROCm: version 10.1, root {rocm}, libraries {rocm}/lib",
        f"nixlbench: LD_LIBRARY_PATH: {rocm}/lib",
    ]
    result = _launch(
        site,
        tmp_path / "none",
        env={"NIXLBENCH_WHEEL_DEBUG": "1", "NIXL_PLUGIN_DIR": "/other/plugins"},
    )
    assert "nixlbench: ROCm: not detected" in result.stderr
    assert "nixlbench: NIXL plugins: /other/plugins" in result.stderr


def test_launcher_of_a_cuda_package_ignores_rocm(tmp_path):
    directory = tmp_path / "site"
    _install(_build(tmp_path, name="nixl-cu13"), directory)
    _install(_make_nixl_wheel(tmp_path / "nixl", name="nixl-cu13"), directory)
    rocm = _rocm(tmp_path / "rocm")
    result = _launch(
        directory, rocm, package="nixlbench_cu13", env={"ROCM_PATH": str(rocm)}
    )
    assert result.returncode == 0
    assert result.stderr == ""
    assert _value(result, "LD_LIBRARY_PATH") == "<unset>"


def test_launcher_file_is_executable_in_the_installed_tree(site):
    mode = (site / "nixlbench_rocm10_1/bin/nixlbench").stat().st_mode
    assert stat.S_IMODE(mode) == 0o755


# --- dispatcher --------------------------------------------------------------


@pytest.fixture
def merged_site(tmp_path):
    """site-packages holding rocm-nixlbench for ROCm 10.1 and 10.2."""
    directory = tmp_path / "site"
    _install(
        _merge(tmp_path, _backend(tmp_path, "10.1"), _backend(tmp_path, "10.2")),
        directory,
    )
    for rocm in ("10.1", "10.2"):
        _install(
            _make_nixl_wheel(tmp_path / "nixl", name=f"nixl-rocm{rocm}"), directory
        )
    return directory


def _dispatch(site, default_root, **kwargs):
    code = (
        "import importlib, os, nixlbench\n"
        "for name in nixlbench.find_backends().values():\n"
        "    backend = importlib.import_module(name)\n"
        "    backend._DEFAULT_ROCM_ROOT = os.environ['TEST_ROCM_ROOT']\n"
        "nixlbench.main()\n"
    )
    return _run(code, [site], default_root, **kwargs)


def test_dispatcher_follows_the_rocm_install_not_torch(merged_site, tmp_path):
    torch = merged_site / "torch-2.15.0+rocm10.2.0.dist-info"
    torch.mkdir()
    (torch / "METADATA").write_text(
        "Metadata-Version: 2.4\nName: torch\nVersion: 2.15.0+rocm10.2.0\n"
    )
    rocm = _rocm(tmp_path / "rocm", "10.1.0")
    result = _dispatch(merged_site, rocm, args=["--version"], env={"FAKE_EXIT": "3"})
    assert result.returncode == 3
    assert result.stderr == ""
    assert _value(result, "BINARY") == str(
        merged_site / "nixlbench_rocm10_1/bin/nixlbench"
    )
    assert _value(result, "ARG") == "--version"
    assert _value(result, "LD_LIBRARY_PATH") == f"{rocm}/lib"


def test_dispatcher_selects_each_backend(merged_site, tmp_path):
    rocm = _rocm(tmp_path / "rocm", "10.2.1")
    result = _dispatch(merged_site, tmp_path / "none", env={"ROCM_PATH": str(rocm)})
    assert _value(result, "BINARY") == str(
        merged_site / "nixlbench_rocm10_2/bin/nixlbench"
    )
    assert _value(result, "LD_LIBRARY_PATH") == f"{rocm}/lib"


def test_dispatcher_refuses_a_rocm_without_backend(merged_site, tmp_path):
    rocm = _rocm(tmp_path / "rocm", "7.14.1")
    result = _dispatch(merged_site, rocm)
    assert result.returncode == 1
    assert result.stdout == ""
    assert (
        f"nixlbench: found ROCm 7.14 at {rocm} but this rocm-nixlbench has no "
        "backend for it (backends included: 10.1, 10.2)" in result.stderr
    )


def test_dispatcher_without_rocm_runs_the_newest_backend(merged_site, tmp_path):
    result = _dispatch(merged_site, tmp_path / "none")
    assert result.returncode == 0
    assert "no ROCm install found" in result.stderr
    assert "newest ROCm version (10.2)" in result.stderr
    assert _value(result, "BINARY") == str(
        merged_site / "nixlbench_rocm10_2/bin/nixlbench"
    )
    assert _value(result, "LD_LIBRARY_PATH") == "<unset>"


def test_dispatcher_refuses_another_nixl_version(tmp_path):
    directory = tmp_path / "site"
    _install(_merge(tmp_path, _backend(tmp_path, "10.1")), directory)
    _install(
        _make_nixl_wheel(tmp_path / "nixl", name="rocm-nixl", version="1.6.1"),
        directory,
    )
    # The files a merged rocm-nixl installs for this backend.
    _install(_make_nixl_wheel(tmp_path / "nixl"), directory)
    shutil.rmtree(directory / f"nixl_rocm10_1-{VERSION}.dist-info")
    result = _dispatch(directory, _rocm(tmp_path / "rocm"))
    assert result.returncode == 1
    assert (
        f"nixlbench: rocm-nixlbench {VERSION} requires rocm-nixl=={VERSION}, "
        "but rocm-nixl 1.6.1 is installed" in result.stderr
    )


def test_dispatcher_without_backends(tmp_path):
    directory = tmp_path / "site" / "nixlbench"
    directory.mkdir(parents=True)
    (directory / "__init__.py").write_bytes(DISPATCHER.read_bytes())
    result = _run("import nixlbench; nixlbench.main()", [directory.parent], tmp_path)
    assert result.returncode == 1
    assert "no nixlbench_rocm<X>_<Y> backend is installed" in result.stderr
