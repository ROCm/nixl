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

# Off by default, and deliberately so: --enable-debug compiles in the
# providers' FI_DBG calls and their assertions, which is what makes
# FI_LOG_LEVEL=debug say anything at all.  Without it the only thing that
# answers a provider-level question is core initialisation chatter.
#
# It is not free -- the logging is on the data path and the assertions are
# real work -- so a benchmark image should not carry it. Build a second image
# with LIBFABRIC_DEBUG=1 when a provider needs interrogating, and throw it
# away afterwards.
LIBFABRIC_DEBUG="${LIBFABRIC_DEBUG:-}"
DEBUG_ARGS=()
if [[ -n "${LIBFABRIC_DEBUG}" && "${LIBFABRIC_DEBUG}" != "0" ]]; then
	DEBUG_ARGS+=(--enable-debug)
	echo "[libfabric] --enable-debug requested: FI_DBG and assertions compiled in"
	echo "[libfabric] this build is for diagnosis, not for benchmark numbers"
fi

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
	"${DEBUG_ARGS[@]}" \
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

# fabtests, into the same prefix.
#
# fi_info alone cannot tell you whether a provider actually moves data, and the
# NIXL plugin is too many layers up to bisect with.  What is needed is a pair of
# programs that differ in exactly one thing: fi_msg_pingpong drives a verbs
# FI_EP_MSG endpoint directly, fi_rdm_pingpong drives the same device through
# ofi_rxm.  One working and the other not is the difference between "the device
# cannot do this" and "rxm's use of it is wrong", and that distinction is not
# otherwise reachable from inside this image.
#
# Not fatal if it fails to build: these are diagnostics, and an image without
# them is still a working benchmark image.  It builds from the same checkout, so
# it cannot drift from the library it tests against.
if [[ -f "${LIBFABRIC_SRC}/fabtests/configure.ac" ]]; then
	echo "building fabtests"
	if (
		cd "${LIBFABRIC_SRC}/fabtests"
		./autogen.sh
		./configure --prefix="${LIBFABRIC_PREFIX}" \
			--with-libfabric="${LIBFABRIC_PREFIX}"
		make -j"${BUILD_JOBS}"
		make install
	); then
		echo "fabtests installed:"
		ls "${LIBFABRIC_PREFIX}/bin" | grep -E '^fi_(msg|rdm|rma)' | sed 's/^/  /' | head
	else
		echo "WARNING: fabtests did not build; continuing without them" >&2
	fi
else
	echo "no fabtests/ in this libfabric checkout -- skipping"
fi

echo "PASS: libfabric built -> ${LIBFABRIC_PREFIX}"
