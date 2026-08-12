# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# nixl-mori-test: build NIXL + MORI from tagged upstream releases plus the
# local patchsets in patches/, for AMD ROCm (not CUDA).
#
# This tree is self-contained: the build context and all sources live here, so
# "repo root" is this directory (no dependency on any parent checkout).
REPO_ROOT := $(CURDIR)

# ---- Component pins ---------------------------------------------------------
# Each ref is a tagged upstream release; local changes go in patches/<name>/ and
# are applied to the fresh checkout in lexical order.  Keep these in sync with
# the ARG defaults in docker/Dockerfile -- the values here are passed as build
# args, and docker/scripts/image-tag.sh reads the Dockerfile when they are not.
ROCM_VERSION ?= 7.14.0
NIXL_GIT_URL ?= https://github.com/ai-dynamo/nixl.git
NIXL_REF     ?= v1.3.2
MORI_GIT_URL ?= https://github.com/ROCm/mori.git
MORI_REF     ?= v1.2.2
UCX_GIT_URL  ?= https://github.com/ROCm/ucx.git
UCX_REF      ?= v1.19.x

override VERSION := $(strip $(file <$(REPO_ROOT)/VERSION))

# ---- Image naming -----------------------------------------------------------
IMAGE_NAME ?= nixl-mori-test
DOCKERFILE := $(REPO_ROOT)/docker/Dockerfile

# The tag encodes every component version, so two builds that differ in any pin
# cannot collide.  Command-line overrides of the pins above are exported into
# the tag script, which otherwise falls back to the Dockerfile ARG defaults.
_TAG_ARGS := ROCM_VERSION NIXL_REF MORI_REF
_single_quote := '
_shell_quote = '$(subst $(_single_quote),'"'"',$(1))'
_TAG_ENV := $(foreach _a,$(_TAG_ARGS),$(if $(filter undefined,$(origin $(_a))),,$(_a)=$(call _shell_quote,$(value $(_a)))))

_IMAGE_TAG := $(shell $(_TAG_ENV) $(REPO_ROOT)/docker/scripts/image-tag.sh 2>/dev/null)
IMAGE_TAG  ?= $(if $(_IMAGE_TAG),$(_IMAGE_TAG),latest)
IMAGE_REF  := $(IMAGE_NAME):$(IMAGE_TAG)

# ---- Build knobs ------------------------------------------------------------
# ROCM_ARCH is baked into MORI's GPU_TARGETS.  Auto-detected from this host;
# set it explicitly when building for a different GPU than the build node's.
_ROCM_ARCH_DETECTED := $(shell rocm_agent_enumerator 2>/dev/null | grep -E '^gfx' | head -1)
ROCM_ARCH := $(if $(strip $(ROCM_ARCH)),$(strip $(ROCM_ARCH)),$(_ROCM_ARCH_DETECTED))

# Cap parallel compile jobs in the image build; empty = all cores ($(nproc)).
BUILD_JOBS ?=
# UCX_FAST=1 drops UCX logging/asserts for a quicker dev rebuild.
UCX_FAST ?= 0
# Path to a corporate CA (e.g. Zscaler).  Passed as a BuildKit secret, never
# as a build arg, so it is not baked into the image.
TLS_CERT ?=
# NO_CACHE=1 forces a full rebuild.
NO_CACHE ?=
# PROGRESS=plain streams full build output instead of the collapsed TTY view.
PROGRESS ?= auto

# INSTALL_TORCH=0 skips the multi-GB ROCm torch wheel in the runtime stage.
# nixlbench and `import mori` still work; only nixl._api (which does
# `import torch` at module scope) becomes unavailable.
INSTALL_TORCH ?= 1

# Literal comma, so --secret id=...,src=... survives make's argument splitting.
comma := ,

