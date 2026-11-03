# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# nixl-mori-test: NIXL + MORI built from tagged upstream releases plus local
# patches, for AMD ROCm (not CUDA).
#
#   NIXL: ai-dynamo/nixl   ${NIXL_REF} + every patch in patches/nixl/
#   MORI: ROCm/mori        ${MORI_REF} + every patch in patches/mori/
#   UCX:  openucx/ucx      ${UCX_REF} + every patch in patches/ucx/
#                                       (source-built so NIXL's UCX backend is
#                                        ROCm/HIP-aware; the distro UCX is not)
#
# Patches: every *.patch under patches/<component>/ is applied to that
# component's fresh checkout in lexical order -- name them NN-<topic>.patch to
# fix the sequence.  A patch that does not apply fails the build.  An empty
# patchset directory is fine: the pristine tag is built.
#
# Stage graph (each stage is reachable on its own with --target):
#
#   base -> ucx ------------> nixl ----\
#     \---> mori --(SDK)-------^        >-> runtime
#     \---> hsasnoop -------------------/
#            \-------------------------/
#            \------> wheels (scratch; wheel export only)
#
# The mori -> nixl edge is the MORI C++ SDK (/opt/mori): a NIXL MORI backend
# plugin dropped into patches/nixl/ links against MORI-IO.  Without such a
# patch the SDK is simply unused, but the edge is unconditional so the plugin
# can be added without restructuring the build.
#
#   docker build -f docker/Dockerfile --target nixl .   # NIXL only
#   docker build -f docker/Dockerfile --target mori .   # MORI only
#
# Behind a corporate proxy (AMD Zscaler), pass the CA as a BuildKit secret --
# never as a build arg, so it is not baked into the image:
#
#   docker build --secret id=tls_cert,src=/path/to/corp-ca.crt ...
#
# Build args are surfaced as `make build NIXL_REF=... MORI_REF=...`.

# ----- 0. Base ---------------------------------------------------------------
ARG ROCM_VERSION=7.14.0
ARG ROCM_BASE_IMAGE=rocm/dev-ubuntu-24.04:${ROCM_VERSION}-full
# Base for the slim runtime stage. Overridable so it can come from somewhere
# other than Docker Hub: a site mirror that proxies docker.io makes the
# *mirror* fetch upstream, so a local `docker login` does not apply to it and
# its quota is what you hit. Naming a registry explicitly --
# registry-1.docker.io/library/ubuntu:24.04 -- skips the mirror.
ARG RUNTIME_BASE_IMAGE=ubuntu:24.04

FROM ${ROCM_BASE_IMAGE} AS base

ARG ROCM_VERSION
ARG ROCM_BASE_IMAGE
# Target GPU arch (e.g. gfx942, gfx950).  Empty = each component's own default
# (MORI autodetects, which finds nothing in a build layer and falls back to its
# built-in arch list).  `make build` passes the detected host arch.
ARG ROCM_ARCH=
# Cap parallel compile jobs; empty = all cores.
ARG BUILD_JOBS=

ENV ROCM_PATH=/opt/rocm \
    ROCM_ARCH=${ROCM_ARCH} \
    BUILD_JOBS=${BUILD_JOBS} \
    DEBIAN_FRONTEND=noninteractive \
    PIP_ROOT_USER_ACTION=ignore

# Optional corporate CA, mounted read-only into this one layer and never
# written into the image filesystem or a cache entry.
RUN --mount=type=secret,id=tls_cert,required=false \
    if [ -f /run/secrets/tls_cert ] && [ -s /run/secrets/tls_cert ]; then \
        cp /run/secrets/tls_cert /usr/local/share/ca-certificates/corp-ca.crt && \
        update-ca-certificates; \
    fi

# One apt layer shared by every stage: NIXL's build+runtime deps, MORI's
# (libpci/libibverbs/libdw), and UCX's autotools chain.  The dev packages stay
# in the runtime stage on purpose -- they carry the shared objects both
# components have a NEEDED on, and this is a test image, not a size-optimised
# deployment artifact.
#
# Deliberately NOT here: the gRPC/protobuf/abseil stack.  MORI needs it only
# for UMBP (off by default), and installing libabsl-dev breaks the NIXL build
# outright -- NIXL finds Ubuntu's absl_base, sees no absl_log, and refuses to
# fall back to its own Abseil subproject rather than risk two Abseil versions
# in one process.  The mori stage installs it on demand instead.
#
# apt lists and debs live on BuildKit cache mounts so they are reused across
# rebuilds without landing in the layer; docker-clean must go first or the base
# image purges the archive cache after every install.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        autoconf \
        automake \
        build-essential \
        ca-certificates \
        ccache \
        cmake \
        curl \
        git \
        ibverbs-providers \
        ibverbs-utils \
        libaio-dev \
        libasio-dev \
        libdrm-dev \
        libdw-dev \
        libgflags-dev \
        libhwloc-dev \
        libibverbs-dev \
        libltdl-dev \
        libmount-dev \
        libnuma-dev \
        libopenmpi3t64 \
        libpci-dev \
        librdmacm-dev \
        libtool \
        liburing-dev \
        ninja-build \
        pkg-config \
        python3-dev \
        python3-pip \
        rdma-core \
    && update-ca-certificates
