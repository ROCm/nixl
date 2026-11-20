#!/usr/bin/env python3

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

"""Build, check and merge nixlbench wheels.

A nixlbench wheel holds the nixlbench binary and a launcher, and depends on the
NIXL backend wheel it was linked against: nixlbench-<variant> requires
nixl-<variant> at exactly the same version, because libnixl has no versioned
ABI.

  build  assemble the unrepaired nixlbench_<variant> wheel from a built binary
  check  verify a repaired nixlbench_<variant> wheel
  merge  merge nixlbench_rocm<X>_<Y> wheels into one rocm-nixlbench wheel
"""

from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from typing import NamedTuple

GENERATOR = "wheel_nixlbench"
SUMMARY = "NIXL benchmark tool (nixlbench)"
HOMEPAGE = "https://github.com/ai-dynamo/nixl"
# What the nixlbench binary links from the NIXL wheel, by DT_NEEDED name.
NIXL_LIBRARIES = ("libnixl.so", "libnixl_build.so", "libserdes.so")

_ROCM_BACKEND_RE = re.compile(r"nixlbench_rocm(?P<major>\d+)_(?P<minor>\d+)")
_WHEEL_NAME_RE = re.compile(
    r"(?P<prefix>[^-]+)-(?P<version>[^-]+)"
    r"-(?P<python>[^-]+)-(?P<abi>[^-]+)-(?P<platform>[^-]+)\.whl"
)
_PINNED_RE = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;,]+)\s*")
_GENERATED_TIME = (1980, 1, 1, 0, 0, 0)
_REGULAR_FILE = 0o100644 << 16
_EXECUTABLE_FILE = 0o100755 << 16


class WheelError(Exception):
    """A wheel cannot be built, checked or merged."""


class NixlWheel(NamedTuple):
    name: str
    version: str
    package: str
    requires_python: list[str]


class Backend(NamedTuple):
    path: str
    rocm: tuple[int, int]
    version: str
    python: str
    abi: str
    platform: str

    @property
    def package(self) -> str:
        return "nixlbench_rocm{}_{}".format(*self.rocm)

    @property
    def nixl_requirement(self) -> str:
        return "nixl-rocm{}.{}=={}".format(*self.rocm, self.version)

    @property
    def tags(self) -> tuple[str, str, str]:
        return (self.python, self.abi, self.platform)


def _sha256_b64(data: bytes) -> str:
    digest = hashlib.sha256(data).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _package(name: str) -> str:
    """The import package (and wheel file prefix) of a distribution name."""
    return _normalize(name).replace("-", "_")


def _split_metadata(text: str) -> tuple[list[tuple[str, str]], str]:
    """Split METADATA into its (field, value) headers and its body."""
    head, _, body = text.partition("\n\n")
    headers: list[tuple[str, str]] = []
    for line in head.splitlines():
        if line[:1] in (" ", "\t") and headers:
            field, value = headers[-1]
            headers[-1] = (field, value + "\n" + line)
            continue
        field, sep, value = line.partition(":")
        if not sep:
            raise WheelError(f"malformed METADATA header line: {line!r}")
        headers.append((field.strip(), value.strip()))
    return headers, body


def _field(headers: list[tuple[str, str]], name: str) -> list[str]:
    return [value for field, value in headers if field.lower() == name.lower()]


def _same_requirement(requirement: str, expected: str) -> bool:
    """Whether both are the same "<name>==<version>" pin."""
    mine, theirs = _PINNED_RE.fullmatch(requirement), _PINNED_RE.fullmatch(expected)
    return (
        mine is not None
        and theirs is not None
        and _normalize(mine.group(1)) == _normalize(theirs.group(1))
        and mine.group(2) == theirs.group(2)
    )


def read_metadata(path: str, whl: zipfile.ZipFile) -> tuple[str, list[tuple[str, str]]]:
    """Return a wheel's dist-info directory and its METADATA headers."""
    name = os.path.basename(path)
    dist_infos = sorted(
        {
            entry.split("/", 1)[0]
            for entry in whl.namelist()
            if entry.split("/", 1)[0].endswith(".dist-info")
        }
    )
    if len(dist_infos) != 1:
        raise WheelError(
            f"{name}: expected one .dist-info directory, found "
            f"{', '.join(dist_infos) or 'none'}"
        )
    try:
        text = whl.read(f"{dist_infos[0]}/METADATA").decode("utf-8")
    except KeyError:
        raise WheelError(f"{name}: no {dist_infos[0]}/METADATA") from None
    headers, _ = _split_metadata(text)
    return dist_infos[0], headers