_BUILD_ARGS := \
	--build-arg ROCM_VERSION=$(ROCM_VERSION) \
	--build-arg ROCM_ARCH=$(ROCM_ARCH) \
	--build-arg BUILD_JOBS=$(BUILD_JOBS) \
	--build-arg NIXL_GIT_URL=$(NIXL_GIT_URL) \
	--build-arg NIXL_REF=$(NIXL_REF) \
	--build-arg MORI_GIT_URL=$(MORI_GIT_URL) \
	--build-arg MORI_REF=$(MORI_REF) \
	--build-arg UCX_GIT_URL=$(UCX_GIT_URL) \
	--build-arg UCX_REF=$(UCX_REF) \
	--build-arg UCX_FAST=$(UCX_FAST) \
	--build-arg VERSION=$(VERSION) \
	--build-arg INSTALL_TORCH=$(INSTALL_TORCH) \
	$(if $(ROCM_BASE_IMAGE),--build-arg ROCM_BASE_IMAGE=$(ROCM_BASE_IMAGE),) \
	$(if $(NIXL_DISABLE_PLUGINS),--build-arg NIXL_DISABLE_PLUGINS=$(NIXL_DISABLE_PLUGINS),) \
	$(if $(NIXL_MESON_EXTRA_ARGS),--build-arg NIXL_MESON_EXTRA_ARGS=$(NIXL_MESON_EXTRA_ARGS),) \
	$(if $(MORI_BUILD_UMBP),--build-arg MORI_BUILD_UMBP=$(MORI_BUILD_UMBP),) \
	$(if $(TLS_CERT),--secret id=tls_cert$(comma)src=$(TLS_CERT),) \
	$(if $(NO_CACHE),--no-cache,)

DOCKER_BUILD := DOCKER_BUILDKIT=1 docker build --progress=$(PROGRESS) -f $(DOCKERFILE)

# ---- Container run flags ----------------------------------------------------
# ROCm needs /dev/kfd + /dev/dri and the render/video groups; RDMA (NIXL UCX,
# MORI IBGDA) additionally needs the verbs devices, and host networking + IPC
# for multi-process runs.
#
# IPC_LOCK *and* an unlimited memlock ulimit are both required, and the second
# is the one that is easy to miss: docker defaults memlock to 64 KiB, so a
# GPUDirect registration of a multi-GiB buffer fails with a bare
# "failed to register address ... Input/output error" from UCX and the run
# quietly falls back to a slow transport.
_GROUP_ADDS := $(shell for g in video render; do getent group $$g >/dev/null && echo --group-add $$g; done)
DOCKER_RUN_FLAGS ?= \
	--rm \
	--device=/dev/kfd \
	--device=/dev/dri \
	$(if $(wildcard /dev/infiniband),--device=/dev/infiniband,) \
	$(_GROUP_ADDS) \
	--cap-add=IPC_LOCK \
	--ulimit memlock=-1:-1 \
	--cap-add=SYS_PTRACE \
	--security-opt seccomp=unconfined \
	--ipc=host \
	--network=host \
	--shm-size=16g \
	-v $(CURDIR):/work \
	-w /work

# Component to act on for patch-check / patch-new: nixl, mori, or all.
COMPONENT ?= all

# ---- Slurm ------------------------------------------------------------------
# The build is heavy and needs no GPU, so it belongs on a CPU-only compute node
# rather than the shared login node.  .slurm/run-build.sh submits it and streams
# the log back; NM_* knobs are documented in that script's header.
DIST := $(REPO_ROOT)/.slurm/run-build.sh

.PHONY: help build build-nixl build-mori wheels shell test test-nogpu \
        nixlbench bench bench-nvme bench-compare dist-build dist-load \
        dist-build-here dist-bench dist-bench-2node \
        patch-check patch-list print-tag print-config clean clean-images

.DEFAULT_GOAL := help