# libmount-dev is hipFile's one non-ROCm build dep (src/amd_detail/mountinfo.cpp
# resolves which filesystem a target file lives on, since the fast path only
# accepts xfs/ext4 with ordered journaling).
# libopenmpi3t64 is the openmpi RUNTIME lib only (not openmpi-bin/-dev): ROCm
# torch wheels have a hard NEEDED on libmpi_cxx.so.40 via torch._load_global_deps(),
# so `import torch` fails without it.  libgflags-dev/libasio-dev are nixlbench's
# required deps.

# Every source below is cloned then checked out at a pinned ref (detached HEAD
# by design); silence the multi-line advisory each clone would print.
RUN git config --global advice.detachedHead false

# Ubuntu 24.04 marks the system interpreter PEP-668 "externally managed", which
# makes plain `python3 -m pip install` refuse to touch /usr.  This is a
# single-purpose container built entirely on the system Python.
RUN rm -f /usr/lib/python3.*/EXTERNALLY-MANAGED

# ROCm 7.14.0 ships a hsakmtTargets.cmake with the packager's own build-host
# paths baked in, including an /usr/lib64/libc.so that does not exist on Ubuntu
# -- which breaks any find_package(hsakmt) consumer (MORI).  See the script.
COPY docker/scripts/fix-rocm-cmake.sh /tmp/scripts/
RUN bash /tmp/scripts/fix-rocm-cmake.sh

# The rocm/dev-ubuntu-24.04 image ships no ld.so.conf.d entry for /opt/rocm/lib
# (`ldconfig -p | grep amdhip64` comes back empty), so anything linked against
# libamdhip64 -- nixlbench built with -Duse_rocm=true, the MORI libs -- fails to
# load unless the caller sets LD_LIBRARY_PATH.  Register it once, here, so every
# later stage and the final image just work.
RUN printf '%s\n' /opt/rocm/lib /opt/rocm/lib64 > /etc/ld.so.conf.d/rocm.conf && \
    ldconfig && \
    ldconfig -p | grep -q libamdhip64 || \
    { echo "ERROR: libamdhip64 still not in the ldconfig cache" >&2; exit 1; }

# ----- 1. UCX (source-built with HIP memory support) -------------------------
# openucx rather than the ROCm fork: the fork has no v1.22 tag, only a v1.22.x
# branch whose head is the identical commit to openucx's v1.22.0 tag, and a tag
# is reproducible where a branch head is not.  Revisit if the fork diverges.
FROM base AS ucx

ARG UCX_GIT_URL=https://github.com/openucx/ucx.git
ARG UCX_REF=v1.22.0
# UCX_FAST=1 drops logging/asserts/param-checks for a quicker dev rebuild.
ARG UCX_FAST=0
# UCX_DEBUG_LOG=1 keeps ucs_debug/ucs_trace compiled in, which is the only way
# to see UCX's transport-selection reasoning.  Diagnostic builds only.
ARG UCX_DEBUG_LOG=0

ENV UCX_PREFIX=/opt/rocnixl-ucx

COPY docker/scripts/clone-src.sh docker/scripts/build-ucx.sh /tmp/scripts/
COPY patches/ucx/ /tmp/patches/ucx/

RUN SRC_NAME=ucx \
    SRC_GIT_URL="${UCX_GIT_URL}" \
    SRC_REF="${UCX_REF}" \
    SRC_DEST=/tmp/ucx-src \
    SRC_PATCH_DIR=/tmp/patches/ucx \
    bash /tmp/scripts/clone-src.sh

RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    UCX_SRC=/tmp/ucx-src UCX_FAST="${UCX_FAST}" UCX_DEBUG_LOG="${UCX_DEBUG_LOG}" \
    bash /tmp/scripts/build-ucx.sh && \
    rm -rf /tmp/ucx-src

# ----- 1a-bis. libfabric (OFI, for NIXL's LIBFABRIC backend) -----------------
# FROM base, not FROM ucx: libfabric and UCX are independent transports, so this
# stage runs concurrently with the UCX, hipFile and MORI stages.
#
# Source-built for the same reason UCX is: Ubuntu 24.04 packages libfabric 1.17,
# below the 1.21.0 floor the NIXL plugin requires.  See build-libfabric.sh for
# which providers are compiled in and why.
#
# Pinned to the latest stable release rather than to NIXL's own CI pin.  Note
# that upstream tests its ROCm lane against v1.21.0 and builds it
# --disable-verbs --enable-efa, so verbs;ofi_rxm with ROCR -- what this image
# runs -- is not a configuration NIXL exercises on any version.  Being current
# is the better of two untested positions, and 2.x is where FI_HMEM_ROCR
# carries a device handle (fi_mr_attr gained device.rocr in v2.3, which NIXL
# v1.5.0 sets behind a FI_VERSION guard).
#
# LIBFABRIC_REF empty skips the stage and leaves /opt/libfabric empty, which
# build-nixl.sh reads as "do not build the LIBFABRIC plugin" -- the escape hatch
# if a libfabric bump regresses, and what keeps the COPY below unconditional.
FROM base AS libfabric

ARG LIBFABRIC_GIT_URL=https://github.com/ofiwg/libfabric.git
ARG LIBFABRIC_REF=v2.7.0
# Empty for a benchmark image.  Set to 1 to compile in the providers' FI_DBG
# calls and assertions, which is the only way FI_LOG_LEVEL=debug reports
# anything from a provider rather than from the core.  See
# docker/scripts/build-libfabric.sh for why this is not the default.
ARG LIBFABRIC_DEBUG=

ENV LIBFABRIC_PREFIX=/opt/libfabric

