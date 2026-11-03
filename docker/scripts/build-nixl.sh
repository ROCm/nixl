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

# The AWS SDK, so meson finds aws-cpp-sdk-s3, -s3-crt and -core. Without
# these three src/utils/object skips obj_utils and src/plugins/obj then calls
# subdir_done(), so EVERY object engine disappears -- with one warning in the
# meson log and no error anywhere. Harmless when the prefix is empty.
AWS_SDK_PREFIX="${AWS_SDK_PREFIX:-/opt/aws-sdk}"
if [[ -d "${AWS_SDK_PREFIX}/lib/pkgconfig" ]]; then
	export PKG_CONFIG_PATH="${AWS_SDK_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH}"
	export LD_LIBRARY_PATH="${AWS_SDK_PREFIX}/lib:${LD_LIBRARY_PATH:-}"
	# CPLUS_INCLUDE_PATH is not belt-and-braces, it is the only thing that
	# puts the SDK headers on the compile line. src/utils/object/meson.build
	# narrows all three dependencies with
	#   partial_dependency(compile_args: false, includes: true, ...)
	# and for a pkg-config dependency meson keeps the -I flags from Cflags in
	# compile_args -- `includes` only carries include_directories() objects,
	# of which there are none here. So compile_args: false discards every -I
	# and the plugin compiles with no way to find <aws/s3/S3Client.h>.
	# Upstream never notices because its CI installs the SDK into a default
	# prefix where the compiler looks anyway; ours lives in /opt beside every
	# other component, which is the convention worth keeping.
	export CPLUS_INCLUDE_PATH="${AWS_SDK_PREFIX}/include:${CPLUS_INCLUDE_PATH:-}"
	echo "[nixl] AWS SDK at ${AWS_SDK_PREFIX} -- object engines can build"
else
	echo "[nixl] no AWS SDK at ${AWS_SDK_PREFIX} -- NIXL will skip the OBJ plugin"
fi
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
#
# Prefer the source-built hipFile staged at HIPFILE_PREFIX by the hipfile stage
# over the one the ROCm base image packages: the AIS plugin needs the AMD async
# backend, which the packaged copy predates.  The directory is always present
# but is empty when HIPFILE_REF was unset, hence the test on the library rather
# than on the directory.
HIPFILE_PREFIX="${HIPFILE_PREFIX:-/opt/hipfile}"
if grep -q "option('rocm_ais_path'" "${NIXL_SRC}/meson_options.txt"; then
	if [[ -z "${AIS_PATH:-}" ]]; then
		if [[ -e "${HIPFILE_PREFIX}/lib/libhipfile.so" ]]; then
			AIS_PATH="${HIPFILE_PREFIX}"
		else
			AIS_PATH="${ROCM_PATH}"
		fi
	fi
	MESON_ARGS+=("-Drocm_ais_path=${AIS_PATH}")
	export LD_LIBRARY_PATH="${AIS_PATH}/lib:${LD_LIBRARY_PATH}"
	echo "[nixl] hipFile for the AIS backends: ${AIS_PATH}"
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

# The OBJ plugin's hipobj engine -- S3 over RDMA on ROCm.  Same opt-in shape as
# mori_path: the patchset adds the option, the hipobject stage stages the
# library, and an unpatched tree gets nothing.  Note this is an ENGINE inside
# the OBJ plugin, not a plugin of its own: without the AWS SDK there is no OBJ
# plugin for it to live in, which is what AWS_SDK_PREFIX above provides.
HIPOBJECT_PREFIX="${HIPOBJECT_PREFIX:-/opt/hipobject}"
if grep -q "option('hipobj_path'" "${NIXL_SRC}/meson_options.txt"; then
	if [[ ! -f "${HIPOBJECT_PREFIX}/include/hipobj/hipobj.h" ]]; then
		echo "ERROR: the NIXL patchset declares hipobj_path but no hipObject at ${HIPOBJECT_PREFIX}" >&2
		exit 1
	fi
	MESON_ARGS+=("-Dhipobj_path=${HIPOBJECT_PREFIX}")
	export LD_LIBRARY_PATH="${HIPOBJECT_PREFIX}/lib:${LD_LIBRARY_PATH}"
	echo "[nixl] building with the hipobj S3-over-RDMA engine (-Dhipobj_path=${HIPOBJECT_PREFIX})"
