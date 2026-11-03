# A rocm-ernic image whose RDMA CM works, on the libvfio-user revision this
# cluster's QEMU needs.
#
# WHY THIS EXISTS
#
# NIXL's LIBFABRIC backend needs RDMA CM: libfabric's verbs;ofi_rxm sets its
# connections up through it. Guest-to-guest rdma_cm was broken until
# ROCm/rocm-ernic PR #315 ("feat/make guest-to-guest rdma_cm work", branch
# feat/rdma-cm), merged to develop as daa7d94 on 2026-10-22. The device set
# IONIC_V1_CQE_RECV_IS_IPV4 on receives while still writing an IPv6 GRH, so
# the kernel read the source address out of the middle of the destination GID,
# ib_cm ARPed for 0.0.255.255, and every CM REQ was dropped with no reject and
# nothing logged at either end. It presents as rdma_connect() timing out --
# RDMA_CM_EVENT_UNREACHABLE, error -110 -- and as nixlbench --backend
# LIBFABRIC reaching "All processes are ready to proceed" and then never
# transferring a byte.
#
# The published ernic this cluster pins, ernic.31d3114 from the 20261021
# build, is one day older than that merge. Two published builds do carry the
# fix -- ernic.8f35f4a (20261030) and anything newer -- and neither is usable
# here, because of the second constraint:
#
# WHY IT IS BUILT ON TOP OF A PUBLISHED IMAGE RATHER THAN FROM SCRATCH
#
# QEMU and the device servers must share one libvfio-user revision; the
# vfio-user wire protocol is not compatible across them, and qemu-tool's
# gen-compose refuses a mismatched set. VRAM over the fabric needs the FORKED
# QEMU, because an RDMA write into GPU memory is one vfio-user device DMAing
# into another's BAR and stock QEMU never advertises the BAR window. The only
# forked QEMU builds carrying that fix are at vfu.8039244 -- and the 20261030
# ernic is at vfu.5b9c722. So no published pair gives both a working CM and a
# working VRAM path.
#
# Building newer rocm-ernic against the libvfio-user already installed in the
# 20261021 image resolves it: new device, old wire protocol. That is the only
# reason the base image is what it is.
#
# So the invariant is NOT that ERNIC_BASE_IMAGE tracks ERNIC_IMAGE in
# cluster/env/nixl-mori -- that now names this image, built here. It is that
# ERNIC_BASE_IMAGE carries the same vfu.* revision as QEMU_IMAGE, because the
# server inherits its libvfio-user from the base and has to speak the same
# wire protocol as the QEMU it serves. `make pins-check` enforces it.
#
#   docker buildx bake ernic
#   make ernic-image
#
# KNOWN GAP, NOT FIXABLE HERE
#
# This server binds its mesh-manager port on tcp6 only -- a dual-stack socket,
# which Linux reports in /proc/net/tcp6 and not /proc/net/tcp. gen-compose's
# manager healthcheck greps /proc/net/tcp for a v4 LISTEN, so it never passes,
# every worker gates on it and `make cluster-up` starts no guests. The mesh
# itself is fine: v4 clients connect through the dual-stack socket. It cannot
# be worked around from this image -- an explicit 0.0.0.0 bind would collide
# with the v6 socket, and the backend string is generated, not ours. The fix
# belongs in gen-compose, which should check both files. Until then see
# cluster/README.md for the start order that works.

ARG ERNIC_BASE_IMAGE=docker.io/sbates130272/batesste-ci-images-ubuntu-rocm-ernic-sbates-fork:20261030.gadc94af-ernic.8f35f4a-vfu.3719c3a

# ---------------------------------------------------------------------------
# Build the server against the base image's own libvfio-user.
# ---------------------------------------------------------------------------
FROM ${ERNIC_BASE_IMAGE} AS build

# A SHA, not a branch. `develop` would make this image's contents depend on
# the day it was built, and the whole point is a device whose behaviour is
# known. Move it deliberately, and re-test LIBFABRIC when you do.
ARG ERNIC_REF=461dbbd5762c9485a4918d3cad7e616aaaedcda0
ARG ERNIC_GIT_URL=https://github.com/ROCm/rocm-ernic.git

# The commit that makes guest-to-guest rdma_cm work. Asserted below rather
# than assumed: a ref that predates it builds and runs perfectly well and
# fails only later, in LIBFABRIC, as a timeout with nothing logged.
ARG ERNIC_RDMA_CM_FIX=daa7d94

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

RUN git clone --quiet "${ERNIC_GIT_URL}" /src \
    && git -C /src checkout --quiet "${ERNIC_REF}" \
    && if ! git -C /src merge-base --is-ancestor "${ERNIC_RDMA_CM_FIX}" HEAD; then \
         echo "ERROR: ERNIC_REF=${ERNIC_REF} predates ${ERNIC_RDMA_CM_FIX}," >&2; \
         echo "       ROCm/rocm-ernic#315 -- guest-to-guest rdma_cm does not work" >&2; \
         echo "       there, and LIBFABRIC will hang rather than fail." >&2; \
         exit 1; \
       fi \
    && echo "building $(git -C /src rev-parse --short HEAD)"

# The base image already carries libvfio-user, its headers, and the glib,
# json-c and cmocka dev packages. Nothing is installed here on purpose: an
# apt-get would risk pulling a different libvfio-user than the one this image
# has to match.
RUN cmake -G Ninja -S /src -B /src/build \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_TESTING=OFF \
        -DERNIC_BUILD_DOCS=OFF \
    && ninja -C /src/build rocm-ernic

# Fail here rather than shipping a server that cannot talk to this QEMU.
RUN ldd /src/build/rocm-ernic | grep -q 'libvfio-user\.so' \
    && /src/build/rocm-ernic --help >/dev/null 2>&1 \
    && git -C /src rev-parse HEAD > /src/build/rocm-ernic.sha

# ---------------------------------------------------------------------------
# Same base, so everything except the server is byte-identical to the image
# the rest of the fleet was built and tested against.
# ---------------------------------------------------------------------------
FROM ${ERNIC_BASE_IMAGE}

ARG ERNIC_REF
ARG ERNIC_BASE_IMAGE

COPY --from=build /src/build/rocm-ernic /usr/local/bin/rocm-ernic
COPY --from=build /src/build/rocm-ernic.sha /usr/local/share/rocm-ernic-source.sha

# So a running container can be asked what it is. `docker exec ernic-1-1 cat
# /usr/local/share/rocm-ernic-source.sha` answers "which device am I actually
# running", which took a while to establish by binary size alone.
LABEL org.opencontainers.image.title="rocm-ernic with working guest-to-guest RDMA CM" \
      org.opencontainers.image.description="rocm-ernic built from a ref containing ROCm/rocm-ernic#315, against the base image's libvfio-user so it pairs with the forked QEMU at vfu.8039244" \
      io.batesste.ernic.source-ref="${ERNIC_REF}" \
      io.batesste.ernic.base-image="${ERNIC_BASE_IMAGE}"
