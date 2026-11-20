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

"""Launcher of the nixlbench binary shipped in a nixlbench backend wheel.

Installed as the __init__.py of nixlbench_<variant> (nixlbench_rocm10_1,
nixlbench_cu13, ...). It runs <package>/bin/nixlbench against the libnixl of the
matching NIXL backend package nixl_<variant>, and, for a ROCm variant, with the
ROCm install found on disk on the loader path.
"""

from __future__ import annotations

import glob
import importlib.util
import os
import re
import signal
import sys
from importlib import metadata
from typing import NamedTuple, NoReturn

NIXL_PACKAGE = "nixl" + __name__.removeprefix("nixlbench")
BINARY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "nixlbench")
_DEFAULT_ROCM_ROOT = "/opt/rocm"
_ROCM_PACKAGE = re.fullmatch(r"nixlbench_rocm(\d+)_(\d+)", __name__)
_NIXL_LIBS_DIR = f".{NIXL_PACKAGE}.mesonpy.libs"
_PINNED = re.compile(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;,]+)\s*")


class RocmInstall(NamedTuple):
    root: str
    version: tuple[int, int]

    @property
    def lib_dir(self) -> str:
        return os.path.join(self.root, "lib")


def _warn(message: str) -> None:
    # .ci/scripts/check_prints.sh rejects the print builtin under benchmark/.
    sys.stderr.write(f"nixlbench: {message}\n")


def _fail(message: str) -> NoReturn:
    _warn(message)
    sys.exit(1)


def rocm_search_root() -> str:
    """Where find_rocm() starts: $ROCM_PATH, or the default install root."""
    return os.environ.get("ROCM_PATH") or _DEFAULT_ROCM_ROOT


def no_rocm_text() -> str:
    """Where find_rocm() looked, for the warning that it found nothing."""
    hint = "" if os.environ.get("ROCM_PATH") else " (set ROCM_PATH)"
    return f"no ROCm install found under {rocm_search_root()}{hint}"


def _rocm_install(root: str) -> RocmInstall | None:
    try:
        with open(os.path.join(root, ".info", "version"), encoding="utf-8") as f:
            match = re.match(r"\s*(\d+)\.(\d+)", f.read())
    except (OSError, UnicodeDecodeError):
        return None
    if match is None or not glob.glob(os.path.join(root, "lib", "libamdhip64.so*")):
        return None
    return RocmInstall(root, (int(match.group(1)), int(match.group(2))))


def find_rocm() -> RocmInstall | None:
    """The ROCm install nixlbench should load its HIP runtime from."""
    root = rocm_search_root()
    candidates = [root, os.path.join(root, "core")]
    # The component packages populate only <root>/core-<X.Y>. Several versions
    # side by side are ambiguous.
    versioned = glob.glob(os.path.join(root, "core-*"))
    if len(versioned) == 1:
        candidates.append(versioned[0])
    for candidate in candidates:
        install = _rocm_install(candidate)
        if install is not None:
            return install
    return None


def _nixl_libs() -> tuple[str, bool]:
    """The NIXL wheel's library directory, and whether the binary's RPATH finds it.

    The RPATH is relative to the binary's real location, which is not inside
    site-packages when an installer links files in from a cache.
    """
    real = os.path.realpath(BINARY)
    sibling = os.path.normpath(
        os.path.join(os.path.dirname(real), "..", "..", _NIXL_LIBS_DIR)
    )
    if os.path.isfile(os.path.join(sibling, "libnixl.so")):
        return sibling, True
    try:
        spec = importlib.util.find_spec(NIXL_PACKAGE)
    except (ImportError, ValueError):
        spec = None
    for location in (spec.submodule_search_locations or []) if spec else []:
        libs = os.path.join(os.path.dirname(os.path.abspath(location)), _NIXL_LIBS_DIR)
        if os.path.isfile(os.path.join(libs, "libnixl.so")):
            return libs, False
    _fail(
        f"{NIXL_PACKAGE} is not installed next to this nixlbench; "
        "install the matching NIXL wheel"
    )


