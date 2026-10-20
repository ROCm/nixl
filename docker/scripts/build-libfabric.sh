#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build libfabric (OFI) into LIBFABRIC_PREFIX for NIXL's LIBFABRIC backend.
#
# Source-built rather than apt-installed because Ubuntu 24.04 ships libfabric
# 1.17 and the NIXL plugin requires 1.21.0 or newer -- the distro package is
# not merely old, it is below the floor.  Same reasoning as the UCX stage.
#
# Providers are selected explicitly rather than left to autodetect so that a
# missing build dep turns into a configure error here instead of a provider
# that silently vanishes at runtime:
#
#   verbs    RDMA over the ConnectX-7/Broadcom back-end NICs (libibverbs,
#            librdmacm -- both in the shared apt layer).  Needs ofi_rxm to
#            present an FI_EP_RDM endpoint, which is what NIXL asks for.
#   tcp      the portable fallback, and what NIXL's provider preference list
#            lands on when nothing faster is present.
#   shm      intra-node transfers.
#   sockets  legacy fallback; cheap to build and useful for bring-up.
#
# efa is deliberately absent: it needs the AWS EFA kernel driver and is
# meaningless off EC2.
set -euo pipefail

LIBFABRIC_PREFIX="${LIBFABRIC_PREFIX:-/opt/libfabric}"
LIBFABRIC_SRC="${LIBFABRIC_SRC:-/tmp/libfabric-src}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

# The checkout is made (and patched, if patches/libfabric/ ever gains content)
# by clone-src.sh, exactly as for UCX, NIXL and MORI.
if [[ ! -f "${LIBFABRIC_SRC}/configure.ac" ]]; then
	echo "ERROR: ${LIBFABRIC_SRC} is not a libfabric checkout (no configure.ac)" >&2
	exit 1
fi

cd "${LIBFABRIC_SRC}"
./autogen.sh
rm -rf build
mkdir build
cd build

../configure \
	--prefix="${LIBFABRIC_PREFIX}" \
	--enable-shared \
	--disable-static \
	--enable-verbs \
	--enable-tcp \
	--enable-shm \
	--enable-sockets \
	--disable-efa \
	--disable-psm2 \
	--disable-psm3 \
	--disable-opx \
	--disable-usnic \
	--disable-rstream

make -j"${BUILD_JOBS}"
make install

# fi_info is the first thing to reach for when the plugin picks an unexpected
# provider on a new host, so keep it (configure builds it by default) and prove
# the library it links against is the one just installed.
if [[ ! -x "${LIBFABRIC_PREFIX}/bin/fi_info" ]]; then
	echo "ERROR: ${LIBFABRIC_PREFIX}/bin/fi_info not installed" >&2
	exit 1
fi

# A build-time check only: this container has no RDMA devices, so fi_info finds
# nothing and exits non-zero.  What is being asserted is that the providers were
# compiled in, which `fi_info -l` reports without touching hardware.
echo "libfabric providers compiled in:"
LD_LIBRARY_PATH="${LIBFABRIC_PREFIX}/lib:${LD_LIBRARY_PATH:-}" \
	"${LIBFABRIC_PREFIX}/bin/fi_info" -l | sed 's/^/  /'

for _prov in verbs tcp shm; do
	if ! LD_LIBRARY_PATH="${LIBFABRIC_PREFIX}/lib:${LD_LIBRARY_PATH:-}" \
		"${LIBFABRIC_PREFIX}/bin/fi_info" -l | grep -q "^${_prov}:"; then
		echo "ERROR: libfabric built without the ${_prov} provider" >&2
		exit 1
	fi
done

ldconfig
echo "PASS: libfabric built -> ${LIBFABRIC_PREFIX}"