COPY patches/libfabric/ /tmp/patches/libfabric/
COPY docker/scripts/clone-src.sh docker/scripts/build-libfabric.sh /tmp/scripts/

RUN mkdir -p /opt/libfabric && \
    if [ -z "${LIBFABRIC_REF}" ]; then \
        echo "LIBFABRIC_REF empty -- skipping libfabric; NIXL's LIBFABRIC plugin will not be built"; \
    else \
        SRC_NAME=libfabric \
        SRC_GIT_URL="${LIBFABRIC_GIT_URL}" \
        SRC_REF="${LIBFABRIC_REF}" \
        SRC_PATCH_DIR=/tmp/patches/libfabric \
        SRC_DEST=/tmp/libfabric-src \
        bash /tmp/scripts/clone-src.sh; \
    fi

RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    if [ -n "${LIBFABRIC_REF}" ]; then \
        LIBFABRIC_SRC=/tmp/libfabric-src \
        LIBFABRIC_DEBUG="${LIBFABRIC_DEBUG}" \
        bash /tmp/scripts/build-libfabric.sh && rm -rf /tmp/libfabric-src; \
    fi

# ----- 1b. hipFile (source-built, overlays the ROCm-packaged copy) -----------
# FROM base, not FROM ucx: hipFile depends on neither, so this runs concurrently
# with the UCX and MORI stages.
#
# HIPFILE_REF empty means "use whatever libhipfile the ROCm base image packages"
# -- the escape hatch if a develop commit regresses.  The stage still runs and
# still produces /opt/hipfile, just empty, so the COPY lines below do not have
# to be conditional.  See build-hipfile.sh for why a SHA on develop is the only
# way to track hipFile's live stream.
FROM base AS hipfile

ARG HIPFILE_GIT_URL=https://github.com/ROCm/hipFile.git
ARG HIPFILE_REF=bd0bc2330a1573223209b9899055d1d6188b9113

ENV HIPFILE_PREFIX=/opt/hipfile

COPY docker/scripts/clone-src.sh docker/scripts/build-hipfile.sh \
     docker/scripts/hipfile-batch-smoke.hip.cpp /tmp/scripts/
# patches/hipfile implements the batch API, which upstream accepts and then
# silently drops.  Patched here rather than worked around in the AIS plugin
# because the defect is in hipFile and fio hits it too.
COPY patches/hipfile /tmp/patches/hipfile

RUN mkdir -p /opt/hipfile && \
    if [ -z "${HIPFILE_REF}" ]; then \
        echo "HIPFILE_REF empty -- using the ROCm-packaged hipFile"; \
    else \
        SRC_NAME=hipfile \
        SRC_GIT_URL="${HIPFILE_GIT_URL}" \
        SRC_REF="${HIPFILE_REF}" \
        SRC_DEST=/tmp/hipfile-src \
        SRC_PATCH_DIR=/tmp/patches/hipfile \
        bash /tmp/scripts/clone-src.sh; \
    fi

RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    if [ -n "${HIPFILE_REF}" ]; then \
        HIPFILE_SRC=/tmp/hipfile-src HIPFILE_REF="${HIPFILE_REF}" \
        bash /tmp/scripts/build-hipfile.sh && rm -rf /tmp/hipfile-src; \
    fi

# ----- 1c. fio with the hipFile ioengine --------------------------------------
# FROM hipfile, not FROM base: the engine must link the same libhipfile the AIS
# plugins do, or a "fio says X, NIXL says Y" comparison is comparing two
# libraries rather than two submission paths.  build-fio.sh explains how the
# two hipFile installs are disambiguated.
#
# The ROCm fork is pinned to a SHA on a topic branch that publishes no tags;
# it carries the async engine (hipfile_mode=sync|stream|batch) that upstream
# fio does not have.  FIO_REF empty skips the whole stage -- /opt/fio is then
# absent and the image simply has no fio, so the escape hatch costs nothing.
FROM hipfile AS fio

ARG FIO_GIT_URL=https://github.com/ROCm/fio.git
ARG FIO_REF=c32261752c88b3f7aadfde4d011a028ae954f869

ENV FIO_PREFIX=/opt/fio

RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    if [ -n "${FIO_REF}" ]; then \
        apt-get update && apt-get install -y --no-install-recommends \
            libaio-dev libnuma-dev zlib1g-dev pkg-config; \
    fi

COPY docker/scripts/clone-src.sh docker/scripts/build-fio.sh /tmp/scripts/

RUN mkdir -p /opt/fio /opt/fio-hipfile && \
    if [ -z "${FIO_REF}" ]; then \
        echo "FIO_REF empty -- no fio in this image"; \
    else \
        SRC_NAME=fio \
        SRC_GIT_URL="${FIO_GIT_URL}" \
        SRC_REF="${FIO_REF}" \
        SRC_DEST=/tmp/fio-src \
        bash /tmp/scripts/clone-src.sh; \
    fi

RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    if [ -n "${FIO_REF}" ]; then \
        FIO_SRC=/tmp/fio-src bash /tmp/scripts/build-fio.sh && rm -rf /tmp/fio-src; \
    fi

# ----- 2. MORI (ROCm/mori tag + patches/mori/) -------------------------------
# FROM base, not FROM ucx: MORI needs neither UCX nor NIXL, so this stage runs
# concurrently with the UCX build.  It is placed before the nixl stage because
# nixl copies MORI's C++ SDK out of it.
FROM base AS mori

