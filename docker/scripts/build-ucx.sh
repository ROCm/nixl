#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build ROCm/ucx (the AMD fork) with HIP memory-type support into UCX_PREFIX.
# This is a ROCm build, not CUDA: --with-rocm points UCX at $ROCM_PATH and no
# CUDA transport is configured.  NIXL's UCX backend links against this.
set -euo pipefail

UCX_GIT_URL="${UCX_GIT_URL:-https://github.com/ROCm/ucx.git}"
UCX_REF="${UCX_REF:-v1.19.x}"
UCX_PREFIX="${UCX_PREFIX:-/opt/rocnixl-ucx}"
UCX_SRC="${UCX_SRC:-/tmp/ucx-src}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

git config --global advice.detachedHead false

if [[ ! -d "${UCX_SRC}/.git" ]]; then
	rm -rf "${UCX_SRC}"
	git clone --depth 1 --branch "${UCX_REF}" "${UCX_GIT_URL}" "${UCX_SRC}"
fi

cd "${UCX_SRC}"
./autogen.sh
rm -rf build
mkdir build
cd build

UCX_CONFIGURE_FLAGS=(
	--prefix="${UCX_PREFIX}"
	--enable-shared
	--disable-static
	--disable-doxygen-doc
	--enable-devel-headers
	--with-rocm="${ROCM_PATH}"
	--with-verbs
	--with-dm
	--enable-mt
)

# UCX_FAST=1 trades diagnostics for compile time on the dev edit-build loop.
if [[ "${UCX_FAST:-0}" == "1" ]]; then
	UCX_CONFIGURE_FLAGS+=(
		--disable-logging
		--disable-debug
		--disable-assertions
		--disable-params-check
		--without-knem
		--without-xpmem
		--without-ugni
		--without-java
	)
else
	UCX_CONFIGURE_FLAGS+=(--enable-optimizations)
fi

../configure "${UCX_CONFIGURE_FLAGS[@]}"
make -j"${BUILD_JOBS}"
make install
ldconfig

# A UCX without the ROCm memory domain would silently degrade NIXL's GPU path
# to host staging, so treat it as a build failure here rather than a runtime
# surprise.
if [[ ! -e "${UCX_PREFIX}/lib/ucx/libuct_rocm.so" ]]; then
	echo "ERROR: ${UCX_PREFIX}/lib/ucx/libuct_rocm.so not built -- UCX did not pick up ROCm at ${ROCM_PATH}" >&2
	exit 1
fi

echo "PASS: UCX built with ROCm support -> ${UCX_PREFIX}"
