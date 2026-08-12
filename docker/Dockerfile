# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# nixl-mori-test: NIXL + MORI built from tagged upstream releases plus local
# patches, for AMD ROCm (not CUDA).
#
#   NIXL: ai-dynamo/nixl   ${NIXL_REF} + every patch in patches/nixl/
#   MORI: ROCm/mori        ${MORI_REF} + every patch in patches/mori/
#   UCX:  ROCm/ucx         ${UCX_REF}   (source-built so NIXL's UCX backend is
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
        libibverbs-dev \
        libltdl-dev \
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

# ----- 1. UCX (ROCm/ucx, source-built with HIP memory support) ---------------
FROM base AS ucx

ARG UCX_GIT_URL=https://github.com/ROCm/ucx.git
ARG UCX_REF=v1.19.x
# UCX_FAST=1 drops logging/asserts/param-checks for a quicker dev rebuild.
ARG UCX_FAST=0

ENV UCX_PREFIX=/opt/rocnixl-ucx

COPY docker/scripts/build-ucx.sh /tmp/scripts/
RUN --mount=type=cache,target=/root/.cache,sharing=locked \
    UCX_GIT_URL="${UCX_GIT_URL}" UCX_REF="${UCX_REF}" UCX_FAST="${UCX_FAST}" \
    bash /tmp/scripts/build-ucx.sh && \
    rm -rf /tmp/ucx-src

# ----- 2. MORI (ROCm/mori tag + patches/mori/) -------------------------------
# FROM base, not FROM ucx: MORI needs neither UCX nor NIXL, so this stage runs
# concurrently with the UCX build.  It is placed before the nixl stage because
# nixl copies MORI's C++ SDK out of it.
FROM base AS mori

ARG MORI_GIT_URL=https://github.com/ROCm/mori.git
ARG MORI_REF=v1.2.2
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
ARG NIXL_REF=v1.3.2
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

# ----- 4. Runtime (both stacks in one image) ---------------------------------
FROM base AS runtime

ARG ROCM_VERSION
ARG NIXL_REF
ARG MORI_REF
ARG UCX_REF
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
      com.amd.ucx.ref="${UCX_REF}"

COPY --from=ucx  /opt/rocnixl-ucx /opt/rocnixl-ucx
COPY --from=mori /opt/mori        /opt/mori
COPY --from=nixl /opt/nixl        /opt/nixl
COPY --from=nixl /opt/nixlbench   /opt/nixlbench
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
    PATH=/opt/nixlbench/bin:/opt/rocnixl-ucx/bin:${PATH}

# /opt/mori/lib holds the same libmori_*.so the amd_mori wheel bundles under its
# package dir.  Both are on the search path, which is fine as long as one
# process loads only one of them -- the C++ path (nixlbench + a NIXL MORI
# plugin) uses /opt/mori, the Python path uses the wheel's copy.  Mixing them in
# a single process would load MORI twice.
RUN printf '%s\n' \
        /opt/nixl/lib/x86_64-linux-gnu \
        /opt/nixl/lib \
        /opt/mori/lib \
        /opt/rocnixl-ucx/lib \
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
    for p in UCX MORI_IO AIS_MT POSIX; do \
        test -e "${NIXL_PLUGIN_DIR}/libplugin_$p.so" || \
            { echo "ERROR: missing NIXL plugin: $p" >&2; exit 1; }; \
        ldd "${NIXL_PLUGIN_DIR}/libplugin_$p.so" | grep "not found" && \
            { echo "ERROR: libplugin_$p.so has unresolved libs" >&2; exit 1; }; \
        echo "nixl plugin OK: $p"; \
    done; \
    if [ "${INSTALL_TORCH}" = "1" ]; then \
        python3 -c "import nixl; print('nixl._api OK', nixl.__file__)"; \
    else \
        echo "INSTALL_TORCH=0 -- skipping the nixl._api import check"; \
    fi

WORKDIR /work
CMD ["/bin/bash"]

# ----- 5. Wheels (export only; never built by `make build`) ------------------
# `make wheels` extracts the MORI wheel to ./dist without loading an image:
#   docker build --target wheels --output type=local,dest=dist .
FROM scratch AS wheels
COPY --from=mori /wheels /
