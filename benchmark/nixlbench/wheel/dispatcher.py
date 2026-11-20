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

"""nixlbench command of the rocm-nixlbench wheel.

Runs the nixlbench_rocm<X>_<Y> backend built for the ROCm install found on
disk. nixlbench loads the HIP runtime of that install, so the installed PyTorch
is not consulted.
"""

from __future__ import annotations

import importlib
import pkgutil
import re
import sys


def find_backends() -> dict[tuple[int, int], str]:
    """Installed nixlbench_rocm<X>_<Y> packages, keyed by ROCm (major, minor)."""
    found = {}
    for module in pkgutil.iter_modules():
        match = re.fullmatch(r"nixlbench_rocm(\d+)_(\d+)", module.name)
        if match:
            found[(int(match.group(1)), int(match.group(2)))] = module.name
    return found


def _warn(message: str) -> None:
    # .ci/scripts/check_prints.sh rejects the print builtin under benchmark/.
    sys.stderr.write(f"nixlbench: {message}\n")


def main() -> None:
    backends = find_backends()
    if not backends:
        _warn("no nixlbench_rocm<X>_<Y> backend is installed")
        sys.exit(1)
    newest = max(backends)
    included = ", ".join("{}.{}".format(*version) for version in sorted(backends))

    backend = importlib.import_module(backends[newest])
    rocm = backend.find_rocm()
    if rocm is None:
        _warn(
            f"{backend.no_rocm_text()}; using the backend for the newest ROCm "
            "version ({}.{}) and relying on the dynamic loader".format(*newest)
        )
    elif rocm.version not in backends:
        _warn(
            "found ROCm {}.{} at {} but this rocm-nixlbench has no backend for "
            "it (backends included: {})".format(*rocm.version, rocm.root, included)
        )
        sys.exit(1)
    elif rocm.version != newest:
        backend = importlib.import_module(backends[rocm.version])
    # The same detection result selects the backend and its HIP runtime.
    backend.run(rocm)
