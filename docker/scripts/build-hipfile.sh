#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build ROCm/hipFile from source into HIPFILE_PREFIX, overlaying the copy that
# the ROCm base image already packages.
#
# Why source-build at all, when /opt/rocm ships a libhipfile:
#
#   ROCm/hipFile publishes no semver releases.  Its `nightly` git tag has been
#   frozen at a 2026-01-22 commit, and the .deb/.rpm assets attached to the
#   "Nightly" GitHub release were last refreshed 2026-06-08 -- both are older
#   than the 0.3.0 that the pinned base image carries, so neither is a way to
#   move forward.  The only live stream is the `develop` branch, so HIPFILE_REF
#   pins a commit on it.  A SHA, not the branch head: two builds of the "same"
#   image must not differ.
#
#   What we want from develop is the asynchronous submission path --
#   hipFileReadAsync / hipFileWriteAsync over a registered HIP stream, reaching
#   Backend::async_io in src/amd_detail/backend/.  The AIS plugin is built on
#   it.  NOT the batch path: as of bd0bc233 that is a stub whose submit only
#   records the ops, with GetStatus/Cancel/Destroy throwing "Not Implemented".
#
# hipFile is pure userspace over amdgpu/DRM -- there is no kernel module in the
# repo -- so overlaying a newer library does not have to be matched against
# anything on the host.
set -euo pipefail

HIPFILE_PREFIX="${HIPFILE_PREFIX:-/opt/hipfile}"
HIPFILE_SRC="${HIPFILE_SRC:-/tmp/hipfile-src}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

if [[ ! -f "${HIPFILE_SRC}/CMakeLists.txt" ]]; then
	echo "ERROR: ${HIPFILE_SRC} is not a hipFile checkout (no CMakeLists.txt)" >&2
	exit 1
fi

# project(... LANGUAGES HIP) reads $ROCM_PATH from the environment and ignores
# it as a CMake argument, so it has to be exported rather than passed with -D.
export ROCM_PATH

cd "${HIPFILE_SRC}"
rm -rf build

# AIS_CXX_STANDARD defaults to 17 upstream, and at 17 the tree does not
# compile: src/amd_detail/batch/batch.cpp calls std::bit_cast, which libstdc++
# only declares under C++20, so the default build dies with "'bit_cast' is not
# a member of 'std'".  20 is an offered value (the cache property lists 17 and
# 20), and it is what NIXL is built at anyway.
cmake -S . -B build -G Ninja \
	-DCMAKE_BUILD_TYPE=RelWithDebInfo \
	-DAIS_CXX_STANDARD=20 \
	-DCMAKE_INSTALL_PREFIX="${HIPFILE_PREFIX}" \
	-DCMAKE_PREFIX_PATH="${ROCM_PATH}" \
	-DCMAKE_HIP_COMPILER="${ROCM_PATH}/bin/amdclang++" \
	-DBUILD_SHARED_LIBS=ON \
	-DBUILD_TESTING=OFF \
	-DAIS_INSTALL_EXAMPLES=OFF \
	-DAIS_INSTALL_TOOLS=ON

cmake --build build -j"${BUILD_JOBS}"
cmake --install build
ldconfig

# ais-check is a standalone script, not a CMake target, so `cmake --install`
# does not place it.  Install it anyway: it is the documented way to confirm
# kernel P2PDMA / amdgpu / HIP-runtime support on a node, which is the first
# thing to check when the AIS plugin silently falls back to compat mode.
if [[ -f tools/ais-check/ais-check ]]; then
	install -Dm755 tools/ais-check/ais-check "${HIPFILE_PREFIX}/bin/ais-check"
fi

# A hipFile without the async entry points is the one outcome that makes this
# whole stage pointless -- the AIS plugin's meson gate would skip the plugin and
# the build would "succeed" with the backend silently absent.  Fail here.
if [[ ! -e "${HIPFILE_PREFIX}/lib/libhipfile.so" ]]; then
	echo "ERROR: ${HIPFILE_PREFIX}/lib/libhipfile.so not built" >&2
	exit 1
fi
for sym in hipFileReadAsync hipFileWriteAsync \
	hipFileStreamRegister hipFileStreamDeregister; do
	if ! nm -D --defined-only "${HIPFILE_PREFIX}/lib/libhipfile.so" | grep -q " ${sym}$"; then
		echo "ERROR: ${sym} is not defined in the built libhipfile -- HIPFILE_REF=${HIPFILE_REF:-?} predates the AMD async backend" >&2
		exit 1
	fi
done

echo "PASS: hipFile built with the async API -> ${HIPFILE_PREFIX}"
grep -hE '#define HIPFILE_VERSION_(MAJOR|MINOR|PATCH)' \
	"${HIPFILE_PREFIX}/include/hipfile.h" || true