def _single(name: str, headers: list[tuple[str, str]], field: str) -> str:
    values = _field(headers, field)
    if len(values) != 1:
        raise WheelError(
            f"{name}: expected one METADATA {field}, found "
            f"{', '.join(values) or 'none'}"
        )
    return values[0]


def read_nixl_wheel(path: str) -> NixlWheel:
    """Describe the NIXL backend wheel nixlbench was linked against."""
    name = os.path.basename(path)
    with zipfile.ZipFile(path) as whl:
        _, headers = read_metadata(path, whl)
        entries = set(whl.namelist())
    dist_name = _single(name, headers, "Name")
    if not _normalize(dist_name).startswith("nixl-"):
        raise WheelError(
            f"{name}: not a NIXL backend wheel: METADATA Name is {dist_name}, "
            "expected nixl-<variant> (the nixl meta wheel has no libnixl)"
        )
    package = _package(dist_name)
    if not any(entry.startswith(f"{package}/") for entry in entries):
        raise WheelError(f"{name}: import package {package} is missing")
    libnixl = f".{package}.mesonpy.libs/libnixl.so"
    if libnixl not in entries:
        raise WheelError(
            f"{name}: {libnixl} is missing; the nixlbench RPATH and launcher "
            "expect the meson-python layout"
        )
    return NixlWheel(
        name=dist_name,
        version=_single(name, headers, "Version"),
        package=package,
        requires_python=_field(headers, "Requires-Python"),
    )


def _wheel_file(python: str, abi: str, platform: str) -> str:
    lines = [
        "Wheel-Version: 1.0",
        f"Generator: {GENERATOR}",
        "Root-Is-Purelib: false",
    ]
    # A compressed tag set expands to one Tag line per combination.
    for py in python.split("."):
        for one_abi in abi.split("."):
            for plat in platform.split("."):
                lines.append(f"Tag: {py}-{one_abi}-{plat}")
    return "\n".join(lines) + "\n"


def _entry_points(package: str) -> bytes:
    return f"[console_scripts]\nnixlbench = {package}:main\n".encode("utf-8")


