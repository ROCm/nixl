#!/bin/bash
# Build linux-rdma/perftest with ROCm support.
#
# perftest is the reference RDMA microbenchmark -- ib_write_bw, ib_read_bw,
# ib_send_bw and friends. It exists in this image for the same reason fio
# does: to put a number next to NIXL's that has none of NIXL in it. When
# LIBFABRIC and UCX disagree, or when a backend plateaus, the question is
# always whether the fabric or the stack is the limit, and perftest answers
# the first half directly against libibverbs.
#
# --enable-rocm is what makes it useful here rather than just present: with
# it, `ib_write_bw --use_rocm=<gpu>` registers VRAM and measures the same
# device-memory path the NIXL backends take, with nothing between the
# benchmark and the NIC.
#
# --enable-rocm-dmabuf asks for the dmabuf registration path specifically.
# That is the path this cluster had to patch twice to get working at all
# (patches/ucx/03, patches/libfabric/01), so a perftest that can exercise it
# directly is the quickest way to tell a library bug from a device one: if
# ib_write_bw --use_rocm succeeds where a NIXL backend fails, the device is
# fine and the fault is in the library above it.
set -euo pipefail

PERFTEST_SRC="${PERFTEST_SRC:-/tmp/perftest-src}"
PERFTEST_PREFIX="${PERFTEST_PREFIX:-/opt/perftest}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

cd "${PERFTEST_SRC}"

# perftest ships no configure in git; autogen.sh generates it.
./autogen.sh

# CUDA is deliberately absent: this is a ROCm image and perftest's CUDA path
# would not compile. The two are mutually exclusive in its configure anyway.
# --with-rocm=<path> is the one that matters: it is what adds the include and
# library paths and links libamdhip64/libhsa-runtime64. --enable-rocm alone
# only sets __HIP_PLATFORM_AMD__, and configure then fails to find the headers
# and quietly leaves the ROCm benchmarks out -- the build still succeeds and
# ib_write_bw simply has no --use_rocm, which is what the check below catches.
./configure \
	--prefix="${PERFTEST_PREFIX}" \
	--enable-rocm \
	--enable-rocm-dmabuf \
	--with-rocm="${ROCM_PATH}"

make -j"${BUILD_JOBS}"
make install

# Fail here rather than shipping a perftest that silently cannot touch VRAM:
# without ROCm support --use_rocm is simply not a recognised option, and the
# first indication would be a benchmark run that measured host memory.
# Capture first, then grep. `ib_write_bw --help` exits non-zero, and under
# `set -o pipefail` a pipeline into grep reports THAT status even when grep
# matched -- so piping straight into grep -q inverts the test and fails a
# perfectly good ROCm build.
_help="$("${PERFTEST_PREFIX}/bin/ib_write_bw" --help 2>&1 || true)"
if ! grep -q -- '--use_rocm' <<< "${_help}"; then
	echo "ERROR: ib_write_bw has no --use_rocm; the ROCm build did not take" >&2
	exit 1
fi
echo "[perftest] ROCm support confirmed (--use_rocm present)"
ls "${PERFTEST_PREFIX}/bin" | tr '\n' ' '
echo
