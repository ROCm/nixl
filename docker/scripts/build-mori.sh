#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build the MORI (ROCm/mori) wheel from $MORI_SRC into $MORI_WHEEL_DIR.
#
# MORI is ROCm-native: setup.py always configures with -DUSE_ROCM=ON.  Host code
# builds with the system C++ compiler and the GPU kernels are JIT-compiled by
# hipcc on first use (cached in ~/.mori/jit), so no GPU is needed at build time
# -- MORI_SKIP_PRECOMPILE=1 below suppresses the opportunistic precompile that
# would otherwise probe for a device during `pip wheel`.
set -euo pipefail

MORI_SRC="${MORI_SRC:-/tmp/mori}"
MORI_REF="${MORI_REF:-}"
MORI_WHEEL_DIR="${MORI_WHEEL_DIR:-/wheels}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

if [[ ! -f "${MORI_SRC}/setup.py" ]]; then
	echo "ERROR: ${MORI_SRC}/setup.py not found" >&2
	exit 1
fi

mkdir -p "${MORI_WHEEL_DIR}"

# MORI derives its version from setuptools_scm.  The shallow clone carries the
# tag, but git apply leaves the tree dirty, which would stamp the wheel
# 1.2.2+d<date> and make the build non-reproducible.  Pin the version to the
# tag instead (MORI_VERSION overrides; a non-tag ref falls back to scm).
if [[ -z "${MORI_VERSION:-}" && "${MORI_REF}" =~ ^v[0-9]+(\.[0-9]+)*$ ]]; then
	MORI_VERSION="${MORI_REF#v}"
fi
if [[ -n "${MORI_VERSION:-}" ]]; then
	export SETUPTOOLS_SCM_PRETEND_VERSION="${MORI_VERSION}"
	echo "[mori] SETUPTOOLS_SCM_PRETEND_VERSION=${MORI_VERSION}"
fi

# Build knobs consumed by MORI's setup.py -> cmake.  Defaults here are the
# library itself (EP/SHMEM/IO/CCL/pybinds) without the C++ tests, examples or
# benchmark binaries, which need MPI and are not what this image is for.
export ROCM_PATH
export MORI_SKIP_PRECOMPILE=1
export CMAKE_BUILD_TYPE="${CMAKE_BUILD_TYPE:-Release}"
export BUILD_TESTS="${BUILD_TESTS:-OFF}"
export BUILD_EXAMPLES="${BUILD_EXAMPLES:-OFF}"
export BUILD_BENCHMARK="${BUILD_BENCHMARK:-OFF}"
export BUILD_UMBP="${BUILD_UMBP:-OFF}"
export BUILD_UMBP_SPDK="${BUILD_UMBP_SPDK:-OFF}"
export MORI_WITH_MPI="${MORI_WITH_MPI:-OFF}"
export CMAKE_BUILD_PARALLEL_LEVEL="${BUILD_JOBS}"
export MAX_JOBS="${BUILD_JOBS}"

# GPU_ARCHS drives -DGPU_TARGETS.  Unset = MORI's own autodetect, which reads
# the build host's GPUs; there are none inside a build layer, so pass ROCM_ARCH
# through when the caller knows the target arch.
if [[ -n "${ROCM_ARCH:-}" ]]; then
	export MORI_GPU_ARCHS="${ROCM_ARCH}"
	echo "[mori] MORI_GPU_ARCHS=${ROCM_ARCH}"
fi

cd "${MORI_SRC}"

# Build WITHOUT pip's build isolation, against the pinned deps below, rather
# than letting pip resolve MORI's unpinned pyproject requires to "latest".
#
# setuptools has to land inside a window, which is the whole reason it is
# pinned rather than left to pip:
#   >= 77  MORI's pyproject.toml uses the PEP 639 string form
#          (license = "MIT").  Older setuptools only accepts the table form and
#          fails metadata generation with "`project.license` must be valid
#          exactly by one definition (2 matches found)".
#   <  80  MORI builds mori.cco.cco through Cython's build_ext, and setuptools
#          80 added `assert isinstance(self.compiler, CCompiler)` to distutils'
#          build_extension.  Cython still hands it the legacy compiler string,
#          so the assertion trips and the wheel build dies on a bare
#          AssertionError.
# Override MORI_BUILD_DEPS to try a different combination.
MORI_BUILD_DEPS="${MORI_BUILD_DEPS:-setuptools>=77,<80 setuptools_scm>=8 wheel ninja cmake>=3.19 pybind11 Cython>=3.0}"
# shellcheck disable=SC2086  # the pin list is intentionally word-split
python3 -m pip install --break-system-packages ${MORI_BUILD_DEPS}

python3 -m pip wheel . --no-deps --no-build-isolation --wheel-dir "${MORI_WHEEL_DIR}"

_mori_whl="$(find "${MORI_WHEEL_DIR}" -maxdepth 1 -name 'amd_mori-*.whl' | head -1)"
if [[ -z "${_mori_whl}" ]]; then
	echo "ERROR: no amd_mori-*.whl produced in ${MORI_WHEEL_DIR}" >&2
	exit 1
fi

echo "PASS: MORI wheel built -> ${_mori_whl}"

# ----- C++ SDK install ------------------------------------------------------
# The wheel carries only the .so files, under the Python package dir.  Anything
# that wants to *link* against MORI-IO -- notably the NIXL MORI backend plugin
# -- needs the headers and a normal lib layout too, and MORI's CMakeLists has
# proper install rules for exactly that (libs to lib/, include/mori to
# include/, plus mori-config.cmake).  Reuse the build tree pip just produced,
# so this costs an install step rather than a second compile.
MORI_INSTALL_PREFIX="${MORI_INSTALL_PREFIX:-/opt/mori}"
_build_dir="${MORI_SRC}/${MORI_PYBUILD_DIR:-build}"
if [[ -f "${_build_dir}/CMakeCache.txt" ]]; then
	cmake --install "${_build_dir}" --prefix "${MORI_INSTALL_PREFIX}"
	if [[ ! -f "${MORI_INSTALL_PREFIX}/include/mori/io/engine.hpp" ]]; then
		echo "ERROR: ${MORI_INSTALL_PREFIX} has no include/mori/io/engine.hpp after install" >&2
		exit 1
	fi
	if [[ ! -e "${MORI_INSTALL_PREFIX}/lib/libmori_io.so" ]]; then
		echo "ERROR: ${MORI_INSTALL_PREFIX}/lib/libmori_io.so missing after install" >&2
		exit 1
	fi
	# MORI's PUBLIC headers include submodule headers it does not install:
	#   mori/io/common.hpp   -> <msgpack.hpp>          (3rdparty/msgpack-c)
	#   mori/utils/mori_log.hpp -> <spdlog/...>        (3rdparty/spdlog)
	# So anything that includes mori/io/*.hpp -- the NIXL MORI backend plugin --
	# cannot compile against the installed tree alone.  Stage the submodule
	# headers into the same include dir; upstream would need to install them (or
	# stop leaking them from public headers) for this to be unnecessary.
	for _sub in msgpack-c spdlog; do
		if [[ -d "${MORI_SRC}/3rdparty/${_sub}/include" ]]; then
			cp -r "${MORI_SRC}/3rdparty/${_sub}/include/." "${MORI_INSTALL_PREFIX}/include/"
			echo "[mori] staged ${_sub} headers into ${MORI_INSTALL_PREFIX}/include"
		fi
	done
	echo "PASS: MORI C++ SDK installed -> ${MORI_INSTALL_PREFIX}"
else
	echo "WARNING: no CMake build tree at ${_build_dir}; skipping the C++ SDK install" >&2
fi