fi

# The LIBFABRIC backend.  Two options have to line up for it to build at all:
#
#   -Dlibfabric_path  where to find libfabric itself.  Without it meson falls
#                     back to `dependency('libfabric', required: false)`, and
#                     that does NOT come up empty: the base image carries the
#                     distro libfabric1 (1.17 on Ubuntu 24.04), pulled in as a
#                     transitive dependency of libopenmpi3t64.  So omitting
#                     this does not disable the plugin -- it silently builds
#                     it against a libfabric below the plugin's own 1.21.0
#                     floor.  Explicit beats implicit here.
#   -Drocm_path       requests ROCm/HIP support, new in NIXL v1.5.0.  The
#                     plugin's GPU paths are written against CUDA; with this
#                     set, meson runs them through hipify-perl and compiles
#                     them against HIP.  Without it the plugin still builds,
#                     but host-memory only -- VRAM transfers are not available,
#                     which defeats the point of testing it here.
#
# rocm_path is scoped to the libfabric subdirs upstream (src/utils/libfabric,
# src/plugins/libfabric, test/unit/utils), so passing it does not change how any
# other backend is built.  Guarded on the option existing so an older NIXL_REF
# still configures -- v1.4.1 and earlier have libfabric_path but no rocm_path,
# and would build a CUDA-only plugin that then fails to link.
LIBFABRIC_PREFIX="${LIBFABRIC_PREFIX:-/opt/libfabric}"
if [[ -e "${LIBFABRIC_PREFIX}/lib/libfabric.so" ]]; then
	if grep -q "option('rocm_path'" "${NIXL_SRC}/meson_options.txt"; then
		MESON_ARGS+=("-Dlibfabric_path=${LIBFABRIC_PREFIX}" "-Drocm_path=${ROCM_PATH}")
		WANT_LIBFABRIC=1
		export PKG_CONFIG_PATH="${LIBFABRIC_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH}"
		export LD_LIBRARY_PATH="${LIBFABRIC_PREFIX}/lib:${LD_LIBRARY_PATH}"
		echo "[nixl] building the LIBFABRIC backend with HIP support" \
			"(-Dlibfabric_path=${LIBFABRIC_PREFIX} -Drocm_path=${ROCM_PATH})"
	else
		echo "[nixl] libfabric present at ${LIBFABRIC_PREFIX} but ${NIXL_SRC} has no rocm_path" \
			"option (NIXL_REF predates v1.5.0) -- skipping the LIBFABRIC plugin" >&2
	fi
else
	echo "[nixl] no libfabric at ${LIBFABRIC_PREFIX} -- skipping the LIBFABRIC plugin"
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

# meson drops an optional plugin whose dependency does not resolve without
# failing the build, so "we asked for LIBFABRIC" and "we got LIBFABRIC" are
# different claims.  Assert the second one, or the image ships a backend that
# only fails at nixlbench --backend=LIBFABRIC time.
if [[ "${WANT_LIBFABRIC:-0}" == "1" && ! -e "${NIXL_PLUGIN_DIR}/libplugin_LIBFABRIC.so" ]]; then
	echo "ERROR: libplugin_LIBFABRIC.so not built despite -Dlibfabric_path=${LIBFABRIC_PREFIX}" >&2
	echo "Check meson's libfabric/hwloc/numa dependency resolution in the configure log." >&2
	exit 1
fi

echo "PASS: NIXL (ROCm variant) built -> ${NIXL_INSTALL_PREFIX}"