ARG MORI_GIT_URL=https://github.com/ROCm/mori.git
ARG MORI_REF=v1.2.4
# MORI's optional pieces.  UMBP/SPDK is off (the SPDK submodule is huge and
# needs its own selective checkout); tests/examples/benchmarks need MPI.
ARG MORI_BUILD_UMBP=OFF
ARG MORI_BUILD_TESTS=OFF
ARG MORI_BUILD_EXAMPLES=OFF
ARG MORI_BUILD_BENCHMARK=OFF

# MORI's UMBP control plane (src/umbp) is the only part that needs gRPC and
# protobuf, and pulling those in drags Ubuntu's libabsl along -- which is why
# they are installed HERE, only when UMBP is actually being built, and never in
# the shared base where they would break the NIXL stage's Abseil detection.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    if [ "${MORI_BUILD_UMBP}" = "ON" ]; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            libgrpc++-dev libgrpc-dev libprotobuf-dev protobuf-compiler-grpc; \
    fi

COPY docker/scripts/clone-src.sh docker/scripts/build-mori.sh /tmp/scripts/
COPY patches/mori/ /tmp/patches/mori/

RUN SRC_NAME=mori \
    SRC_GIT_URL="${MORI_GIT_URL}" \
    SRC_REF="${MORI_REF}" \
    SRC_DEST=/tmp/mori \
    SRC_PATCH_DIR=/tmp/patches/mori \
    SRC_SUBMODULES=1 \
    bash /tmp/scripts/clone-src.sh

RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    MORI_SRC=/tmp/mori \
    MORI_REF="${MORI_REF}" \
    MORI_WHEEL_DIR=/wheels \
    BUILD_UMBP="${MORI_BUILD_UMBP}" \
    BUILD_TESTS="${MORI_BUILD_TESTS}" \
    BUILD_EXAMPLES="${MORI_BUILD_EXAMPLES}" \
    BUILD_BENCHMARK="${MORI_BUILD_BENCHMARK}" \
    bash /tmp/scripts/build-mori.sh

# ----- 3. NIXL (ai-dynamo/nixl tag + patches/nixl/) --------------------------
FROM ucx AS nixl

ARG NIXL_GIT_URL=https://github.com/ai-dynamo/nixl.git
ARG NIXL_REF=v1.5.2
# Plugins excluded from the build: CUDA-only (GDS/GDS_MT/GPUNETIO) or needing
# vendor SDKs / cloud object stores we do not test here.
ARG NIXL_DISABLE_PLUGINS=GDS,GDS_MT,GPUNETIO,OBJ,AZURE_BLOB,INFINIA
# Extra meson options, e.g. NIXL_MESON_EXTRA_ARGS="-Dbuild_docs=true".
ARG NIXL_MESON_EXTRA_ARGS=

ENV NIXL_INSTALL_PREFIX=/opt/nixl \
    MORI_INSTALL_PREFIX=/opt/mori

RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python3 -m pip install --break-system-packages meson ninja pybind11 tomlkit

# MORI's C++ SDK (headers + libmori_io.so) so a NIXL MORI backend plugin from
# patches/nixl/ can link against MORI-IO.  This is the one edge that makes the
# nixl stage wait on the mori stage; without a MORI plugin in the patchset it
# is just an unused directory, and build-nixl.sh only passes -Dmori_path when
# the patched tree actually declares that option.
COPY --from=mori /opt/mori /opt/mori

# The source-built hipFile the AIS and AIS_MT plugins link against.  Empty when
# HIPFILE_REF is unset, in which case build-nixl.sh falls back to $ROCM_PATH and
# both plugins link the packaged libhipfile instead.
COPY --from=hipfile /opt/hipfile /opt/hipfile

# The source-built libfabric the LIBFABRIC backend links against.  Empty when
# LIBFABRIC_REF is unset, in which case build-nixl.sh leaves the plugin out of
# the build rather than falling back to the too-old distro libfabric.
COPY --from=libfabric /opt/libfabric /opt/libfabric

COPY docker/scripts/clone-src.sh docker/scripts/build-nixl.sh \
     docker/scripts/build-nixlbench.sh /tmp/scripts/

# Patches are copied immediately before use and per component, so editing a
# MORI patch does not invalidate the NIXL clone/build layers (and vice versa).
COPY patches/nixl/ /tmp/patches/nixl/

RUN SRC_NAME=nixl \
    SRC_GIT_URL="${NIXL_GIT_URL}" \
    SRC_REF="${NIXL_REF}" \
    SRC_DEST=/tmp/nixl \
    SRC_PATCH_DIR=/tmp/patches/nixl \
    bash /tmp/scripts/clone-src.sh

RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    NIXL_SRC=/tmp/nixl \
    NIXL_DISABLE_PLUGINS="${NIXL_DISABLE_PLUGINS}" \
    NIXL_MESON_EXTRA_ARGS="${NIXL_MESON_EXTRA_ARGS}" \
    bash /tmp/scripts/build-nixl.sh

# nixlbench lives inside the NIXL tree and links against the NIXL just
# installed, so it is a separate step in the same stage.  Built with
# -Duse_rocm=true, i.e. HIP device memory rather than CUDA.
RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    NIXL_SRC=/tmp/nixl \
    bash /tmp/scripts/build-nixlbench.sh