help:
	@echo "nixl-mori-test — NIXL + MORI on ROCm, from tagged releases plus local patches"
	@echo ""
	@echo "Build targets:"
	@echo "  make build           Build the combined image ($(IMAGE_REF))"
	@echo "  make build-nixl      Build only through the NIXL stage (UCX + NIXL)"
	@echo "  make build-mori      Build only the MORI stage"
	@echo "  make wheels          Export the MORI wheel to ./dist (no image load)"
	@echo ""
	@echo "Patch targets (patches/<component>/*.patch, applied in lexical order):"
	@echo "  make patch-list      List the patchsets that will be applied"
	@echo "  make patch-check     Dry-run every patchset against its pinned tag"
	@echo "                       (host-side clone into build/patch-check/; no image build)"
	@echo "                       Narrow it: make patch-check COMPONENT=nixl"
	@echo ""
	@echo "Slurm targets (the build needs no GPU — keep it off the login node):"
	@echo "  make dist-build      Build on a CPU-only node, save the tarball to"
	@echo "                       /scratch/\$$USER/nixl-mori-images/ (log -> logs/)"
	@echo "  make dist-build-here Build ON the benchmark node ($(BENCH_PARTITION)), no tarball —"
	@echo "                       faster, and skips the 68 GB /scratch round trip"
	@echo "  make dist-bench      Run nixlbench (UCX + MORI_IO) on a GPU node"
	@echo "  make dist-bench-2node  Same, initiator and target on two nodes"
	@echo "  make dist-load NM_TARGETS=<node>[,<node>]"
	@echo "                       docker load that tarball on each target node"
	@echo "                       Knobs (NM_BUILD_PARTITION, NM_ROCM_ARCH, ...) are"
	@echo "                       documented in .slurm/run-build.sh"
	@echo ""
	@echo "Run targets:"
	@echo "  make shell           Interactive shell in the image (GPUs + RDMA passed through)"
	@echo "  make test            Smoke test in the image: imports, NIXL plugins, MORI devices"
	@echo "  make test-nogpu      Import/plugin checks only (no /dev/kfd needed)"
	@echo "  make nixlbench ARGS='...'   Run nixlbench in the image (ROCm build)"
	@echo "  make bench BACKEND=UCX|MORI_IO [SEG_TYPE=VRAM|DRAM]"
	@echo "                       Two-process nixlbench pair over the ASIO runtime"
	@echo "  make bench-compare   Same run for UCX then MORI_IO, back to back"
	@echo "  make bench ... HSA_SNOOP=1   Also collect GPU/AIS counters via hsa-snoop"
	@echo "  make bench-nvme [NVME_PATH=/mnt/nixl-nvme-0/... POSIX_API=AIO|URING]"
	@echo "                       NVMe via NIXL's POSIX backend, O_DIRECT"
	@echo ""
	@echo "Introspection:"
	@echo "  make print-tag       Print the derived image tag"
	@echo "  make print-config    Print every resolved pin and build knob"
	@echo "  make clean           Remove build/ (patch-check clones) and dist/"
	@echo "  make clean-images    Remove local $(IMAGE_NAME) images"
	@echo ""
	@echo "Component pins (override on the command line):"
	@echo "  ROCM_VERSION=$(ROCM_VERSION)"
	@echo "  NIXL_REF=$(NIXL_REF)   ($(NIXL_GIT_URL))"
	@echo "  MORI_REF=$(MORI_REF)   ($(MORI_GIT_URL))"
	@echo "  UCX_REF=$(UCX_REF)     ($(UCX_GIT_URL))"
	@echo ""
	@echo "Build knobs:"
	@echo "  ROCM_ARCH=$(ROCM_ARCH)   GPU arch baked into MORI (auto-detected)"
	@echo "  BUILD_JOBS=$(BUILD_JOBS)         Cap parallel compile jobs (empty = all cores)"
	@echo "  UCX_FAST=$(UCX_FAST)           1 = drop UCX logging/asserts for a faster rebuild"
	@echo "  TLS_CERT=$(TLS_CERT)           Corporate CA, passed as a BuildKit secret"
	@echo "  NO_CACHE=$(NO_CACHE)           1 = --no-cache"
	@echo "  PROGRESS=$(PROGRESS)        plain = stream full build output"
	@echo ""
	@echo "Examples:"
	@echo "  make build"
	@echo "  make build NIXL_REF=v1.3.1 MORI_REF=v1.2.1"
	@echo "  make build TLS_CERT=/etc/ssl/certs/zscaler-ca.crt BUILD_JOBS=16"
	@echo "  make patch-check COMPONENT=mori"
	@echo "  make test"
	@echo ""

# ---- Build ------------------------------------------------------------------

build:
	@test -n "$(ROCM_ARCH)" || echo "WARNING: ROCM_ARCH empty (no ROCm on this host?) — MORI will use its default arch list" >&2
	$(DOCKER_BUILD) $(_BUILD_ARGS) --target runtime -t "$(IMAGE_REF)" "$(REPO_ROOT)"
	@docker tag "$(IMAGE_REF)" "$(IMAGE_NAME):latest"
	@echo "Built $(IMAGE_REF) (also tagged $(IMAGE_NAME):latest)"

build-nixl:
	$(DOCKER_BUILD) $(_BUILD_ARGS) --target nixl -t "$(IMAGE_NAME):nixl-$(NIXL_REF)" "$(REPO_ROOT)"
	@echo "Built $(IMAGE_NAME):nixl-$(NIXL_REF)"

build-mori:
	$(DOCKER_BUILD) $(_BUILD_ARGS) --target mori -t "$(IMAGE_NAME):mori-$(MORI_REF)" "$(REPO_ROOT)"
	@echo "Built $(IMAGE_NAME):mori-$(MORI_REF)"