def _own_distribution() -> metadata.Distribution | None:
    """The installed wheel this package is part of: its own or a merged one."""
    site = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for dist in metadata.distributions(path=[site]):
        if __name__ in (dist.read_text("top_level.txt") or "").split():
            return dist
    return None


def _check_nixl_version() -> None:
    """Refuse a NIXL wheel of another version than the one this wheel requires.

    libnixl has no versioned ABI and the NIXL package name is the same for
    every version, so only the installer enforces the pin, and not always
    (pip install --no-deps, a later upgrade of the NIXL wheel alone).
    """
    own = _own_distribution()
    if own is None:
        return
    for requirement in own.requires or []:
        pin = _PINNED.fullmatch(requirement)
        if pin is None:
            continue
        # A merged wheel requires the merged NIXL wheel, but runs as well on
        # the NIXL backend wheel of its version.
        for name in (pin.group(1), NIXL_PACKAGE):
            try:
                installed = metadata.version(name)
            except metadata.PackageNotFoundError:
                continue
            if not installed:
                continue
            if installed != pin.group(2):
                _fail(
                    f"{own.metadata['Name']} {own.version} requires "
                    f"{requirement.strip()}, but {name} {installed} is installed"
                )
            return


def run(rocm: RocmInstall | None) -> NoReturn:
    """Replace this process with nixlbench, passing the command line through."""
    nixl_libs, by_rpath = _nixl_libs()
    _check_nixl_version()
    fallback = None if by_rpath else nixl_libs
    paths = [
        fallback,
        rocm.lib_dir if rocm is not None else None,
        *os.environ.get("LD_LIBRARY_PATH", "").split(os.pathsep),
    ]
    env = dict(os.environ)
    library_path = os.pathsep.join(path for path in paths if path)
    if library_path:
        env["LD_LIBRARY_PATH"] = library_path

    # libnixl takes its plugins from NIXL_PLUGIN_DIR before its own directory.
    plugin_dir = os.environ.get("NIXL_PLUGIN_DIR")
    wheel_plugins = os.path.join(nixl_libs, "plugins")
    if plugin_dir and os.path.realpath(plugin_dir) != os.path.realpath(wheel_plugins):
        _warn(
            f"NIXL_PLUGIN_DIR is set: NIXL loads its plugins from {plugin_dir}, "
            f"not from the NIXL wheel ({wheel_plugins})"
        )

    # CPython ignores both, and an ignored signal stays ignored across execve.
    for name in ("SIGPIPE", "SIGXFSZ"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), signal.SIG_DFL)

    if os.environ.get("NIXLBENCH_WHEEL_DEBUG"):
        libs_text = f"LD_LIBRARY_PATH fallback {fallback}" if fallback else "RPATH"
        if rocm is None:
            rocm_text = "not detected"
        else:
            rocm_text = "version {}.{}, root {}, libraries {}".format(
                *rocm.version, rocm.root, rocm.lib_dir
            )
        for line in (
            f"package: {__name__}",
            f"binary: {BINARY}",
            f"binary realpath: {os.path.realpath(BINARY)}",
            f"NIXL libraries: {libs_text}",
            f"NIXL plugins: {plugin_dir or wheel_plugins}",
            f"ROCm: {rocm_text}",
            f"LD_LIBRARY_PATH: {library_path}",
        ):
            _warn(line)
        sys.stderr.flush()

    os.execve(BINARY, [BINARY, *sys.argv[1:]], env)


def main() -> NoReturn:
    rocm = None
    if _ROCM_PACKAGE is not None:
        rocm = find_rocm()
        built_for = (int(_ROCM_PACKAGE.group(1)), int(_ROCM_PACKAGE.group(2)))
        if rocm is None:
            _warn(f"{no_rocm_text()}; relying on the dynamic loader")
        elif rocm.version != built_for:
            _warn(
                "found ROCm {}.{} at {} but this nixlbench was built for ROCm "
                "{}.{}".format(*rocm.version, rocm.root, *built_for)
            )
    run(rocm)
