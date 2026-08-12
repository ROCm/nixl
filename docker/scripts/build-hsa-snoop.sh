#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build hsa-snoop -- an HSA AQL queue/dispatch snooper with a Prometheus
# exporter -- into HSA_SNOOP_INSTALL_PREFIX.
#
# Why it is in this image: it is the only thing here that can see what the AIS
# (AMD Infinity Storage) path is actually doing.  nixlbench reports end-to-end
# bandwidth; hsa-snoop reports the AIS rx/tx ops, bytes and errors underneath
# it, plus SDMA/dispatch activity and GPU page-fault (XNACK) retries.  When an
# AIS_MT run comes back slower than the POSIX one, this is how you find out
# whether the GPU-direct path is being used at all.
#
# -DHSA_SNOOP_PROMETHEUS=ON pulls prometheus-cpp v1.2.4 via CMake FetchContent
# (CivetWeb bundled, no system dependency) and is what enables --prometheus.
#
# -DCMAKE_DISABLE_FIND_PACKAGE_hip=ON skips the HIP example programs.  They are
# not needed here and they would want a GPU arch to compile for, which a
# CPU-only build node does not have.
set -euo pipefail

HSA_SNOOP_GIT_URL="${HSA_SNOOP_GIT_URL:-https://github.com/sbates130272/hsa-snoop.git}"
HSA_SNOOP_REF="${HSA_SNOOP_REF:-main}"
HSA_SNOOP_SRC="${HSA_SNOOP_SRC:-/tmp/hsa-snoop}"
HSA_SNOOP_INSTALL_PREFIX="${HSA_SNOOP_INSTALL_PREFIX:-/opt/hsa-snoop}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

git config --global advice.detachedHead false

rm -rf "${HSA_SNOOP_SRC}"
if ! git clone --depth 1 --branch "${HSA_SNOOP_REF}" "${HSA_SNOOP_GIT_URL}" "${HSA_SNOOP_SRC}" 2> /dev/null; then
	echo "[hsa-snoop] shallow clone of '${HSA_SNOOP_REF}' failed; retrying as a full clone"
	rm -rf "${HSA_SNOOP_SRC}"
	git clone "${HSA_SNOOP_GIT_URL}" "${HSA_SNOOP_SRC}"
	git -C "${HSA_SNOOP_SRC}" checkout --detach "${HSA_SNOOP_REF}"
fi
echo "[hsa-snoop] ${HSA_SNOOP_REF} sha=$(git -C "${HSA_SNOOP_SRC}" rev-parse HEAD)"

cmake -S "${HSA_SNOOP_SRC}" -B "${HSA_SNOOP_SRC}/build" -G Ninja \
	-DCMAKE_BUILD_TYPE=Release \
	-DHSA_SNOOP_PROMETHEUS=ON \
	-DCMAKE_DISABLE_FIND_PACKAGE_hip=ON
cmake --build "${HSA_SNOOP_SRC}/build" --parallel "${BUILD_JOBS}"
cmake --install "${HSA_SNOOP_SRC}/build" --prefix "${HSA_SNOOP_INSTALL_PREFIX}"

_bin="${HSA_SNOOP_INSTALL_PREFIX}/bin/hsa-snoop"
[[ -x "${_bin}" ]] || {
	echo "ERROR: ${_bin} not installed" >&2
	exit 1
}

# A build without the exporter still produces a working binary, just one that
# cannot do the job we want it for -- so check for the flag, not the file.
if ! { "${_bin}" --help 2>&1 || true; } | grep -q -- '--prometheus'; then
	echo "ERROR: hsa-snoop built without the Prometheus exporter (--prometheus absent)" >&2
	exit 1
fi

echo "PASS: hsa-snoop (${HSA_SNOOP_REF}) with Prometheus exporter -> ${_bin}"