# ----- 3b. hsa-snoop (HSA queue/dispatch snooper + Prometheus exporter) ------
# Observability for the AIS path: nixlbench tells you the end-to-end number,
# hsa-snoop tells you what AIS actually did underneath it (rx/tx ops, bytes,
# errors), plus SDMA/dispatch activity and XNACK page faults.  FROM base, so it
# builds alongside the UCX/NIXL chain rather than after it.
FROM base AS hsasnoop

ARG HSA_SNOOP_GIT_URL=https://github.com/sbates130272/hsa-snoop.git
# Deliberately a branch, not a tag: this is being tracked upstream right now.
# Pin it to a tag (v1.0.0, or `latest`) once the metrics you rely on settle --
# a moving ref means two builds of the "same" image can differ.
ARG HSA_SNOOP_REF=main

COPY docker/scripts/build-hsa-snoop.sh /tmp/scripts/
RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    HSA_SNOOP_GIT_URL="${HSA_SNOOP_GIT_URL}" HSA_SNOOP_REF="${HSA_SNOOP_REF}" \
    bash /tmp/scripts/build-hsa-snoop.sh && \
    rm -rf /tmp/hsa-snoop

# ----- 4. Runtime (both stacks in one image) ---------------------------------
FROM base AS runtime

ARG ROCM_VERSION
ARG NIXL_REF
ARG MORI_REF
ARG UCX_REF
ARG LIBFABRIC_REF
ARG HIPFILE_REF
ARG FIO_REF
ARG VERSION=
# NIXL's Python API (nixl._api) does `import torch` at module scope, and MORI's
# Python-side benchmarks want it too, so a ROCm torch is installed by default.
# It is a multi-GB download: INSTALL_TORCH=0 skips it, leaving the nixlbench
# C++ path and `import mori` fully usable and only nixl._api unavailable.
#
# The rocm7.2 wheels are deliberate rather than a version mismatch: they are
# the newest published on download.pytorch.org and they run fine on the ROCm
# 7.14 runtime here (same choice rocm-aic makes).
ARG INSTALL_TORCH=1
ARG TORCH_VERSION=2.13.0+rocm7.2
ARG TORCH_INDEX_URL=https://download.pytorch.org/whl/rocm7.2

LABEL org.opencontainers.image.title="nixl-mori-test" \
      org.opencontainers.image.description="NIXL + MORI on ROCm, built from tagged releases plus local patches" \
      org.opencontainers.image.version="${VERSION}" \
      com.amd.rocm.version="${ROCM_VERSION}" \
      com.amd.nixl.ref="${NIXL_REF}" \
      com.amd.mori.ref="${MORI_REF}" \
      com.amd.ucx.ref="${UCX_REF}" \
      com.amd.hipfile.ref="${HIPFILE_REF}" \
      com.amd.libfabric.ref="${LIBFABRIC_REF}" \
      com.amd.fio.ref="${FIO_REF}"

COPY --from=ucx     /opt/rocnixl-ucx /opt/rocnixl-ucx
COPY --from=hipfile /opt/hipfile     /opt/hipfile
# Empty when LIBFABRIC_REF is unset; libplugin_LIBFABRIC.so carries an rpath to
# it, and fi_info comes along for diagnosing provider selection on a new host.
COPY --from=libfabric /opt/libfabric /opt/libfabric
# Both are symlink trees or empty when FIO_REF is unset; /opt/fio-hipfile is
# the prefix fio's rpath points at, so it has to come along with the binary.
COPY --from=fio     /opt/fio         /opt/fio
COPY --from=fio     /opt/fio-hipfile /opt/fio-hipfile
COPY --from=mori /opt/mori        /opt/mori
COPY --from=nixl /opt/nixl        /opt/nixl
COPY --from=nixl /opt/nixlbench   /opt/nixlbench
COPY --from=hsasnoop /opt/hsa-snoop /opt/hsa-snoop
COPY --from=mori /wheels          /wheels

RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    python3 -m pip install --break-system-packages numpy /wheels/amd_mori-*.whl

RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    if [ "${INSTALL_TORCH}" = "1" ]; then \
        python3 -m pip install --break-system-packages \
            --index-url "${TORCH_INDEX_URL}" "torch==${TORCH_VERSION}"; \
    else \
        echo "INSTALL_TORCH=0 -- skipping torch; nixl._api will be unavailable"; \
    fi

# NIXL is installed by meson into its own prefix (not pip), so the loader and
# interpreter are pointed at it explicitly.  NIXL_PLUGIN_DIR is where the
# backend plugins were installed; the ldconfig entries make libnixl and the
# ROCm UCX resolvable for anything that dlopens them.
ENV NIXL_PREFIX=/opt/nixl \
    NIXLBENCH_PREFIX=/opt/nixlbench \
    MORI_PREFIX=/opt/mori \
    UCX_PREFIX=/opt/rocnixl-ucx \
    NIXL_PLUGIN_DIR=/opt/nixl/lib/x86_64-linux-gnu/plugins \
    PYTHONPATH=/opt/nixl/lib/python3/dist-packages \
    FIO_PREFIX=/opt/fio \
    LIBFABRIC_PREFIX=/opt/libfabric \
    PATH=/opt/nixlbench/bin:/opt/hsa-snoop/bin:/opt/fio/bin:/opt/rocnixl-ucx/bin:/opt/libfabric/bin:${PATH}

