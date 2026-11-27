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

"""Merging ROCm backend wheels into the nixl-rocm wheel.

Runs contrib/wheel_merge_rocm.py on fabricated backend wheels, so it needs
neither a built NIXL nor a GPU nor network access.
"""

import base64
import csv
import hashlib
import importlib.util
import io
import os
import pathlib
import platform
import re
import subprocess
import sys
import zipfile

import pytest

REPO_DIR = pathlib.Path(__file__).resolve().parents[2]
MERGE_SCRIPT = REPO_DIR / "contrib" / "wheel_merge_rocm.py"
META_DIR = REPO_DIR / "src/bindings/python/nixl-meta"

VERSION = "1.6.0"
PY_TAG = "cp{}{}".format(*sys.version_info[:2])
ABI_TAG = PY_TAG + getattr(sys, "abiflags", "")
PLATFORM = "manylinux_2_28_x86_64"
BACKEND_SUBMODULES = ("_bindings", "_utils", "logging", "_api")


def _load_merge_module():
    spec = importlib.util.spec_from_file_location("wheel_merge_rocm", MERGE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wheel_merge_rocm = _load_merge_module()


def _sha256_b64(data):
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _make_wheel(
    directory,
    name,
    version,
    files,
    tags=("py3", "none", "any"),
    headers=(),
    dist_info_files=None,
    file_prefix=None,
):
    """Write a wheel holding `files` (path -> bytes) and return its path."""
    prefix = file_prefix or name.replace("-", "_").replace(".", "_")
    dist_info = f"{prefix}-{version}.dist-info"
    metadata = "\n".join(
        ["Metadata-Version: 2.4", f"Name: {name}", f"Version: {version}", *headers]
    )
    entries = dict(files)
    entries[f"{dist_info}/METADATA"] = (metadata + "\n\nbackend readme\n").encode()
    entries[f"{dist_info}/WHEEL"] = (
        "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: false\n"
        f"Tag: {'-'.join(tags)}\n"
    ).encode()
    for relative, data in (dist_info_files or {}).items():
        entries[f"{dist_info}/{relative}"] = data
    rows = [[n, f"sha256={_sha256_b64(d)}", str(len(d))] for n, d in entries.items()]
    rows.append([f"{dist_info}/RECORD", "", ""])
    record = io.StringIO()
    csv.writer(record, lineterminator="\n").writerows(rows)
    entries[f"{dist_info}/RECORD"] = record.getvalue().encode()

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{prefix}-{version}-{'-'.join(tags)}.whl"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as whl:
        for entry, data in entries.items():
            info = zipfile.ZipInfo(entry, date_time=(2026, 1, 2, 3, 4, 6))
            info.compress_type = zipfile.ZIP_DEFLATED
            mode = 0o100755 if entry.endswith(".so") else 0o100644
            info.external_attr = mode << 16
            whl.writestr(info, data)
    return path


def _backend_files(package):
    files = {f"{package}/__init__.py": f"BACKEND = {package!r}\n".encode()}
    for submodule in BACKEND_SUBMODULES:
        files[f"{package}/{submodule}.py"] = f"WHERE = {package!r}\n".encode()
    files[f".{package}.mesonpy.libs/libnixl.so"] = b"libnixl " + package.encode()
    files[f".{package}.mesonpy.libs/plugins/libplugin_UCX.so"] = (
        b"plugin " + package.encode()
    )
    files[f"{package}.libs/libucs-0123abcd.so.0.0.0"] = b"ucs " + package.encode()
    files[f"{package}.libs/ucx/libuct_ib.so.0"] = b"uct " + package.encode()
    files[f"{package}.libs/nixl/libplugin_POSIX.so"] = b"posix " + package.encode()
    return files


def _make_backend(
    directory,
    rocm="10.1",
    version=VERSION,
    tags=(PY_TAG, ABI_TAG, PLATFORM),
    name=None,
    extra_files=None,
    license_text=b"Apache license text\n",
    requires_python=">=3.10",
):
    package = "nixl_rocm" + rocm.replace(".", "_")
    files = _backend_files(package)
    files.update(extra_files or {})
    return _make_wheel(
        directory,
        name or f"nixl-rocm{rocm}",
        version,
        files,
        tags=tags,
        headers=(
            "Summary: NIXL Python API",
            "Author-email: NIXL Developers <nixl-developers@nvidia.com>",
            "License-Expression: MIT AND Apache-2.0",
            "License-File: LICENSE",
            "License-File: licenses/NVIDIA-proprietary-LICENSE.txt",
            "Classifier: Programming Language :: Python :: 3",
            "Project-URL: Homepage, https://github.com/ai-dynamo/nixl",
            f"Requires-Python: {requires_python}",
            "Requires-Dist: numpy",
            "Description-Content-Type: text/markdown",
        ),
        dist_info_files={
            "licenses/LICENSE": license_text,
            "licenses/licenses/NVIDIA-proprietary-LICENSE.txt": b"NVIDIA license\n",
            "sboms/auditwheel.cdx.json": b"{}",
        },
        file_prefix=package,
    )


def _merge(tmp_path, *backends):
    return pathlib.Path(
        wheel_merge_rocm.merge(
            [str(b) for b in backends], str(tmp_path / "out"), str(META_DIR)
        )
    )


def _parsed_backends(*rocm):
    return [
        wheel_merge_rocm.parse_backend(
            f"nixl_rocm{version.replace('.', '_')}-{VERSION}-"
            f"{PY_TAG}-{ABI_TAG}-{PLATFORM}.whl"
        )
        for version in rocm
    ]


def _install_command(rocm):
    index_url = wheel_merge_rocm.ROCM_PYTORCH[rocm].index_url
    return f'pip install "nixl-rocm[rocm{rocm}]" --extra-index-url {index_url}'


def _type_stub(wheel):
    with zipfile.ZipFile(wheel) as whl:
        return whl.read("nixl/_api.py").decode()


def _metadata(wheel):
    dist_info = f"nixl_rocm-{VERSION}.dist-info"
    with zipfile.ZipFile(wheel) as whl:
        text = whl.read(f"{dist_info}/METADATA").decode()
    head, _, body = text.partition("\n\n")
    return head.splitlines(), body


@pytest.fixture
def backends(tmp_path):
    return (
        _make_backend(tmp_path / "rocm10.2", "10.2"),
        _make_backend(tmp_path / "rocm10.1", "10.1"),
        _make_backend(tmp_path / "rocm7.14", "7.14"),
    )


@pytest.fixture
def merged(tmp_path, backends):
    return _merge(tmp_path, *backends)


def test_merged_wheel_name(merged):
    assert merged.name == f"nixl_rocm-{VERSION}-{PY_TAG}-{ABI_TAG}-{PLATFORM}.whl"
    assert [p.name for p in merged.parent.iterdir()] == [merged.name]


def test_merged_wheel_contents(merged, backends):
    dist_info = f"nixl_rocm-{VERSION}.dist-info"
    expected = {
        "nixl_meta_utils.py",
        f"{dist_info}/METADATA",
        f"{dist_info}/WHEEL",
        f"{dist_info}/RECORD",
        f"{dist_info}/top_level.txt",
        f"{dist_info}/licenses/LICENSE",
        f"{dist_info}/licenses/licenses/NVIDIA-proprietary-LICENSE.txt",
    }
    # The Python files of the nixl meta wheel. The CUDA-only nixl.cute and
    # nixl_ep are placeholders: test_cuda_only_modules_say_so.
    expected |= {
        "nixl/__init__.py",
        "nixl/_api.py",
        "nixl/cute.py",
        "nixl/logging.py",
        "nixl/runtime/__init__.py",
        "nixl_ep/__init__.py",
    }
    expected.add("nixl/rocm_pytorch_install.txt")
    for package in ("nixl_rocm10_2", "nixl_rocm10_1", "nixl_rocm7_14"):
        expected |= set(_backend_files(package))

    with zipfile.ZipFile(merged) as whl:
        names = whl.namelist()
        assert len(names) == len(set(names))
        assert set(names) == expected
        assert names[-1] == f"{dist_info}/RECORD"
        assert whl.testzip() is None

        for package in ("nixl_rocm10_2", "nixl_rocm10_1", "nixl_rocm7_14"):
            for name, data in _backend_files(package).items():
                assert whl.read(name) == data
        for name in ("nixl/__init__.py", "nixl_meta_utils.py"):
            assert whl.read(name) == (META_DIR / name).read_bytes()

        lib = whl.getinfo(".nixl_rocm10_1.mesonpy.libs/plugins/libplugin_UCX.so")
        assert (lib.external_attr >> 16) & 0o777 == 0o755
        assert whl.read(f"{dist_info}/top_level.txt").decode().split() == [
            "nixl",
            "nixl_ep",
            "nixl_meta_utils",
            "nixl_rocm10_1",
            "nixl_rocm10_2",
            "nixl_rocm7_14",
        ]
    assert [n for n in names if n.startswith("nixl_ep")] == ["nixl_ep/__init__.py"]
    # The backends' own metadata is left out: only the merged wheel's remains.
    assert {n.split("/")[0] for n in names if ".dist-info/" in n} == {dist_info}


def test_merged_wheel_metadata(merged):
    headers, body = _metadata(merged)
    pins = wheel_merge_rocm.ROCM_PYTORCH

    assert headers[0] == "Metadata-Version: 2.4"
    assert "Name: nixl-rocm" in headers
    assert f"Version: {VERSION}" in headers
    assert "Requires-Python: >=3.10" in headers
    assert "License-Expression: MIT AND Apache-2.0" in headers
    assert "License-File: LICENSE" in headers
    assert "License-File: licenses/NVIDIA-proprietary-LICENSE.txt" in headers
    assert "Classifier: Programming Language :: Python :: 3" in headers
    assert "Project-URL: Homepage, https://github.com/ai-dynamo/nixl" in headers
    assert headers.count("Description-Content-Type: text/markdown") == 1
    assert sum(h.startswith("Name:") for h in headers) == 1

    assert [h for h in headers if h.startswith("Requires-Dist:")] == [
        "Requires-Dist: numpy",
        f'Requires-Dist: torch=={pins["7.14"].torch}; extra == "rocm7-14"',
        f'Requires-Dist: torch=={pins["10.1"].torch}; extra == "rocm10-1"',
        f'Requires-Dist: torch=={pins["10.2"].torch}; extra == "rocm10-2"',
    ]
    assert [h for h in headers if h.startswith("Provides-Extra:")] == [
        "Provides-Extra: rocm7-14",
        "Provides-Extra: rocm10-1",
        "Provides-Extra: rocm10-2",
    ]
    assert "backend readme" not in body
    template = (META_DIR / "README.rocm.md").read_text()
    assert body.splitlines()[0] == template.splitlines()[0]
    assert "<!--" not in body
    assert "7.14, 10.1 and 10.2" in body
    for rocm in pins:
        assert _install_command(rocm) in body


def test_rocm_10_2_extra(merged):
    # No Python marker: on Python 3.10, where AMD has no torch build for ROCm
    # 10.2, pip fails rather than install nixl-rocm without PyTorch.
    headers, _ = _metadata(merged)
    assert 'Requires-Dist: torch==2.15.0+rocm10.2.0; extra == "rocm10-2"' in headers


def test_pytorch_pins_are_exact_rocm_builds():
    for rocm, pin in wheel_merge_rocm.ROCM_PYTORCH.items():
        assert pin.torch.split("+rocm")[1].startswith(rocm + ".")
        assert pin.index_url.startswith("https://") and pin.index_url.endswith("/")


VERSIONS_REGION = "<!-- rocm-versions -->stale<!-- /rocm-versions -->"
INSTALL_REGION = (
    "<!-- rocm-pytorch-install: a note\nover two lines -->\n"
    "stale\n"
    "<!-- /rocm-pytorch-install -->"
)


def test_readme_names_the_merged_rocm_versions():
    template = f"Supports {VERSIONS_REGION}.\n{INSTALL_REGION}\nEnd.\n"

    def render(*rocm):
        return wheel_merge_rocm.render_readme(template, _parsed_backends(*rocm))

    assert render("7.14", "10.1", "10.2") == (
        "Supports 7.14, 10.1 and 10.2.\n"
        "```bash\n"
        "# ROCm 10.2 (Python 3.11 or newer)\n"
        f"{_install_command('10.2')}\n\n"
        "# ROCm 10.1\n"
        f"{_install_command('10.1')}\n\n"
        "# ROCm 7.14\n"
        f"{_install_command('7.14')}\n"
        "```\n"
        "End.\n"
    )
    assert render("7.14", "10.1").startswith(
        "Supports 7.14 and 10.1.\n```bash\n# ROCm 10.1\n"
    )
    assert render("10.1").startswith("Supports 10.1.\n```bash\n# ROCm 10.1\n")


def test_readme_in_the_source_tree_shows_every_supported_version():
    # It is what the repository and the dispatcher's error message link to.
    template = (META_DIR / "README.rocm.md").read_text()
    backends = _parsed_backends(*wheel_merge_rocm.ROCM_PYTORCH)
    backends.sort(key=lambda backend: backend.rocm)
    rendered = wheel_merge_rocm.render_readme(template, backends)
    assert rendered == re.sub(
        r"<!-- /?rocm-.*?-->\n?", "", template, flags=re.S
    ), "README.rocm.md does not match ROCM_PYTORCH: update its generated regions"


@pytest.mark.parametrize(
    "template, region",
    [
        (INSTALL_REGION, "rocm-versions"),
        (VERSIONS_REGION, "rocm-pytorch-install"),
        (VERSIONS_REGION * 2 + INSTALL_REGION, "rocm-versions"),
    ],
)
def test_refuses_readme_without_exactly_one_of_each_region(template, region):
    with pytest.raises(
        wheel_merge_rocm.MergeError,
        match=f"README.rocm.md: expected one <!-- {region} --> region",
    ):
        wheel_merge_rocm.render_readme(template, _parsed_backends("10.1"))


def test_install_commands_for_the_dispatcher(merged):
    # nixl/__init__.py prints them when it finds a CUDA build of PyTorch.
    with zipfile.ZipFile(merged) as whl:
        commands = whl.read("nixl/rocm_pytorch_install.txt").decode()
    assert commands == (
        "# ROCm 10.2 (Python 3.11 or newer)\n"
        f"{_install_command('10.2')}\n\n"
        "# ROCm 10.1\n"
        f"{_install_command('10.1')}\n\n"
        "# ROCm 7.14\n"
        f"{_install_command('7.14')}\n"
    )
    _, body = _metadata(merged)
    assert commands in body


def test_type_stub_names_the_newest_merged_backend(merged):
    source = (META_DIR / "nixl" / "_api.py").read_text()
    assert _type_stub(merged) == source.replace(
        "from nixl_rocm._api import", "from nixl_rocm10_2._api import"
    )
    assert "from nixl_rocm._api import" not in _type_stub(merged)


def test_logging_stub_names_the_newest_merged_backend(merged):
    source = (META_DIR / "nixl" / "logging.py").read_text()
    with zipfile.ZipFile(merged) as whl:
        stub = whl.read("nixl/logging.py").decode()
    assert stub == source.replace(
        "from nixl_cu13.logging import", "from nixl_rocm10_2.logging import"
    )
    assert "nixl_cu13" not in stub


@pytest.mark.parametrize("module", ["nixl.cute", "nixl_ep"])
def test_cuda_only_modules_say_so(merged, tmp_path, module):
    # Where the nixl meta wheel has a module that needs a CUDA backend, the
    # merged wheel has one that says so when it is imported.
    path = module.replace(".", "/")
    source = META_DIR / (path + ".py")
    if not source.is_file():
        source = META_DIR / path / "__init__.py"
    assert source.is_file()
    name = str(source.relative_to(META_DIR))
    with zipfile.ZipFile(merged) as whl:
        placeholder = whl.read(name)
    assert placeholder != source.read_bytes()
    with pytest.raises(ImportError) as excinfo:
        exec(compile(placeholder, name, "exec"), {})
    assert excinfo.value.name == module
    assert str(excinfo.value) == (
        f"{module} is CUDA-only: it is not available in nixl-rocm, "
        "the NIXL wheel for AMD ROCm"
    )
    assert not isinstance(excinfo.value, ModuleNotFoundError)


@pytest.mark.parametrize("stub", [b"", b"from nixl_rocm._api import a\n" * 2])
def test_refuses_type_stub_it_cannot_retarget(stub):
    with pytest.raises(
        wheel_merge_rocm.MergeError,
        match=re.escape("expected one 'from nixl_rocm._api import'"),
    ):
        wheel_merge_rocm.retarget_stub(stub, "nixl_rocm10_1")


def test_merged_wheel_tag(merged):
    with zipfile.ZipFile(merged) as whl:
        lines = whl.read(f"nixl_rocm-{VERSION}.dist-info/WHEEL").decode().splitlines()
    assert "Wheel-Version: 1.0" in lines
    assert "Root-Is-Purelib: false" in lines
    assert [line for line in lines if line.startswith("Tag:")] == [
        f"Tag: {PY_TAG}-{ABI_TAG}-{PLATFORM}"
    ]


def test_merged_wheel_record(merged):
    record_path = f"nixl_rocm-{VERSION}.dist-info/RECORD"
    with zipfile.ZipFile(merged) as whl:
        rows = list(csv.reader(io.StringIO(whl.read(record_path).decode())))
        assert sorted(row[0] for row in rows) == sorted(whl.namelist())
        for name, digest, size in rows:
            if name == record_path:
                assert (digest, size) == ("", "")
                continue
            data = whl.read(name)
            assert digest == f"sha256={_sha256_b64(data)}"
            assert size == str(len(data))


def test_backend_data_and_scripts_are_left_out(tmp_path):
    inputs = []
    for rocm in ("10.1", "7.14"):
        package = "nixl_rocm" + rocm.replace(".", "_")
        data = f"{package}-{VERSION}.data"
        inputs.append(
            _make_backend(
                tmp_path / f"rocm{rocm}",
                rocm,
                extra_files={
                    f"{data}/headers/asio.hpp": b"asio\n",
                    f"{data}/data/pkgconfig/asio.pc": b"asio\n",
                    f"{data}/scripts/nixl_example": b"exec " + package.encode(),
                    f"{package}.scripts/nixl_example": b"elf " + package.encode(),
                },
            )
        )
    merged = _merge(tmp_path, *inputs)
    dist_info = f"nixl_rocm-{VERSION}.dist-info"
    with zipfile.ZipFile(merged) as whl:
        names = whl.namelist()
        record = whl.read(f"{dist_info}/RECORD").decode()
    assert not any(".data/" in n or ".scripts/" in n for n in names)
    assert ".data/" not in record and ".scripts/" not in record
    assert "nixl_rocm10_1/__init__.py" in names
    assert "nixl_rocm7_14.libs/ucx/libuct_ib.so.0" in names


def test_single_backend_merge(tmp_path):
    merged = _merge(tmp_path, _make_backend(tmp_path / "in", "10.1"))
    headers, _ = _metadata(merged)
    assert [h for h in headers if h.startswith("Provides-Extra:")] == [
        "Provides-Extra: rocm10-1"
    ]
    with zipfile.ZipFile(merged) as whl:
        assert "nixl/__init__.py" in whl.namelist()


def test_subset_merge_names_only_its_backends(tmp_path):
    merged = _merge(
        tmp_path,
        _make_backend(tmp_path / "rocm10.1", "10.1"),
        _make_backend(tmp_path / "rocm7.14", "7.14"),
    )
    _, body = _metadata(merged)
    assert "7.14 and 10.1" in body
    assert _install_command("10.1") in body
    assert "10.2" not in body
    assert "from nixl_rocm10_1._api import" in _type_stub(merged)
    with zipfile.ZipFile(merged) as whl:
        assert "10.2" not in whl.read("nixl/rocm_pytorch_install.txt").decode()


def test_refuses_version_mismatch(tmp_path):
    a = _make_backend(tmp_path / "a", "10.1")
    b = _make_backend(tmp_path / "b", "7.14", version="1.6.1")
    with pytest.raises(wheel_merge_rocm.MergeError, match="disagree on version"):
        _merge(tmp_path, a, b)


def test_refuses_python_tag_mismatch(tmp_path):
    a = _make_backend(tmp_path / "a", "10.1", tags=("cp312", "cp312", PLATFORM))
    b = _make_backend(tmp_path / "b", "7.14", tags=("cp313", "cp313", PLATFORM))
    with pytest.raises(wheel_merge_rocm.MergeError, match="disagree on Python/ABI tag"):
        _merge(tmp_path, a, b)


def test_refuses_platform_tag_mismatch(tmp_path):
    a = _make_backend(tmp_path / "a", "10.1")
    b = _make_backend(
        tmp_path / "b", "7.14", tags=(PY_TAG, ABI_TAG, "manylinux_2_39_x86_64")
    )
    with pytest.raises(wheel_merge_rocm.MergeError, match="disagree on platform tag"):
        _merge(tmp_path, a, b)


def test_refuses_non_backend_wheel(tmp_path):
    a = _make_backend(tmp_path / "a", "10.1")
    cuda = _make_wheel(
        tmp_path / "b",
        "nixl-cu12",
        VERSION,
        {"nixl_cu12/__init__.py": b""},
        tags=(PY_TAG, ABI_TAG, PLATFORM),
    )
    with pytest.raises(wheel_merge_rocm.MergeError, match="not a nixl_rocm<X>_<Y>"):
        _merge(tmp_path, a, cuda)


def test_refuses_backend_with_wrong_distribution_name(tmp_path):
    wrong = _make_backend(tmp_path / "a", "10.1", name="nixl-cu12")
    with pytest.raises(wheel_merge_rocm.MergeError, match="METADATA Name is nixl-cu12"):
        _merge(tmp_path, wrong)


def test_refuses_duplicate_rocm_version(tmp_path):
    a = _make_backend(tmp_path / "a", "10.1")
    b = _make_backend(tmp_path / "b", "10.1")
    with pytest.raises(
        wheel_merge_rocm.MergeError, match="duplicate backend for ROCm 10.1"
    ):
        _merge(tmp_path, a, b)


def test_refuses_rocm_version_without_pytorch_pin(tmp_path):
    a = _make_backend(tmp_path / "a", "9.9")
    with pytest.raises(
        wheel_merge_rocm.MergeError, match="no PyTorch pin for ROCm 9.9"
    ):
        _merge(tmp_path, a)


def test_refuses_collision_between_backends(tmp_path):
    shared = {"shared.libs/libfoo.so": b"foo"}
    a = _make_backend(tmp_path / "a", "10.1", extra_files=shared)
    b = _make_backend(tmp_path / "b", "7.14", extra_files=shared)
    with pytest.raises(
        wheel_merge_rocm.MergeError, match="path collision: shared.libs/libfoo.so"
    ):
        _merge(tmp_path, a, b)


def test_refuses_collision_with_dispatcher(tmp_path):
    a = _make_backend(
        tmp_path / "a", "10.1", extra_files={"nixl/__init__.py": b"# stray\n"}
    )
    with pytest.raises(
        wheel_merge_rocm.MergeError,
        match="path collision: nixl/__init__.py is in both the nixl dispatcher",
    ):
        _merge(tmp_path, a)


def test_refuses_differing_license_files(tmp_path):
    a = _make_backend(tmp_path / "a", "10.1")
    b = _make_backend(tmp_path / "b", "7.14", license_text=b"other\n")
    with pytest.raises(wheel_merge_rocm.MergeError, match="license file .* differs"):
        _merge(tmp_path, a, b)


def test_refuses_requires_python_mismatch(tmp_path):
    a = _make_backend(tmp_path / "a", "10.1")
    b = _make_backend(tmp_path / "b", "7.14", requires_python=">=3.11")
    with pytest.raises(
        wheel_merge_rocm.MergeError, match="disagree on Requires-Python"
    ):
        _merge(tmp_path, a, b)


def test_failed_merge_writes_nothing(tmp_path):
    shared = {"shared.libs/libfoo.so": b"foo"}
    a = _make_backend(tmp_path / "a", "10.1", extra_files=shared)
    b = _make_backend(tmp_path / "b", "7.14", extra_files=shared)
    with pytest.raises(wheel_merge_rocm.MergeError):
        _merge(tmp_path, a, b)
    out = tmp_path / "out"
    assert not out.exists() or not list(out.iterdir())


def _run_script(*args):
    return subprocess.run(
        [sys.executable, str(MERGE_SCRIPT), *map(str, args)],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )


def test_command_line(tmp_path, backends):
    out = tmp_path / "cli-out"
    result = _run_script("--output-dir", out, *backends)
    assert result.returncode == 0, result.stderr
    name = f"nixl_rocm-{VERSION}-{PY_TAG}-{ABI_TAG}-{PLATFORM}.whl"
    assert (out / name).is_file()
    assert name in result.stdout
    assert "ROCm 7.14: nixl_rocm7_14" in result.stdout
    assert "ROCm 10.1: nixl_rocm10_1" in result.stdout
    assert "ROCm 10.2: nixl_rocm10_2" in result.stdout
    assert f"wheel size: {(out / name).stat().st_size} bytes" in result.stdout


def test_command_line_reports_refusal(tmp_path, backends):
    result = _run_script("--output-dir", tmp_path / "cli-out", backends[0], backends[0])
    assert result.returncode == 1
    assert "ERROR: duplicate backend for ROCm 10.2" in result.stderr
    assert "Traceback" not in result.stderr


def _pip(python, *args):
    return subprocess.run(
        [str(python), "-m", "pip", "--disable-pip-version-check", *map(str, args)],
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def venv(tmp_path_factory):
    """A venv with the merged wheel installed from local files only."""
    if sys.platform != "linux" or platform.machine() != "x86_64":
        pytest.skip("the merged wheel is tagged manylinux_2_28_x86_64")
    libc, libc_version = platform.libc_ver()
    if libc != "glibc" or tuple(map(int, libc_version.split(".")[:2])) < (2, 28):
        pytest.skip("the merged wheel is tagged manylinux_2_28_x86_64")

    root = tmp_path_factory.mktemp("install")
    merged = _merge(
        root,
        _make_backend(root / "rocm10.2", "10.2"),
        _make_backend(root / "rocm10.1", "10.1"),
        _make_backend(root / "rocm7.14", "7.14"),
    )
    links = root / "links"
    _make_wheel(links, "numpy", "2.0.0", {"numpy/__init__.py": b""})

    created = subprocess.run(
        [sys.executable, "-m", "venv", str(root / "venv")],
        capture_output=True,
        text=True,
    )
    if created.returncode != 0:
        pytest.skip(f"cannot create a venv: {created.stderr.strip()}")
    python = root / "venv" / "bin" / "python"

    installed = _pip(
        python, "install", "--no-index", "--find-links", links, merged, "numpy"
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return python, merged, root


def test_installs_with_only_numpy(venv):
    python, _, _ = venv
    listed = _pip(python, "list", "--format=freeze")
    assert listed.returncode == 0, listed.stderr
    packages = {line.split("==")[0] for line in listed.stdout.split()}
    assert packages - {"pip", "setuptools", "wheel"} == {"nixl-rocm", "numpy"}
    assert f"nixl-rocm=={VERSION}" in listed.stdout.split()
    checked = _pip(python, "check")
    assert checked.returncode == 0, checked.stdout


@pytest.mark.parametrize(
    "rocm_version, backend",
    [
        ("10.2.0", "nixl_rocm10_2"),
        ("10.1.0", "nixl_rocm10_1"),
        ("7.14.1", "nixl_rocm7_14"),
    ],
)
def test_installed_wheel_selects_backend(venv, tmp_path, rocm_version, backend):
    python, _, _ = venv
    (tmp_path / ".info").mkdir()
    (tmp_path / ".info" / "version").write_text(rocm_version + "\n")
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "libamdhip64.so.7").write_bytes(b"")
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(ROCM_PATH=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [str(python), "-c", "import sys, nixl; sys.stdout.write(nixl._api.WHERE)"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == backend


def test_installed_wheel_names_the_commands_that_replace_a_cuda_torch(venv, tmp_path):
    python, _, _ = venv
    (tmp_path / "torch").mkdir()
    (tmp_path / "torch" / "__init__.py").write_text("")
    (tmp_path / "torch" / "version.py").write_text('cuda = "12.6"\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env.update(PYTHONPATH=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    result = subprocess.run(
        [str(python), "-c", "import nixl"],
        capture_output=True,
        text=True,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode != 0
    assert "found a CUDA build of PyTorch (CUDA 12.6)" in result.stderr
    assert 'Run "pip uninstall torch", then the command for your ROCm' in result.stderr
    for rocm in wheel_merge_rocm.ROCM_PYTORCH:
        assert _install_command(rocm) in result.stderr


@pytest.mark.parametrize("rocm", ["10.2", "10.1", "7.14"])
def test_extra_installs_only_the_rocm_pytorch(venv, tmp_path, rocm):
    python, merged, _ = venv
    pip_version = _pip(python, "--version").stdout.split()[1]
    if tuple(map(int, pip_version.split(".")[:2])) < (23, 3):
        pytest.skip(f"pip {pip_version} in the venv is older than 23.3")
    pin = wheel_merge_rocm.ROCM_PYTORCH[rocm].torch
    public = pin.split("+")[0]
    numpy = _make_wheel(
        tmp_path / "numpy", "numpy", "2.0.0", {"numpy/__init__.py": b""}
    )
    requirement = f"nixl-rocm[rocm{rocm}] @ {merged.as_uri()}"

    # Only the CUDA-style public version is available: the extra cannot resolve.
    pypi_only = tmp_path / "pypi-only"
    _make_wheel(pypi_only, "torch", public, {"torch/__init__.py": b""})
    refused = _pip(
        python,
        "install",
        "--dry-run",
        "--no-index",
        "--find-links",
        numpy.parent,
        "--find-links",
        pypi_only,
        requirement,
    )
    assert refused.returncode != 0
    assert "torch" in refused.stderr

    with_amd = tmp_path / "with-amd"
    _make_wheel(with_amd, "torch", public, {"torch/__init__.py": b""})
    _make_wheel(
        with_amd,
        "torch",
        pin,
        {"torch/__init__.py": b""},
        file_prefix="torch",
    )
    resolved = _pip(
        python,
        "install",
        "--dry-run",
        "--no-index",
        "--find-links",
        numpy.parent,
        "--find-links",
        with_amd,
        requirement,
    )
    assert resolved.returncode == 0, resolved.stdout + resolved.stderr
    assert f"torch-{pin}" in resolved.stdout
