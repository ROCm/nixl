#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build NIXL from $NIXL_SRC for AMD ROCm.  This is a ROCm build, not CUDA: no
# CUDA toolkit is present in the image, so meson's cuda_dep never resolves, the
# CUDA GDS backend is disabled explicitly, and the wheel/bindings variant is
# forced to `rocm` (-> nixl_rocm) instead of the autodetected nixl_cu12.
#
# The checkout arrives already patched with everything in patches/nixl/ (applied
# by clone-src.sh in the Dockerfile), so any ROCm/HIP functionality those
# patches add is simply present in $NIXL_SRC by the time this runs.
set -euo pipefail

NIXL_SRC="${NIXL_SRC:-/tmp/nixl}"
UCX_PREFIX="${UCX_PREFIX:-/opt/rocnixl-ucx}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
NIXL_INSTALL_PREFIX="${NIXL_INSTALL_PREFIX:-/opt/nixl}"
# Plugins with a hard CUDA/vendor-SDK dependency, or that pull large optional
# deps we do not need for NIXL<->MORI testing.  Override to widen the build.
NIXL_DISABLE_PLUGINS="${NIXL_DISABLE_PLUGINS:-GDS,GDS_MT,GPUNETIO,OBJ,AZURE_BLOB,INFINIA}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

if [[ ! -f "${NIXL_SRC}/meson.build" ]]; then
	echo "ERROR: ${NIXL_SRC}/meson.build not found" >&2
	exit 1
fi

# wheel_variant is what lets us name the ROCm bindings nixl_rocm; a ref without
# it predates ROCm support and would silently produce a nixl_cu12 tree.
if [[ ! -f "${NIXL_SRC}/meson_options.txt" ]] \
	|| ! grep -q "option('wheel_variant'" "${NIXL_SRC}/meson_options.txt"; then
	echo "ERROR: ${NIXL_SRC} lacks the wheel_variant meson option -- wrong NIXL_REF?" >&2
	exit 1
fi

export PKG_CONFIG_PATH="${UCX_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export LD_LIBRARY_PATH="${UCX_PREFIX}/lib:${LD_LIBRARY_PATH:-}"
export ROCM_PATH

cd "${NIXL_SRC}"
rm -rf build

MESON_ARGS=(
	"-Dwheel_variant=rocm"
	"-Ducx_path=${UCX_PREFIX}"
	"-Ddisable_gds_backend=true"
	"-Dbuild_tests=false"
	"-Dbuild_examples=false"
	"-Ddisable_plugins=${NIXL_DISABLE_PLUGINS}"
	"--prefix=${NIXL_INSTALL_PREFIX}"
)

# patches/nixl/ may add ROCm-specific meson options (e.g. a hipFile-backed
# storage backend).  Pass them only when the patched tree actually declares
# them, so an unpatched pristine tag still configures.
if grep -q "option('rocm_ais_path'" "${NIXL_SRC}/meson_options.txt"; then
	MESON_ARGS+=("-Drocm_ais_path=${AIS_PATH:-${ROCM_PATH}}")
fi

# Likewise for a MORI backend plugin: the patch that adds src/plugins/mori also
# adds a mori_path option, and the MORI C++ SDK is staged at MORI_INSTALL_PREFIX
# by the mori stage.  Unpatched, neither exists and nothing is passed.
MORI_INSTALL_PREFIX="${MORI_INSTALL_PREFIX:-/opt/mori}"
if grep -q "option('mori_path'" "${NIXL_SRC}/meson_options.txt"; then
	if [[ ! -f "${MORI_INSTALL_PREFIX}/include/mori/io/engine.hpp" ]]; then
		echo "ERROR: the NIXL patchset declares mori_path but no MORI SDK at ${MORI_INSTALL_PREFIX}" >&2
		exit 1
	fi
	MESON_ARGS+=("-Dmori_path=${MORI_INSTALL_PREFIX}")
	export PKG_CONFIG_PATH="${MORI_INSTALL_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH}"
	export LD_LIBRARY_PATH="${MORI_INSTALL_PREFIX}/lib:${LD_LIBRARY_PATH}"
	echo "[nixl] building with the MORI backend plugin (-Dmori_path=${MORI_INSTALL_PREFIX})"