def _generated(name: str, mode: int = _REGULAR_FILE) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(filename=name, date_time=_GENERATED_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = mode
    return info


def _write_wheel(
    out_path: str,
    dist_info: str,
    entries: list[tuple[zipfile.ZipInfo, bytes]],
) -> None:
    """Write the entries and their RECORD, the dist-info directory last."""
    record_path = f"{dist_info}/RECORD"
    rows = []
    tmp_path = f"{out_path}.tmp"
    with zipfile.ZipFile(
        tmp_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
    ) as zout:
        for info, data in sorted(
            entries,
            key=lambda e: (e[0].filename.startswith(dist_info), e[0].filename),
        ):
            zout.writestr(info, data)
            rows.append([info.filename, f"sha256={_sha256_b64(data)}", str(len(data))])
        rows.append([record_path, "", ""])
        buf = io.StringIO()
        csv.writer(buf, lineterminator="\n").writerows(rows)
        zout.writestr(_generated(record_path), buf.getvalue().encode("utf-8"))
    os.replace(tmp_path, out_path)


def _read_file(path: str, what: str) -> bytes:
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError as e:
        raise WheelError(f"cannot read {what} {path}: {e}") from e


def build(
    binary: str,
    nixl_wheel: str,
    platform: str,
    output_dir: str,
    launcher: str,
    license_expression: str,
    license_files: list[tuple[str, str]],
) -> str:
    """Assemble the nixlbench wheel for one NIXL backend wheel; return its path."""
    nixl = read_nixl_wheel(nixl_wheel)
    dist_name = "nixlbench" + nixl.name[len("nixl") :]
    package = "nixlbench" + nixl.package[len("nixl") :]
    dist_info = f"{package}-{nixl.version}.dist-info"

    metadata = [
        "Metadata-Version: 2.4",
        f"Name: {dist_name}",
        f"Version: {nixl.version}",
        f"Summary: {SUMMARY} for {nixl.name}",
        f"Project-URL: Homepage, {HOMEPAGE}",
        f"License-Expression: {license_expression}",
    ]
    licenses = {}
    for relative, source in license_files:
        parts = relative.split("/")
        if os.path.isabs(relative) or "" in parts or ".." in parts:
            raise WheelError(f"invalid license file path in the wheel: {relative!r}")
        if relative in licenses:
            raise WheelError(f"license file {relative} given twice")
        licenses[relative] = _read_file(source, "license file")
        metadata.append(f"License-File: {relative}")
    metadata += [f"Requires-Python: {value}" for value in nixl.requires_python]
    metadata.append(f"Requires-Dist: {nixl.name}=={nixl.version}")

    entries = [
        (_generated(f"{package}/__init__.py"), _read_file(launcher, "launcher")),
        (
            _generated(f"{package}/bin/nixlbench", _EXECUTABLE_FILE),
            _read_file(binary, "nixlbench binary"),
        ),
        (
            _generated(f"{dist_info}/METADATA"),
            ("\n".join(metadata) + "\n").encode("utf-8"),
        ),
        (
            _generated(f"{dist_info}/WHEEL"),
            _wheel_file("py3", "none", platform).encode("utf-8"),
        ),
        (_generated(f"{dist_info}/entry_points.txt"), _entry_points(package)),
        (_generated(f"{dist_info}/top_level.txt"), f"{package}\n".encode("utf-8")),
    ]
    entries += [
        (_generated(f"{dist_info}/licenses/{relative}"), data)
        for relative, data in licenses.items()
    ]

    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(
        output_dir, f"{package}-{nixl.version}-py3-none-{platform}.whl"
    )
    _write_wheel(out_path, dist_info, entries)
    print(f"wrote {out_path} (requires {nixl.name}=={nixl.version})")
    return out_path


def missing_symbols(needed: set[str], exported: set[str]) -> list[str]:
    """Symbols nixlbench needs that a set of libraries does not export."""
    return sorted(needed - exported)


def _run(tool: str, *args: str) -> str:
    try:
        result = subprocess.run(
            [tool, *args], capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise WheelError(f"cannot run {tool}: {e}") from e
    if result.returncode != 0:
        raise WheelError(f"{tool} {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def _dynamic_symbols(path: str, which: str) -> set[str]:
    """Dynamic symbol names of an ELF file, without their @version suffix."""
    names = set()
    for line in _run("nm", "-D", "-P", which, path).splitlines():
        if line.strip():
            names.add(line.split()[0].split("@", 1)[0])
    return names


def _exported(lib_dir: str) -> set[str]:
    exported: set[str] = set()
    for lib in NIXL_LIBRARIES:
        exported |= _dynamic_symbols(os.path.join(lib_dir, lib), "--defined-only")
    return exported


def _dynamic_section(path: str) -> dict[str, list[str]]:
    """The NEEDED, RPATH and RUNPATH values of an ELF file."""
    tags: dict[str, list[str]] = {"NEEDED": [], "RPATH": [], "RUNPATH": []}
    for line in _run("readelf", "-d", "-W", path).splitlines():
        match = re.search(r"\((NEEDED|RPATH|RUNPATH)\).*\[(.*)\]", line)
        if match:
            tags[match.group(1)].append(match.group(2))
    return tags


def _extract(whl: zipfile.ZipFile, entry: str, directory: str) -> str:
    path = os.path.join(directory, os.path.basename(entry))
    with open(path, "wb") as f:
        f.write(whl.read(entry))
    return path


def _check_elf(
    whl: zipfile.ZipFile,
    package: str,
    nixl_package: str,
    nixl_libdir: str,
    nixl_wheel_dir: str | None,
) -> list[str]:
    errors = []
    with tempfile.TemporaryDirectory() as tmp:
        binary = _extract(whl, f"{package}/bin/nixlbench", tmp)
        dynamic = _dynamic_section(binary)
        for lib in NIXL_LIBRARIES:
            if lib not in dynamic["NEEDED"]:
                errors.append(
                    f"nixlbench does not link {lib} by that name (DT_NEEDED: "
                    f"{', '.join(dynamic['NEEDED'])})"
                )
        rpath = (
            f"$ORIGIN/../../.{nixl_package}.mesonpy.libs:$ORIGIN/../../{package}.libs"
        )
        if dynamic["RUNPATH"] or dynamic["RPATH"] != [rpath]:
            errors.append(
                f"nixlbench must have DT_RPATH {rpath} and no DT_RUNPATH, found "
                f"DT_RPATH {dynamic['RPATH'] or 'none'}, "
                f"DT_RUNPATH {dynamic['RUNPATH'] or 'none'}"
            )

        needed = _dynamic_symbols(binary, "--undefined-only") & _exported(nixl_libdir)
        if not needed:
            errors.append(f"nixlbench uses no symbol of the NIXL in {nixl_libdir}")
        print(f"nixlbench needs {len(needed)} symbols of {', '.join(NIXL_LIBRARIES)}")
        if nixl_wheel_dir is None:
            return errors

        nixl_libs = f".{nixl_package}.mesonpy.libs"
        wheels = sorted(
            entry
            for entry in os.listdir(nixl_wheel_dir)
            if entry.startswith(f"{nixl_package}-") and entry.endswith(".whl")
        )
        if not wheels:
            errors.append(f"no {nixl_package} wheel in {nixl_wheel_dir}")
        for wheel in wheels:
            lib_dir = os.path.join(tmp, wheel)
            os.mkdir(lib_dir)
            with zipfile.ZipFile(os.path.join(nixl_wheel_dir, wheel)) as nixl_whl:
                absent = [
                    lib
                    for lib in NIXL_LIBRARIES
                    if f"{nixl_libs}/{lib}" not in nixl_whl.namelist()
                ]
                if absent:
                    errors.append(f"{wheel}: no {nixl_libs}/{', '.join(absent)}")
                    continue
                for lib in NIXL_LIBRARIES:
                    _extract(nixl_whl, f"{nixl_libs}/{lib}", lib_dir)
            missing = missing_symbols(needed, _exported(lib_dir))
            if missing:
                errors.append(
                    f"{wheel} does not export {len(missing)} symbols nixlbench "
                    f"needs: {', '.join(missing)}"
                )
            else:
                print(f"{wheel}: exports every symbol nixlbench needs")
    return errors


def check(
    path: str,
    allow_vendored: list[str],
    nixl_libdir: str | None = None,
    nixl_wheel_dir: str | None = None,
) -> list[str]:
    """Return every violation found in a repaired nixlbench backend wheel."""
    errors = []
    with zipfile.ZipFile(path) as whl:
        dist_info, headers = read_metadata(path, whl)
        infos = {info.filename: info for info in whl.infolist() if not info.is_dir()}
        dist_name = _single(os.path.basename(path), headers, "Name")
        version = _single(os.path.basename(path), headers, "Version")
        package = _package(dist_name)
        if not package.startswith("nixlbench_"):
            raise WheelError(
                f"{os.path.basename(path)}: not a nixlbench backend wheel: "
                f"METADATA Name is {dist_name}"
            )
        nixl_package = "nixl" + package[len("nixlbench") :]

        binary = f"{package}/bin/nixlbench"
        binaries = sorted(n for n in infos if n.startswith(f"{package}/bin/"))
        if binaries != [binary]:
            errors.append(
                f"expected exactly {binary} under {package}/bin/, found "
                f"{', '.join(binaries) or 'nothing'}"
            )
        elif (infos[binary].external_attr >> 16) & 0o7777 != 0o755:
            errors.append(
                f"{binary} has mode "
                f"{(infos[binary].external_attr >> 16) & 0o7777:o}, expected 755"
            )

        libs = f"{package}.libs/"
        for name in sorted(infos):
            top = name.split("/", 1)[0]
            if name.startswith(libs):
                if not os.path.basename(name).startswith(tuple(allow_vendored)):
                    errors.append(
                        f"{name} is vendored but not allowed (allowed: "
                        f"{', '.join(allow_vendored) or 'none'})"
                    )
            elif top not in (package, dist_info):
                errors.append(f"unexpected file {name}")

        license_dir = f"{dist_info}/licenses/"
        for license_file in _field(headers, "License-File"):
            if f"{license_dir}{license_file}" not in infos:
                errors.append(f"license file {license_file} is missing")
        if not _field(headers, "License-Expression"):
            errors.append("no License-Expression")

        expected = f"nixl{dist_name[len('nixlbench') :]}=={version}"
        requirements = _field(headers, "Requires-Dist")
        if len(requirements) != 1 or not _same_requirement(requirements[0], expected):
            errors.append(
                f"expected the single requirement {expected}, found "
                f"{', '.join(requirements) or 'none'}"
            )

        if nixl_libdir is not None and binaries == [binary]:
            errors += _check_elf(
                whl, package, nixl_package, nixl_libdir, nixl_wheel_dir
            )
    return errors


def parse_backend(path: str) -> Backend:
    """Describe a ROCm nixlbench backend wheel from its file name."""
    name = os.path.basename(path)
    match = _WHEEL_NAME_RE.fullmatch(name)
    rocm = _ROCM_BACKEND_RE.fullmatch(match["prefix"]) if match else None
    if match is None or rocm is None:
        raise WheelError(
            f"{name}: not a nixlbench_rocm<X>_<Y> backend wheel (expected "
            "nixlbench_rocm<X>_<Y>-<version>-<python>-<abi>-<platform>.whl)"
        )
    return Backend(
        path=path,
        rocm=(int(rocm["major"]), int(rocm["minor"])),
        version=match["version"],
        python=match["python"],
        abi=match["abi"],
        platform=match["platform"],
    )


def check_backends(backends: list[Backend]) -> None:
    """Refuse backends that cannot share one wheel."""
    by_rocm: dict[tuple[int, int], Backend] = {}
    for backend in backends:
        other = by_rocm.setdefault(backend.rocm, backend)
        if other is not backend:
            raise WheelError(
                "duplicate backend for ROCm {}.{}: {} and {}".format(
                    *backend.rocm,
                    os.path.basename(other.path),
                    os.path.basename(backend.path),
                )
            )
    first = backends[0]
    for backend in backends[1:]:
        for what, mine, theirs in (
            ("version", backend.version, first.version),
            (
                "Python/ABI tag",
                f"{backend.python}-{backend.abi}",
                f"{first.python}-{first.abi}",
            ),
            ("platform tag", backend.platform, first.platform),
        ):
            if mine != theirs:
                raise WheelError(
                    f"backends disagree on {what}: "
                    f"{os.path.basename(first.path)} has {theirs}, "
                    f"{os.path.basename(backend.path)} has {mine}"
                )


def _backend_headers(
    backend: Backend, whl: zipfile.ZipFile
) -> tuple[str, list[tuple[str, str]]]:
    name = os.path.basename(backend.path)
    dist_info, headers = read_metadata(backend.path, whl)
    if _package(_single(name, headers, "Name")) != backend.package:
        raise WheelError(
            f"{name}: METADATA Name {_single(name, headers, 'Name')} does not "
            f"match the file name ({backend.package})"
        )
    if _single(name, headers, "Version") != backend.version:
        raise WheelError(
            f"{name}: METADATA Version {_single(name, headers, 'Version')} does "
            f"not match the file name ({backend.version})"
        )
    requirements = _field(headers, "Requires-Dist")
    if len(requirements) != 1 or not _same_requirement(
        requirements[0], backend.nixl_requirement
    ):
        raise WheelError(
            f"{name}: expected the single requirement {backend.nixl_requirement}, "
            f"found {', '.join(requirements) or 'none'}"
        )
    return dist_info, headers


def merge(
    backend_wheels: list[str],
    output_dir: str,
    dispatcher: str,
    name: str = "rocm-nixlbench",
    requires_dist: str = "rocm-nixl",
) -> str:
    """Merge ROCm nixlbench backend wheels into one wheel; return its path."""
    backends = sorted(
        (parse_backend(path) for path in backend_wheels), key=lambda b: b.rocm
    )
    if not backends:
        raise WheelError("no backend wheels given")
    check_backends(backends)
    first = backends[0]
    prefix = _package(name)
    dist_info = f"{prefix}-{first.version}.dist-info"

    generated = {"nixlbench/__init__.py": _read_file(dispatcher, "dispatcher")}
    # Wheel path -> where it comes from, to report collisions.
    owners = {path: "the nixlbench dispatcher" for path in generated}

    wheels = [zipfile.ZipFile(backend.path, "r") for backend in backends]
    try:
        first_headers: list[tuple[str, str]] = []
        licenses: dict[str, bytes] = {}
        copies: list[tuple[zipfile.ZipFile, zipfile.ZipInfo]] = []
        for backend, whl in zip(backends, wheels):
            wheel_name = os.path.basename(backend.path)
            backend_dist_info, headers = _backend_headers(backend, whl)
            first_headers = first_headers or headers
            for field in ("Requires-Python", "License-Expression", "License-File"):
                if _field(headers, field) != _field(first_headers, field):
                    raise WheelError(
                        f"backends disagree on {field}: "
                        f"{os.path.basename(first.path)} has "
                        f"{_field(first_headers, field) or 'none'}, "
                        f"{wheel_name} has {_field(headers, field) or 'none'}"
                    )
            for license_file in _field(headers, "License-File"):
                entry = f"{backend_dist_info}/licenses/{license_file}"
                try:
                    data = whl.read(entry)
                except KeyError:
                    raise WheelError(
                        f"{wheel_name}: license file {license_file} is missing"
                    ) from None
                if licenses.setdefault(license_file, data) != data:
                    raise WheelError(
                        f"{wheel_name}: license file {license_file} differs from "
                        f"{os.path.basename(first.path)}"
                    )
            for info in whl.infolist():
                # Of a backend's dist-info only the licenses are kept: its SBOM
                # describes the backend wheel, not the merged one.
                if info.is_dir() or info.filename.startswith(f"{backend_dist_info}/"):
                    continue
                owner = owners.setdefault(info.filename, wheel_name)
                if owner != wheel_name:
                    raise WheelError(
                        f"path collision: {info.filename} is in both {owner} "
                        f"and {wheel_name}"
                    )
                copies.append((whl, info))

        metadata = []
        for field, value in first_headers:
            if field.lower() == "requires-dist":
                continue
            if field.lower() == "name":
                value = name
            elif field.lower() == "summary":
                value = f"{SUMMARY} for {requires_dist}"
            metadata.append(f"{field}: {value}")
        metadata.append(f"Requires-Dist: {requires_dist}=={first.version}")

        generated.update(
            {f"{dist_info}/licenses/{rel}": data for rel, data in licenses.items()}
        )
        generated[f"{dist_info}/METADATA"] = ("\n".join(metadata) + "\n").encode()
        generated[f"{dist_info}/WHEEL"] = _wheel_file(*first.tags).encode("utf-8")
        generated[f"{dist_info}/entry_points.txt"] = _entry_points("nixlbench")
        top_level = ["nixlbench"] + [backend.package for backend in backends]
        generated[f"{dist_info}/top_level.txt"] = (
            "\n".join(sorted(top_level)) + "\n"
        ).encode("utf-8")

        entries = [(info, whl.read(info)) for whl, info in copies]
        entries += [(_generated(path), data) for path, data in generated.items()]
        os.makedirs(output_dir, exist_ok=True)
        out_path = os.path.join(
            output_dir, f"{prefix}-{first.version}-{'-'.join(first.tags)}.whl"
        )
        _write_wheel(out_path, dist_info, entries)
    finally:
        for whl in wheels:
            whl.close()

    print(f"wrote {out_path} (requires {requires_dist}=={first.version})")
    print("backends included:")
    for backend in backends:
        print(
            "  ROCm {}.{}: {} ({})".format(
                *backend.rocm, backend.package, os.path.basename(backend.path)
            )
        )
    return out_path


def _license_file(value: str) -> tuple[str, str]:
    relative, sep, source = value.partition("=")
    if not sep or not relative or not source:
        raise argparse.ArgumentTypeError(
            f"expected <path in the wheel>=<source file>, got {value!r}"
        )
    return relative, source


def main() -> int:
    wheel_dir = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "benchmark",
        "nixlbench",
        "wheel",
    )
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)

    build_parser = commands.add_parser(
        "build", help="assemble the unrepaired nixlbench wheel for one NIXL wheel"
    )
    build_parser.add_argument("--binary", required=True, help="the nixlbench binary")
    build_parser.add_argument(
        "--nixl-wheel",
        required=True,
        help="a NIXL backend wheel of the build nixlbench was linked against; "
        "gives the name, version and Requires-Python",
    )
    build_parser.add_argument(
        "--platform", required=True, help="platform tag, e.g. linux_x86_64"
    )
    build_parser.add_argument(
        "--output-dir", required=True, help="directory to write the wheel to"
    )
    build_parser.add_argument(
        "--launcher",
        default=os.path.join(wheel_dir, "launcher.py"),
        help="installed as the package's __init__.py (default: %(default)s)",
    )
    build_parser.add_argument(
        "--license-expression",
        required=True,
        help="SPDX expression covering nixlbench and what the wheel vendors",
    )
    build_parser.add_argument(
        "--license-file",
        action="append",
        default=[],
        type=_license_file,
        metavar="PATH=SOURCE",
        help="license file to add as dist-info/licenses/PATH (repeatable)",
    )

    check_parser = commands.add_parser(
        "check", help="verify repaired nixlbench backend wheels"
    )
    check_parser.add_argument(
        "--allow-vendored",
        action="append",
        default=[],
        metavar="PREFIX",
        help="file name prefix of a library the wheel may vendor (repeatable)",
    )
    check_parser.add_argument(
        "--nixl-libdir",
        help="library directory of the NIXL install nixlbench was linked "
        "against; enables the ELF and symbol checks (needs readelf and nm)",
    )
    check_parser.add_argument(
        "--nixl-wheel-dir",
        help="directory of NIXL backend wheels that must each export every "
        "NIXL symbol nixlbench uses (requires --nixl-libdir)",
    )
    check_parser.add_argument("wheels", nargs="+", metavar="WHEEL")

    merge_parser = commands.add_parser(
        "merge", help="merge nixlbench_rocm<X>_<Y> wheels into one wheel"
    )
    merge_parser.add_argument(
        "--output-dir", required=True, help="directory to write the merged wheel to"
    )
    merge_parser.add_argument(
        "--name",
        default="rocm-nixlbench",
        help="distribution name of the merged wheel (default: %(default)s)",
    )
    merge_parser.add_argument(
        "--requires-dist",
        default="rocm-nixl",
        help="distribution the merged wheel requires, at the backends' version "
        "(default: %(default)s)",
    )
    merge_parser.add_argument(
        "--dispatcher",
        default=os.path.join(wheel_dir, "dispatcher.py"),
        help="installed as nixlbench/__init__.py (default: %(default)s)",
    )
    merge_parser.add_argument(
        "backend_wheels",
        nargs="+",
        metavar="BACKEND_WHEEL",
        help="nixlbench_rocm<X>_<Y> backend wheels of one NIXL version",
    )
    args = parser.parse_args()

    try:
        if args.command == "build":
            build(
                args.binary,
                args.nixl_wheel,
                args.platform,
                args.output_dir,
                args.launcher,
                args.license_expression,
                args.license_file,
            )
        elif args.command == "check":
            if args.nixl_wheel_dir and not args.nixl_libdir:
                parser.error("--nixl-wheel-dir requires --nixl-libdir")
            failed = False
            for wheel in args.wheels:
                errors = check(
                    wheel, args.allow_vendored, args.nixl_libdir, args.nixl_wheel_dir
                )
                for error in errors:
                    print(f"ERROR: {os.path.basename(wheel)}: {error}", file=sys.stderr)
                if not errors:
                    print(f"{os.path.basename(wheel)}: OK")
                failed = failed or bool(errors)
            return 1 if failed else 0
        else:
            merge(
                args.backend_wheels,
                args.output_dir,
                args.dispatcher,
                args.name,
                args.requires_dist,
            )
    except (WheelError, OSError, zipfile.BadZipFile) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
