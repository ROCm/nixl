#!/bin/bash
# Build ROCm/hipObject: the RDMA-accelerated S3 object-storage client for AMD
# GPUs.
#
# WHY IT IS HERE
#
# This is the client half of an S3-over-RDMA path whose server half this
# project already has. rocm-ernic's `--backend s3` is an S3 server that moves
# object payloads by RDMA rather than in the HTTP body, and its own docs name
# the protocol: "the one cuObject v1.2.0 introduced and hipObject speaks on
# the client side" -- an HTTP/1.1 request carrying an x-amz-rdma-token header
# describing the client's registered buffer, with the payload placed into that
# buffer by RDMA before the headers come back.
#
# NIXL cannot currently reach any of this. Its OBJ plugin is not built here at
# all (it needs the AWS SDK for C++), and the only S3-over-RDMA engine it has,
# src/plugins/obj/s3_accel/dell, is CUDA-only -- it links the CUDA Toolkit's
# cuObject client. There is no hipObject engine in NIXL v1.5.2. So this stage
# exists to put the library in the image first; a NIXL engine on top of it is
# the next step, not this one.
#
# Early-access upstream: hipObject's own README calls it "an early-access
# software technology preview" and says production use is not recommended.
# Pinned to a SHA accordingly.
set -euo pipefail

HIPOBJECT_SRC="${HIPOBJECT_SRC:-/tmp/hipobject-src}"
HIPOBJECT_PREFIX="${HIPOBJECT_PREFIX:-/opt/hipobject}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

cmake -S "${HIPOBJECT_SRC}" -B "${HIPOBJECT_SRC}/build" \
	-DCMAKE_BUILD_TYPE=Release \
	-DCMAKE_INSTALL_PREFIX="${HIPOBJECT_PREFIX}" \
	-DCMAKE_PREFIX_PATH="${ROCM_PATH}" \
	-DBUILD_TESTING=OFF

cmake --build "${HIPOBJECT_SRC}/build" -j"${BUILD_JOBS}"
cmake --install "${HIPOBJECT_SRC}/build"

# Fail here rather than shipping an empty prefix. The header is the contract a
# NIXL engine would compile against, and the library is what it would link.
# The header installs under include/hipobj/, not include/ -- a NIXL engine
# would therefore #include <hipobj/hipobj.h>.
test -f "${HIPOBJECT_PREFIX}/include/hipobj/hipobj.h" \
	|| { echo "ERROR: hipobj/hipobj.h not installed under ${HIPOBJECT_PREFIX}" >&2; exit 1; }
ls "${HIPOBJECT_PREFIX}"/lib*/libhipobj* >/dev/null 2>&1 \
	|| { echo "ERROR: no libhipobj in ${HIPOBJECT_PREFIX}" >&2; exit 1; }

echo "[hipobject] installed:"
ls "${HIPOBJECT_PREFIX}"/lib*/libhipobj* "${HIPOBJECT_PREFIX}/include/hipobj/hipobj.h"
