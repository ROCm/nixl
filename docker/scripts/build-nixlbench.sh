#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build nixlbench (nixl/benchmark/nixlbench) against the NIXL just installed in
# NIXL_INSTALL_PREFIX.  nixlbench is its own meson project inside the NIXL tree
# and, since v1.3.x, has first-class ROCm support:
#
#   -Duse_rocm=true -Drocm_path=$ROCM_PATH
#
# which swaps the CUDA device-memory path for HIP (-lamdhip64 -lhiprtc) and
# defines __HIP_PLATFORM_AMD__.  Without it nixlbench builds CPU/DRAM-only.
#
# ETCD is optional upstream (it is one of the two coordination runtimes, the
# other being the built-in TCP/socket one).  We do not install etcd-cpp-api, so
# nixlbench is built with the socket runtime -- fine for the two-process runs
# this image is for, and it keeps the dependency surface small.
set -euo pipefail

NIXL_SRC="${NIXL_SRC:-/tmp/nixl}"
NIXL_INSTALL_PREFIX="${NIXL_INSTALL_PREFIX:-/opt/nixl}"
NIXLBENCH_SRC="${NIXLBENCH_SRC:-${NIXL_SRC}/benchmark/nixlbench}"
NIXLBENCH_INSTALL_PREFIX="${NIXLBENCH_INSTALL_PREFIX:-/opt/nixlbench}"
UCX_PREFIX="${UCX_PREFIX:-/opt/rocnixl-ucx}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

if [[ ! -f "${NIXLBENCH_SRC}/meson.build" ]]; then
	echo "ERROR: ${NIXLBENCH_SRC}/meson.build not found -- does NIXL_REF ship nixlbench?" >&2
	exit 1
fi

# use_rocm arrived in nixlbench 1.3.x.  On an older ref the option does not
# exist and meson would fail on an unknown option, so check before passing it
# rather than silently building the CUDA path.
if ! grep -q "option('use_rocm'" "${NIXLBENCH_SRC}/meson_options.txt"; then
	echo "ERROR: ${NIXLBENCH_SRC} has no use_rocm meson option -- this NIXL_REF's" >&2
	echo "       nixlbench predates ROCm support and would build CUDA-only." >&2
	exit 1
fi

export PKG_CONFIG_PATH="${NIXL_INSTALL_PREFIX}/lib/x86_64-linux-gnu/pkgconfig:${NIXL_INSTALL_PREFIX}/lib/pkgconfig:${UCX_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
export LD_LIBRARY_PATH="${NIXL_INSTALL_PREFIX}/lib/x86_64-linux-gnu:${NIXL_INSTALL_PREFIX}/lib:${UCX_PREFIX}/lib:${LD_LIBRARY_PATH:-}"

cd "${NIXLBENCH_SRC}"
rm -rf build

MESON_ARGS=(
	"-Duse_rocm=true"
	"-Drocm_path=${ROCM_PATH}"
	"-Dnixl_path=${NIXL_INSTALL_PREFIX}"
	"--prefix=${NIXLBENCH_INSTALL_PREFIX}"
)

# nixlbench builds with werror=true by default.  New compilers (the ROCm base
# image's clang/gcc) routinely find warnings upstream's CI does not, and a
# benchmark failing to build over a warning is not useful here.
MESON_ARGS+=("-Dwerror=false")

# shellcheck disable=SC2206  # intentional word split
MESON_ARGS+=(${NIXLBENCH_MESON_EXTRA_ARGS:-})

# nixl_path expects NIXL's libdir under <prefix>/<libdir>.  Meson installed the
# libs under lib/x86_64-linux-gnu on Debian/Ubuntu, so point libdir there.
if [[ -d "${NIXL_INSTALL_PREFIX}/lib/x86_64-linux-gnu" ]]; then
	MESON_ARGS+=("-Dlibdir=lib/x86_64-linux-gnu")
fi

meson setup build "${MESON_ARGS[@]}"
ninja -C build -j"${BUILD_JOBS}"
ninja -C build install

_bin="${NIXLBENCH_INSTALL_PREFIX}/bin/nixlbench"
if [[ ! -x "${_bin}" ]]; then
	_bin="$(find "${NIXLBENCH_INSTALL_PREFIX}" -name nixlbench -type f -perm -u+x | head -1)"
fi
if [[ -z "${_bin}" || ! -x "${_bin}" ]]; then
	echo "ERROR: nixlbench binary not found under ${NIXLBENCH_INSTALL_PREFIX}" >&2
	exit 1
fi

# A nixlbench that cannot resolve libnixl/libucx at load time is useless; catch
# it here rather than at the first benchmark run.  Note this runs with the
# build-time LD_LIBRARY_PATH set above -- the runtime stage gets the same paths
# through /etc/ld.so.conf.d/nixl-mori.conf.
_ldd="$(ldd "${_bin}" 2>&1 || true)"
if grep -q "not found" <<< "${_ldd}"; then
	echo "ERROR: nixlbench has unresolved shared libraries:" >&2
	printf '%s\n' "${_ldd}" >&2
	exit 1
fi

echo "PASS: nixlbench (ROCm) built -> ${_bin}"