# --output writes the scratch `wheels` stage straight to the host; no image is
# created or loaded.
wheels:
	@mkdir -p "$(REPO_ROOT)/dist"
	$(DOCKER_BUILD) $(_BUILD_ARGS) --target wheels \
		--output type=local,dest="$(REPO_ROOT)/dist" "$(REPO_ROOT)"
	@ls -1 "$(REPO_ROOT)/dist"

# ---- Patches ----------------------------------------------------------------

patch-list:
	@for c in ucx nixl mori; do \
		case $$c in ucx) r=$(UCX_REF);; nixl) r=$(NIXL_REF);; mori) r=$(MORI_REF);; esac; \
		echo "patches/$$c/  ($$c $$r)"; \
		found=0; \
		for p in patches/$$c/*.patch; do \
			[ -e "$$p" ] || continue; found=1; echo "    $$(basename $$p)"; \
		done; \
		[ $$found -eq 1 ] || echo "    (none — pristine tag)"; \
	done

patch-check:
	@COMPONENT="$(COMPONENT)" NIXL_REF="$(NIXL_REF)" NIXL_GIT_URL="$(NIXL_GIT_URL)" \
		MORI_REF="$(MORI_REF)" MORI_GIT_URL="$(MORI_GIT_URL)" \
		"$(REPO_ROOT)/docker/scripts/patch-check.sh"

# ---- Run --------------------------------------------------------------------

shell:
	docker run -it $(DOCKER_RUN_FLAGS) "$(IMAGE_REF)" /bin/bash

test:
	docker run $(DOCKER_RUN_FLAGS) "$(IMAGE_REF)" nixl-mori-smoke-test

test-nogpu:
	docker run --rm -v "$(CURDIR)":/work -w /work -e SMOKE_GPU=0 \
		"$(IMAGE_REF)" nixl-mori-smoke-test

# Pass benchmark flags through: make nixlbench ARGS="--backend UCX ..."
ARGS ?= --help
nixlbench:
	docker run -it $(DOCKER_RUN_FLAGS) "$(IMAGE_REF)" nixlbench $(ARGS)

# Two-process nixlbench pair (initiator + target) over the ASIO socket runtime.
# The ONLY difference between the two backends is this flag, so the numbers are
# directly comparable: same buffers, same warmup, same timing code.
BACKEND  ?= UCX
SEG_TYPE ?= VRAM
# FILEPATH selects a storage target for the POSIX backend; it is bind-mounted
# into the container at the same path so the flag nixlbench sees is the flag
# you typed.
# HSA_SNOOP=1 runs hsa-snoop alongside the benchmark.  It installs kprobes and
# reads other processes' memory, which needs --privileged: CAP_SYS_ADMIN plus a
# tracefs mount is NOT enough (hsa-snoop reports "failed to install kprobe
# (Permission denied)" and then "failed to start discovery", while still
# serving an empty /metrics — so it looks like it is working).  Kept out of
# DOCKER_RUN_FLAGS so ordinary runs are not privileged.
HSA_SNOOP ?=
_SNOOP_FLAGS = $(if $(HSA_SNOOP),-e HSA_SNOOP=1 --privileged --pid=host \
	-v /sys/kernel/tracing:/sys/kernel/tracing $(if $(HSA_SNOOP_PORT),-e HSA_SNOOP_PORT=$(HSA_SNOOP_PORT),),)

bench:
	docker run $(DOCKER_RUN_FLAGS) $(_SNOOP_FLAGS) \
		$(if $(FILEPATH),-v $(FILEPATH):$(FILEPATH) -e FILEPATH=$(FILEPATH),) \
		$(if $(POSIX_API),-e POSIX_API=$(POSIX_API),) \
		$(if $(DIRECT_IO),-e DIRECT_IO=$(DIRECT_IO),) \
		-e BACKEND=$(BACKEND) -e SEG_TYPE=$(SEG_TYPE) \
		$(if $(BENCH_EXTRA),-e EXTRA_ARGS='$(BENCH_EXTRA)',) \
		"$(IMAGE_REF)" run-nixlbench

# NVMe through NIXL's POSIX backend.  FILEPATH must be a directory on the drive
# under test; the node's drives are mounted at /mnt/nixl-nvme-N.
NVME_PATH ?= /mnt/nixl-nvme-0/nixlbench-$(USER)
bench-nvme:
	@test -d "$(dir $(NVME_PATH))" || { echo "ERROR: $(dir $(NVME_PATH)) does not exist — is this an NVMe node?" >&2; exit 1; }
	@mkdir -p "$(NVME_PATH)"
	$(MAKE) --no-print-directory bench BACKEND=POSIX SEG_TYPE=DRAM \
		FILEPATH="$(NVME_PATH)" POSIX_API=$(if $(POSIX_API),$(POSIX_API),AIO)

bench-compare:                 # UCX then MORI_IO, same settings, back to back
	@$(MAKE) --no-print-directory bench BACKEND=UCX      SEG_TYPE=$(SEG_TYPE) || true
	@echo
	@$(MAKE) --no-print-directory bench BACKEND=MORI_IO  SEG_TYPE=$(SEG_TYPE) || true

# Real numbers need real hardware: the login node has one GPU and no NIC, so
# `make bench` there only ever measures loopback.  dist-bench submits the pair
# to GPU+RDMA nodes (see .slurm/run-bench.sh for the NM_* knobs).
DIST_BENCH := $(REPO_ROOT)/.slurm/run-bench.sh

dist-bench:                    # nixlbench on a GPU node via Slurm (UCX + MORI_IO)
	NM_SEG_TYPE="$(SEG_TYPE)" $(DIST_BENCH)

dist-bench-2node:              # initiator and target on two nodes (exercises RDMA)
	NM_BENCH_NODES=2 NM_SEG_TYPE="$(SEG_TYPE)" $(DIST_BENCH)

# ---- Slurm (build off the login node) ---------------------------------------

# ROCM_ARCH is forwarded only when it was set explicitly.  The value autodetected
# on the login node describes the login node, which is not the machine the image
# will run on -- letting that leak into a distributed build is how you get an
# image compiled for the wrong GPU.  Unset, run-build.sh applies its own default.
_DIST_ARCH := $(if $(filter command line environment,$(origin ROCM_ARCH)),$(ROCM_ARCH),$(NM_ROCM_ARCH))

dist-build:                    # Build on a CPU-only Slurm node, save the tarball
	$(if $(_DIST_ARCH),NM_ROCM_ARCH="$(_DIST_ARCH)",) \
		MAKE_ARGS="$(MAKE_ARGS)" $(DIST) build

# Build directly on the GPU node we benchmark on, and skip the tarball.  That
# node has 384 cores and is where the image is needed, so this is both faster
# to build and saves a 68 GB round trip through /scratch.  Use dist-build
# instead when the image has to reach more than one node.
BENCH_PARTITION ?= storage
dist-build-here:               # Build on the benchmark node itself, no tarball
	NM_BUILD_PARTITION="$(BENCH_PARTITION)" NM_BUILD_CONSTRAINT= \
		NM_BUILD_CPUS=64 NM_SKIP_SAVE=1 \
		NM_ROCM_ARCH="$(if $(_DIST_ARCH),$(_DIST_ARCH),gfx942)" \
		MAKE_ARGS="$(MAKE_ARGS)" $(DIST) build

dist-load:                     # docker load the saved tarball onto NM_TARGETS
	@test -n "$(NM_TARGETS)" || { echo "ERROR: set NM_TARGETS=<node>[,<node>...]" >&2; exit 1; }
	NM_TARGETS="$(NM_TARGETS)" MAKE_ARGS="$(MAKE_ARGS)" $(DIST) load

# ---- Housekeeping -----------------------------------------------------------

print-tag:
	@echo "$(IMAGE_REF)"

print-config:
	@echo "VERSION        = $(VERSION)"
	@echo "IMAGE_REF      = $(IMAGE_REF)"
	@echo "ROCM_VERSION   = $(ROCM_VERSION)"
	@echo "ROCM_ARCH      = $(ROCM_ARCH)"
	@echo "NIXL           = $(NIXL_GIT_URL) @ $(NIXL_REF)"
	@echo "MORI           = $(MORI_GIT_URL) @ $(MORI_REF)"
	@echo "UCX            = $(UCX_GIT_URL) @ $(UCX_REF)"
	@echo "BUILD_JOBS     = $(BUILD_JOBS)"
	@echo "TLS_CERT       = $(TLS_CERT)"

clean:
	rm -rf "$(REPO_ROOT)/build" "$(REPO_ROOT)/dist"

clean-images:
	-docker images --format '{{.Repository}}:{{.Tag}}' | grep '^$(IMAGE_NAME):' | xargs -r docker rmi
