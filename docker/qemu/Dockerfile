# A QEMU carrying the NVMe shadow-doorbell polling and VRAM-queue changes.
#
# WHY THIS EXISTS
#
# Two things the published QEMU cannot do, both needed for GPU-initiated I/O
# against the emulated NVMe controller:
#
#   nvme: optional polling of shadow doorbells
#     Stock QEMU reads the shadow doorbell only while the submission queue
#     already has work. nvme_process_sq() re-reads it once on entry and once
#     per command, then returns, and sq->bh is scheduled only from
#     nvme_process_db() -- the MMIO doorbell handler. So a GPU that writes an
#     SQE and the shadow doorbell on an IDLE queue is never noticed: it can
#     say WHAT to run but not THAT it should run. Measured on this fleet, a
#     content-free MMIO write (announcing a tail the controller already had)
#     was enough to make it pick up the GPU's shadow value and complete the
#     command -- so the only missing piece was a watcher.
#
#   nvme: allow queues and descriptor lists in VRAM
#     nvme_addr_read()/nvme_addr_write() special-case CMB and PMR and fall
#     through to pci_dma_read/write() for everything else, so a queue in a
#     peer device's BAR is unreachable even with vram-dev set -- that only
#     covered the data path via nvme_map_addr().
#
# WHY IT IS BUILT ON TOP OF THE PUBLISHED IMAGE
#
# Same reasoning as docker/rocm-ernic/Dockerfile, in the other direction.
# QEMU and the device servers must agree on the vfio-user wire protocol, and
# the servers in this cluster are built against the libvfio-user in their own
# published image. Building QEMU inside the image it is replacing keeps every
# other component byte-identical and changes only the emulator.
#
# The vfio-user CLIENT (-device vfio-user-pci) is built unconditionally in
# QEMU 10.1+, so no feature flag is needed for it; vfio_user_server is a
# separate option this does not want.
#
#   docker build -t nixl-mori-qemu:dbbuf-poll docker/qemu
#
# Point a node at it by setting the qemu service's image in the generated
# compose, the way ansible/gfx-initiated-rdma.yml does for the ernic.

ARG QEMU_BASE_IMAGE=docker.io/sbates130272/batesste-ci-images-ubuntu-qemu-libvfio-user-sbates-fork:20261030.gadc94af-qemu.f91f120-vfu.3719c3a

FROM ${QEMU_BASE_IMAGE} AS build

# A SHA, not a branch. dev/stephen/pci-mmio-bridge-submit is force-pushed, so
# a branch name would make this image's contents depend on the day it was
# built -- and the whole point is an emulator whose behaviour is known.
ARG QEMU_REF=a62c5677992c5e719e7f2c8a51c1b4eb90adaa3b
ARG QEMU_GIT_URL=https://github.com/sbates130272/qemu.git

SHELL ["/bin/bash", "-euo", "pipefail", "-c"]

# Unlike the rocm-ernic image, apt-get here is safe: this image's job is to
# provide qemu-system-x86_64, and nothing it links against is the shared
# wire-protocol surface. libvfio-user lives in the SERVER images.
RUN export DEBIAN_FRONTEND=noninteractive \
    && apt-get update -qq \
    && apt-get install -y -qq --no-install-recommends \
        git build-essential pkg-config python3 python3-venv python3-pip \
        ninja-build meson flex bison \
        libglib2.0-dev libpixman-1-dev libslirp-dev zlib1g-dev \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --quiet "${QEMU_GIT_URL}" /src \
    && git -C /src checkout --quiet "${QEMU_REF}" \
    && echo "building $(git -C /src rev-parse --short HEAD)"

# Fail here rather than after a twenty-minute build: a ref without these two
# commits produces a perfectly good QEMU that silently lacks the features
# this image exists to carry.
RUN grep -q "dbbuf-poll-ns" /src/hw/nvme/ctrl.c \
    && grep -q "nvme_addr_is_vram" /src/hw/nvme/ctrl.c

RUN cd /src \
    && ./configure \
        --prefix=/opt/qemu \
        --target-list=x86_64-softmmu \
        --disable-docs \
        --disable-werror \
    && ninja -C build -j"$(nproc)" \
    && ninja -C build install

# The properties, read back off the built binary. grep on the source says the
# code is present; this says the device actually advertises them.
RUN /opt/qemu/bin/qemu-system-x86_64 -device nvme,help 2>&1 \
        | grep -q "dbbuf-poll-ns" \
    && /opt/qemu/bin/qemu-system-x86_64 -device nvme,help 2>&1 \
        | grep -q "vram-dev" \
    && /opt/qemu/bin/qemu-system-x86_64 -device help 2>&1 \
        | grep -q "vfio-user-pci" \
    && git -C /src rev-parse HEAD > /opt/qemu/qemu-source.sha

# ---------------------------------------------------------------------------
# Same base, so the entrypoint, qemu-tool and everything else are identical to
# the image the rest of the fleet runs. Only the emulator changes.
# ---------------------------------------------------------------------------
FROM ${QEMU_BASE_IMAGE}

ARG QEMU_REF
ARG QEMU_BASE_IMAGE

COPY --from=build /opt/qemu /opt/qemu

# So a running container can be asked what it is, the way the ernic image can.
RUN cp /opt/qemu/qemu-source.sha /usr/local/share/qemu-source.sha 2>/dev/null || true

LABEL org.opencontainers.image.title="QEMU with NVMe shadow-doorbell polling and VRAM queues" \
      org.opencontainers.image.description="Adds dbbuf-poll-ns/dbbuf-poll-eventidx and VRAM support in the NVMe queue and descriptor paths, built against the published image's toolchain" \
      io.batesste.qemu.source-ref="${QEMU_REF}" \
      io.batesste.qemu.base-image="${QEMU_BASE_IMAGE}"