# How many peers libfabric sizes itself for.  rxm derives the size of the MSG
# completion queue it opens under an RDM endpoint from this, and the default of
# 256 asks the verbs provider for a CQ far larger than an emulated ERNIC port
# offers: max_cqe there is 65435, and fi_enable() comes back EINVAL with
# "Unable to create verbs CQ" before a single transfer.  The per-endpoint knobs
# do not reach that term -- neither FI_OFI_RXM_MSG_RX_SIZE/TX_SIZE nor
# FI_VERBS_TX_SIZE/RX_SIZE changes the request -- so this is the one that has
# to be bounded.
#
# Raise it for a fabric with more peers than this, on hardware whose max_cqe
# can take it; it costs only the CQ depth rxm reserves per endpoint.
ENV FI_UNIVERSE_SIZE=16

# Let UCX export a dma-buf fd for ROCm memory, which is what makes a VRAM
# segment registrable with an RDMA NIC at all: without it UCX hands a device
# pointer to plain ibv_reg_mr and gets EFAULT, which reads like a NIC that
# cannot take GPU memory rather than a disabled code path.
#
# `try`, not `y`.  With `y` UCX treats a platform where dmabuf is genuinely
# unavailable as a hard error -- "ROCm dmabuf support requested but not found"
# -- and the whole worker fails to come up, so an image that defaulted to `y`
# would refuse to run anywhere dmabuf is missing.  `try` uses it when it is
# there and falls back silently when it is not, which is the right default for
# an image that runs on both.
#
# Separate from patches/ucx/03, which fixes UCX's *detection* of dmabuf on
# kernels that no longer advertise CONFIG_DMABUF_MOVE_NOTIFY.  Both are needed:
# the patch makes the answer correct, this makes UCX ask the question.
ENV UCX_ROCM_COPY_DMABUF=try

# /opt/mori/lib holds the same libmori_*.so the amd_mori wheel bundles under its
# package dir.  Both are on the search path, which is fine as long as one
# process loads only one of them -- the C++ path (nixlbench + a NIXL MORI
# plugin) uses /opt/mori, the Python path uses the wheel's copy.  Mixing them in
# a single process would load MORI twice.
#
# /opt/hipfile/lib goes first, and this file sorts before rocm.conf, so the
# source-built libhipfile wins over the one /opt/rocm packages.  The AIS plugins
# also carry an rpath to it from meson's rocm_ais_path, so this is belt and
# braces -- but anything else in the image that dlopens libhipfile should get
# the same copy the plugins were linked against.  The directory is absent when
# HIPFILE_REF was unset; ldconfig skips missing entries.
RUN printf '%s\n' \
        /opt/hipfile/lib \
        /opt/nixl/lib/x86_64-linux-gnu \
        /opt/nixl/lib \
        /opt/mori/lib \
        /opt/rocnixl-ucx/lib \
        /opt/libfabric/lib \
        > /etc/ld.so.conf.d/nixl-mori.conf && \
    ldconfig

COPY docker/scripts/smoke-test.sh /usr/local/bin/nixl-mori-smoke-test
COPY docker/scripts/run-nixlbench.sh /usr/local/bin/run-nixlbench
RUN chmod +x /usr/local/bin/nixl-mori-smoke-test /usr/local/bin/run-nixlbench

