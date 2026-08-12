#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Scrub build-host paths that leak out of the ROCm CMake package configs.
#
# ROCm 7.14.0's hsakmtTargets.cmake ships an INTERFACE_LINK_LIBRARIES that
# still names the absolute paths of the machine AMD built the package on:
#
#   -L/__w/rockrel/rockrel/build/third-party/.../lib;$<LINK_ONLY:-ldrm>;...;
#   /usr/lib64/libc.so;...
#
# /usr/lib64/libc.so is an RHEL path that does not exist on Ubuntu, and because
# it is an explicit file (not a -l flag) CMake turns it into a build-graph
# dependency.  Any project that does find_package(hsakmt) -- MORI does, via
# src/application -- then dies at build time with:
#
#   ninja: error: '/usr/lib64/libc.so', needed by 'libmori_application.so',
#          missing and no known rule to make it
#
# Dropping the entry is safe: libc is linked implicitly by the compiler driver.
# The bogus -L directories are left alone -- ld ignores non-existent -L paths,
# and -ldrm resolves from libdrm-dev, which the base stage installs.
#
# Idempotent, and a no-op on a ROCm release that has fixed this.
set -euo pipefail

ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
CMAKE_DIR="${ROCM_PATH}/lib/cmake"

[[ -d "${CMAKE_DIR}" ]] || {
	echo "fix-rocm-cmake: ${CMAKE_DIR} not present, nothing to do"
	exit 0
}

fixed=0
while IFS= read -r f; do
	sed -i 's#/usr/lib64/libc\.so;##g; s#;/usr/lib64/libc\.so"#"#g' "${f}"
	echo "fix-rocm-cmake: scrubbed /usr/lib64/libc.so from ${f}"
	fixed=$((fixed + 1))
done < <(grep -rl '/usr/lib64/libc\.so' "${CMAKE_DIR}" 2>/dev/null || true)

if [[ "${fixed}" -eq 0 ]]; then
	echo "fix-rocm-cmake: no /usr/lib64/libc.so references (ROCm ${ROCM_VERSION:-?} looks clean)"
fi