fi

# NIXL_MESON_EXTRA_ARGS lets the Dockerfile/Makefile append one-off options
# without editing this script (word-split on purpose).
# shellcheck disable=SC2206
MESON_ARGS+=(${NIXL_MESON_EXTRA_ARGS:-})

meson setup build "${MESON_ARGS[@]}"
ninja -C build -j"${BUILD_JOBS}"
ninja -C build install
ldconfig

NIXL_PY_SITE="${NIXL_INSTALL_PREFIX}/lib/python3/dist-packages"

# Meson installs the bindings as nixl_rocm (because of -Dwheel_variant=rocm).
# Upstream's `nixl` meta-package on PyPI only probes CUDA backends, so instead
# of installing it we ship a shim that re-exports nixl_rocm under the `nixl`
# name -- consumers keep writing `import nixl`.
if [[ ! -d "${NIXL_PY_SITE}/nixl_rocm" ]]; then
	echo "ERROR: ${NIXL_PY_SITE}/nixl_rocm not found after meson install" >&2
	exit 1
fi

mkdir -p "${NIXL_PY_SITE}/nixl"
cat > "${NIXL_PY_SITE}/nixl/__init__.py" <<'PY'
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: MIT
"""ROCm shim: meson installs nixl_rocm; consumers import nixl."""

import importlib
import sys

_pkg = importlib.import_module("nixl_rocm")

for sub_name in ("_api", "_bindings", "_utils", "logging"):
    module = importlib.import_module(f"{_pkg.__name__}.{sub_name}")
    sys.modules[f"nixl.{sub_name}"] = module
    setattr(sys.modules[__name__], sub_name, module)
    for attr in dir(module):
        if not attr.startswith("_"):
            setattr(sys.modules[__name__], attr, getattr(module, attr))
PY

NIXL_PLUGIN_DIR="${NIXL_INSTALL_PREFIX}/lib/x86_64-linux-gnu/plugins"
[[ -d "${NIXL_PLUGIN_DIR}" ]] || NIXL_PLUGIN_DIR="${NIXL_INSTALL_PREFIX}/lib/nixl/plugins"
export NIXL_PLUGIN_DIR
export PYTHONPATH="${NIXL_PY_SITE}:${PYTHONPATH:-}"
export LD_LIBRARY_PATH="${NIXL_INSTALL_PREFIX}/lib/x86_64-linux-gnu:${NIXL_INSTALL_PREFIX}/lib:${UCX_PREFIX}/lib:${LD_LIBRARY_PATH}"

python3 -m pip uninstall -y nixl nixl-cu12 nixl-cu13 2>/dev/null || true

# No `import nixl` check in this stage.  nixl_rocm/__init__.py imports _api,
# which imports numpy AND torch at module scope, so the smallest thing that can
# be imported here still drags in the multi-GB ROCm torch wheel -- which
# belongs in the runtime stage, not in a build layer.  What this stage can
# verify is that meson produced the extension module and that it links; the
# real import check runs in the runtime stage, where torch exists.
_bindings="$(find "${NIXL_PY_SITE}/nixl_rocm" -maxdepth 1 -name '_bindings*.so' | head -1)"
if [[ -z "${_bindings}" ]]; then
	echo "ERROR: no _bindings*.so in ${NIXL_PY_SITE}/nixl_rocm after meson install" >&2
	exit 1
fi
if ldd "${_bindings}" | grep -q "not found"; then
	echo "ERROR: ${_bindings} has unresolved shared libraries:" >&2
	ldd "${_bindings}" | grep "not found" >&2
	exit 1
fi
echo "nixl_rocm bindings OK: ${_bindings}"

echo "Built plugins:"
find "${NIXL_PLUGIN_DIR}" -name 'libplugin_*.so' -printf '  %f\n' 2>/dev/null || true

# The UCX backend is the one plugin this image cannot be useful without.
if [[ ! -e "${NIXL_PLUGIN_DIR}/libplugin_UCX.so" ]]; then
	echo "ERROR: libplugin_UCX.so not built -- NIXL did not find UCX at ${UCX_PREFIX}" >&2
	exit 1
fi

echo "PASS: NIXL (ROCm variant) built -> ${NIXL_INSTALL_PREFIX}"