# Sanity checks at build time (no GPU in the builder): a broken link, a missing
# PYTHONPATH entry or an unresolved .so fails here rather than on the first run.
#
# `import nixl` is conditional on torch because nixl_rocm/__init__.py imports
# _api, which imports torch at module scope -- there is no lighter Python entry
# point into NIXL.  The C++ path (nixlbench + the plugins) is checked
# unconditionally, since that is what runs the benchmarks.
RUN set -eu; \
    python3 -c "import mori; print('mori OK', mori.__version__)"; \
    nixlbench --help 2>&1 | grep -q -- "-backend" && echo "nixlbench OK"; \
    hsa-snoop --help 2>&1 | grep -q -- "--prometheus" && echo "hsa-snoop OK"; \
    for p in UCX MORI_IO AIS AIS_MT POSIX; do \
        test -e "${NIXL_PLUGIN_DIR}/libplugin_$p.so" || \
            { echo "ERROR: missing NIXL plugin: $p" >&2; exit 1; }; \
        ldd "${NIXL_PLUGIN_DIR}/libplugin_$p.so" | grep "not found" && \
            { echo "ERROR: libplugin_$p.so has unresolved libs" >&2; exit 1; }; \
        echo "nixl plugin OK: $p"; \
    done; \
    if [ -e /opt/hipfile/lib/libhipfile.so ]; then \
        for p in AIS AIS_MT; do \
            ldd "${NIXL_PLUGIN_DIR}/libplugin_$p.so" | grep -q "libhipfile.*=> */opt/hipfile/lib/" || \
                { echo "ERROR: libplugin_$p.so resolves libhipfile outside /opt/hipfile:" >&2; \
                  ldd "${NIXL_PLUGIN_DIR}/libplugin_$p.so" | grep libhipfile >&2; exit 1; }; \
        done; \
        echo "hipFile OK: AIS/AIS_MT link the source-built /opt/hipfile"; \
    else \
        echo "HIPFILE_REF unset -- AIS/AIS_MT link the ROCm-packaged libhipfile"; \
    fi; \
    if [ -e /opt/libfabric/lib/libfabric.so ]; then \
        test -e "${NIXL_PLUGIN_DIR}/libplugin_LIBFABRIC.so" || \
            { echo "ERROR: libfabric is staged but libplugin_LIBFABRIC.so did not survive to the runtime stage" >&2; exit 1; }; \
        ldd "${NIXL_PLUGIN_DIR}/libplugin_LIBFABRIC.so" | grep "not found" && \
            { echo "ERROR: libplugin_LIBFABRIC.so has unresolved libs" >&2; exit 1; }; \
        ldd "${NIXL_PLUGIN_DIR}/libplugin_LIBFABRIC.so" | grep -q "libfabric.*=> */opt/libfabric/lib/" || \
            { echo "ERROR: libplugin_LIBFABRIC.so resolves libfabric outside /opt/libfabric:" >&2; \
              ldd "${NIXL_PLUGIN_DIR}/libplugin_LIBFABRIC.so" | grep libfabric >&2; exit 1; }; \
        echo "libfabric OK: LIBFABRIC links the source-built /opt/libfabric"; \
    else \
        echo "LIBFABRIC_REF unset -- no LIBFABRIC plugin in this image"; \
    fi; \
    if [ -x /opt/fio/bin/fio ]; then \
        fio --enghelp 2>&1 | grep -qE "^[[:space:]]*libhipfile[[:space:]]*$" || \
            { echo "ERROR: fio has no libhipfile ioengine" >&2; exit 1; }; \
        _fh="$(ldd /opt/fio/bin/fio | awk '/libhipfile/ {print $3}')"; \
        [ -n "${_fh}" ] || { echo "ERROR: fio does not link libhipfile" >&2; exit 1; }; \
        if [ -e /opt/hipfile/lib/libhipfile.so ]; then \
            case "$(readlink -f "${_fh}")" in \
                /opt/hipfile/*) ;; \
                *) echo "ERROR: fio resolves libhipfile to $(readlink -f "${_fh}")," \
                        "not the source-built /opt/hipfile" >&2; exit 1;; \
            esac; \
        fi; \
        echo "fio OK: $(fio --version), hipfile_mode=$(cat /opt/fio/share/fio-hipfile-async.txt)"; \
    else \
        echo "FIO_REF unset -- no fio in this image"; \
    fi; \
    if [ "${INSTALL_TORCH}" = "1" ]; then \
        python3 -c "import nixl; print('nixl._api OK', nixl.__file__)"; \
    else \
        echo "INSTALL_TORCH=0 -- skipping the nixl._api import check"; \
    fi

WORKDIR /work
CMD ["/bin/bash"]

# ----- 4b. Slim runtime ------------------------------------------------------
# The same binaries on a base that was never a build environment.
#
# `runtime` is FROM base, and base is rocm/dev-ubuntu-24.04:*-full: compilers,
# headers, static libraries and tests, none of which a benchmark run uses.  AMD
# publishes no slim 7.14 base to switch to -- only -full exists for that series
# -- and deleting the unwanted parts in a later layer would save nothing,
# because the bytes still ship in the layer underneath.  So this stage starts
# from plain Ubuntu and brings over only what the artifacts actually link.
#
# What it gives up, deliberately:
#
#   MORI's JIT.  MORI compiles its GPU kernels with hipcc on first use, and
#   hipcc is most of what was left behind.  `import mori` works and the C++ SDK
#   is here, but a kernel that is not already in ~/.mori/jit cannot be built in
#   this image.  Use the full one for MORI, or warm the cache there and mount it.
#
#   Anything that wants ROCm headers or a compiler at run time, for the same
#   reason.
#
# NIXL and nixlbench over UCX, libfabric, POSIX, AIS and hipFile are unaffected:
# they link ROCm runtime libraries, which are copied, not the toolchain.
FROM runtime AS runtime-slim-deps

# Resolve what the artifacts link and stage just those, with their symlinks.
# ldd rather than a package list: the closure is whatever the binaries name,
# which is smaller and cannot drift from what was actually built.
RUN set -eux; \
    mkdir -p /slim/opt/rocm/lib; \
    : > /tmp/libs; \
    for d in /opt/nixl /opt/nixlbench /opt/mori /opt/rocnixl-ucx \
             /opt/libfabric /opt/hipfile /opt/fio /opt/fio-hipfile \
             /opt/hsa-snoop; do \
        [ -d "$d" ] || continue; \
        find "$d" -type f \( -name '*.so*' -o -perm -u+x \) -print0 \
        | xargs -0 -r -n1 ldd 2>/dev/null \
        | awk '/=> \/opt\/rocm/ {print $3}' >> /tmp/libs || true; \
    done; \
    sort -u /tmp/libs | grep -v '^$' > /tmp/libs.u; \
    echo "ROCm libraries the artifacts link: $(wc -l < /tmp/libs.u)"; \
    # Libraries reached by dlopen have no ldd edge, so the closure above is
    # blind to them and they have to be named. MORI opens rocm_smi during
    # agent init and exits 255 without a message when it is missing, so the
    # MORI_IO backend is unusable in a slim image that omits it.
    for n in librocm_smi64.so.1 librocm_smi64.so; do \
        for d in /opt/rocm/lib /opt/rocm/lib/rocm_sysdeps/lib; do \
            [ -e "$d/$n" ] && echo "$d/$n" >> /tmp/libs.u || true; \
        done; \
    done; \
    sort -u -o /tmp/libs.u /tmp/libs.u; \
    # Follow the whole symlink chain, not just its ends. A ROCm soname
    # reaches its file through two hops -- lib/libfoo.so.N ->
    # lib/libfoo.so.N.x.y -> core-N/lib/libfoo.so.N.x.y -- and copying only
    # the first link and the final target leaves the first one dangling,
    # which ldconfig then ignores and the loader reports as "not found".
    while read -r lib; do \
        [ -e "$lib" ] || continue; \
        p="$lib"; \
        hops=0; \
        while [ -L "$p" ] && [ "$hops" -lt 16 ]; do \
            cp -a --parents "$p" /slim/ 2>/dev/null || true; \
            t=$(readlink "$p"); \
            case "$t" in /*) p="$t" ;; *) p="$(dirname "$p")/$t" ;; esac; \
            hops=$((hops + 1)); \
        done; \
        [ -e "$p" ] && cp -a --parents "$p" /slim/ 2>/dev/null || true; \
    done < /tmp/libs.u; \
    du -sh /slim

# Base is RUNTIME_BASE_IMAGE, declared at the top of this file: an ARG used in
# FROM has to be global, before the first FROM.
FROM ${RUNTIME_BASE_IMAGE} AS runtime-slim

ARG ROCM_VERSION
ARG NIXL_REF
ARG MORI_REF
ARG UCX_REF
ARG LIBFABRIC_REF
ARG VERSION=

LABEL org.opencontainers.image.title="nixl-mori-test (slim runtime)" \
      org.opencontainers.image.description="NIXL + MORI runtime artifacts without the ROCm build environment" \
      org.opencontainers.image.version="${VERSION}" \
      com.amd.rocm.version="${ROCM_VERSION}"

# Only what the artifacts need to load and run: libibverbs/librdmacm for the
# RDMA backends, numa/atomic for UCX, python3 for the bindings and the wheel.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        ca-certificates libibverbs1 librdmacm1 libnuma1 libatomic1 \
        libglib2.0-0t64 libgomp1 libelf1t64 libdrm2 libnl-3-200 \
        libnl-route-3-200 python3 python3-pip pciutils kmod iproute2 \
        libaio1t64 libdrm-amdgpu1 libgflags2.2 libhwloc15 liburing2; \
    rm -rf /var/lib/apt/lists/*

COPY --from=runtime-slim-deps /slim/opt/rocm /opt/rocm
COPY --from=ucx       /opt/rocnixl-ucx /opt/rocnixl-ucx
COPY --from=libfabric /opt/libfabric   /opt/libfabric
COPY --from=hipfile   /opt/hipfile     /opt/hipfile
COPY --from=fio       /opt/fio         /opt/fio
COPY --from=fio       /opt/fio-hipfile /opt/fio-hipfile
COPY --from=mori      /opt/mori        /opt/mori
COPY --from=nixl      /opt/nixl        /opt/nixl
COPY --from=nixl      /opt/nixlbench   /opt/nixlbench
COPY --from=hsasnoop  /opt/hsa-snoop   /opt/hsa-snoop
COPY --from=mori      /wheels          /wheels

ENV ROCM_PATH=/opt/rocm \
    NIXL_PREFIX=/opt/nixl \
    NIXL_PLUGIN_DIR=/opt/nixl/lib/x86_64-linux-gnu/plugins \
    PYTHONPATH=/opt/nixl/lib/python3/dist-packages \
    MORI_PREFIX=/opt/mori \
    UCX_PREFIX=/opt/rocnixl-ucx \
    LIBFABRIC_PREFIX=/opt/libfabric \
    FIO_PREFIX=/opt/fio \
    FI_UNIVERSE_SIZE=16 \
    UCX_ROCM_COPY_DMABUF=try \
    PATH=/opt/nixlbench/bin:/opt/hsa-snoop/bin:/opt/fio/bin:/opt/rocnixl-ucx/bin:/opt/libfabric/bin:/opt/rocm/bin:${PATH}

RUN set -eux; \
    printf '%s\n' /opt/rocm/lib /opt/rocm/lib/rocm_sysdeps/lib \
        /opt/rocnixl-ucx/lib /opt/libfabric/lib \
        /opt/mori/lib /opt/hipfile/lib /opt/nixl/lib/x86_64-linux-gnu \
        > /etc/ld.so.conf.d/nixl-mori-slim.conf; \
    ldconfig

# The same assertion the full runtime makes: a plugin that cannot resolve its
# libraries is the failure this stage is most likely to introduce, and it
# should fail the build rather than the first benchmark.
RUN set -eux; \
    for so in "${NIXL_PLUGIN_DIR}"/libplugin_*.so; do \
        [ -e "$so" ] || continue; \
        if ldd "$so" | grep -q "not found"; then \
            echo "ERROR: $so has unresolved libraries in the slim runtime:" >&2; \
            ldd "$so" | grep "not found" >&2; \
            exit 1; \
        fi; \
    done; \
    nixlbench --help >/dev/null 2>&1 || nixlbench --help 2>&1 | head -3; \
    echo "slim runtime: plugins resolve"; \
    # ldd cannot speak for the dlopen'd ones, so assert them by name.
    for n in librocm_smi64.so.1; do \
        ldconfig -p | grep -q "$n" \
            || { echo "ERROR: $n is dlopen'd at runtime but absent" >&2; exit 1; }; \
    done; \
    echo "slim runtime: dlopen'd libraries present"

WORKDIR /work
CMD ["/bin/bash"]

# ----- 5. Wheels (export only; never built by `make build`) ------------------
# `make wheels` extracts the MORI wheel to ./dist without loading an image:
#   docker build --target wheels --output type=local,dest=dist .
FROM scratch AS wheels
COPY --from=mori /wheels /
