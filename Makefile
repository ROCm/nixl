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
# THE DEFAULTS LIVE IN docker-bake.hcl, NOT HERE. This file reads them.
#
# They used to be written out in both places, and they drifted: LIBFABRIC_REF
# was bumped to v2.7.0 in docker-bake.hcl and the Dockerfile and left at
# v1.22.0 here, so `docker buildx bake` and `make build` compiled different
# libfabrics from the same tree, and the generated pin table -- which reads
# this file -- advertised the version nothing was running. Nothing failed; the
# two builds just quietly disagreed.
#
# `make pins-check` enforces it. Each ref is a tagged upstream release; local
# changes go in patches/<name>/ and are applied to the fresh checkout in
# lexical order.
DOCKER_BAKE := $(REPO_ROOT)/docker-bake.hcl

# One pass over the bake file into NAME=value words, so a pin costs no extra
# process. Values with whitespace would break the word split; none have any,
# and pins-check fails if that changes.
_BAKE_DEFAULTS := $(shell sed -n 's/^variable[[:space:]]*"\([A-Z_0-9]*\)"[[:space:]]*{[[:space:]]*default[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1=\2/p' $(DOCKER_BAKE))
bake_default = $(patsubst $(1)=%,%,$(filter $(1)=%,$(_BAKE_DEFAULTS)))

# ?= so the command line still wins: `make build NIXL_REF=v1.5.0` is unchanged.
$(foreach _v,ROCM_VERSION NIXL_REF MORI_REF UCX_REF LIBFABRIC_REF HIPFILE_REF FIO_REF PERFTEST_REF IMAGE_NAME,\
  $(eval $(_v) ?= $(call bake_default,$(_v))))

# Not in bake: it builds from a context, and these only matter to the clone
# step. They stay here.
NIXL_GIT_URL ?= https://github.com/ai-dynamo/nixl.git
MORI_GIT_URL ?= https://github.com/ROCm/mori.git
UCX_GIT_URL  ?= https://github.com/openucx/ucx.git
# libfabric (OFI), for NIXL's LIBFABRIC backend -- a second RDMA transport to
# put beside UCX.  Source-built because Ubuntu 24.04 ships 1.17 and the NIXL
# plugin's floor is 1.21.0.  Empty leaves the plugin out of the image.
LIBFABRIC_GIT_URL ?= https://github.com/ofiwg/libfabric.git
# hipFile is the odd one out: ROCm/hipFile publishes no releases, its `nightly`
# git tag has been frozen since 2026-01-22 and the .deb assets on the "Nightly"
# GitHub release since 2026-06-08 -- all three older than the 0.3.0 the ROCm
# base image packages.  The live stream is the develop branch, so the pin is a
# SHA on it rather than a tag.  Set it empty to fall back to the packaged
# libhipfile; the AIS backend then drops out of the build.
HIPFILE_GIT_URL ?= https://github.com/ROCm/hipFile.git
# fio, for a hipFile number with no NIXL in it.  The ROCm fork rather than
# axboe/fio: its libhipfile engine takes hipfile_mode=sync|stream|batch, which
# is the same three submission paths AIS_MT, AIS and hipFile's stubbed batch
# API use, so it can say whether a plateau is NIXL's or the library's.  The
# branch publishes no tags, hence a SHA.  Empty leaves fio out of the image.
FIO_GIT_URL ?= https://github.com/ROCm/fio.git
# perftest, the reference RDMA microbenchmark. The fork rather than
# linux-rdma/perftest: the ROCm data-validation work is only there.
PERFTEST_GIT_URL ?= https://github.com/sbates130272/perftest.git

override VERSION := $(strip $(file <$(REPO_ROOT)/VERSION))

# ---- Image naming -----------------------------------------------------------
DOCKERFILE := $(REPO_ROOT)/docker/Dockerfile

# The tag encodes every component version, so two builds that differ in any pin
# cannot collide.  Command-line overrides of the pins above are exported into
# the tag script, which otherwise falls back to the Dockerfile ARG defaults.
_TAG_ARGS := ROCM_VERSION NIXL_REF MORI_REF HIPFILE_REF FIO_REF
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
	--build-arg LIBFABRIC_GIT_URL=$(LIBFABRIC_GIT_URL) \
	--build-arg LIBFABRIC_REF=$(LIBFABRIC_REF) \
	--build-arg HIPFILE_GIT_URL=$(HIPFILE_GIT_URL) \
	--build-arg HIPFILE_REF=$(HIPFILE_REF) \
	--build-arg FIO_GIT_URL=$(FIO_GIT_URL) \
	--build-arg PERFTEST_REF=$(PERFTEST_REF) \
	--build-arg PERFTEST_GIT_URL=$(PERFTEST_GIT_URL) \
	--build-arg FIO_REF=$(FIO_REF) \
	$(if $(UCX_DEBUG_LOG),--build-arg UCX_DEBUG_LOG=$(UCX_DEBUG_LOG),) \
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
# Numeric GIDs, not names: docker resolves a --group-add name against the
# *container's* /etc/group, and the two do not have to agree.  On
# ctr-smc-mi300x-cx68-25 the host's render group is 109 while the image's is
# 994, so --group-add render granted a group that owns nothing, /dev/kfd stayed
# unopenable, and every ROCm tool reported "no ROCm-capable device is detected"
# on a node with eight MI300X in it.
_GROUP_ADDS := $(shell for g in video render; do getent group $$g | cut -d: -f3 | sed 's/^/--group-add /'; done)
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
        nixlbench bench bench-nvme bench-compare storage-sweep \
        snoop-sweep snoop-up snoop-down monitor-up monitor-down snoop-dump \
        fio-sweep dist-build dist-load \
        dist-build-here dist-bench dist-bench-2node \
        cluster-tool cluster-gen cluster-up cluster-down cluster-status \
        cluster-images cluster-boot cluster-ansible cluster-prep cluster-gpu ernic-image \
        cluster-nvme cluster-stack cluster-s3 cluster-s3-restore \
        patch-check patch-list readme readme-check \
        check pins-check print-pin \
        print-tag print-config clean clean-images

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
	@echo "  make storage-sweep SWEEP_SET=quick|rw|threads|drives|wide|readscale|direct|full"
	@echo "                       AIS vs AIS_MT vs POSIX across the node's NVMe drives -> logs/*.csv"
	@echo "  make fio-sweep FIO_SET=quick|modes|bs|depth|drives|full"
	@echo "                       Same drives via fio's libhipfile engine — NIXL-free"
	@echo "                       ground truth for the AIS numbers -> logs/*.csv"
	@echo ""
	@echo "Monitoring (docker/compose/bench-stack.yml):"
	@echo "  make snoop-up        hsa-snoop collector only, left running"
	@echo "  make monitor-up      + Prometheus, Grafana and the host exporters"
	@echo "  make snoop-dump [RUN_TAG=... PROM_START=<epoch> PROM_END=<epoch>]"
	@echo "                       Prometheus -> CSV under logs/ (the TSDB is scratch)"
	@echo "  make monitor-down / make snoop-down"
	@echo ""
	@echo "Introspection:"
	@echo "  make print-tag       Print the derived image tag"
	@echo "  make print-config    Print every resolved pin and build knob"
	@echo "  make readme          Regenerate the README pin table from the pins"
	@echo "  make readme-check    Fail if that table is stale"
	@echo "  make clean           Remove build/ (patch-check clones) and dist/"
	@echo "  make clean-images    Remove local $(IMAGE_NAME) images"
	@echo ""
	@echo "Component pins (override on the command line):"
	@echo "  ROCM_VERSION=$(ROCM_VERSION)"
	@echo "  NIXL_REF=$(NIXL_REF)   ($(NIXL_GIT_URL))"
	@echo "  MORI_REF=$(MORI_REF)   ($(MORI_GIT_URL))"
	@echo "  UCX_REF=$(UCX_REF)     ($(UCX_GIT_URL))"
	@echo "  LIBFABRIC_REF=$(LIBFABRIC_REF) ($(LIBFABRIC_GIT_URL))"
	@echo "  HIPFILE_REF=$(HIPFILE_REF) ($(HIPFILE_GIT_URL))"
	@echo "  FIO_REF=$(FIO_REF) ($(FIO_GIT_URL))"
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
	@for c in ucx hipfile nixl mori; do \
		case $$c in ucx) r=$(UCX_REF);; hipfile) r=$(HIPFILE_REF);; nixl) r=$(NIXL_REF);; mori) r=$(MORI_REF);; esac; \
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
		HIPFILE_REF="$(HIPFILE_REF)" HIPFILE_GIT_URL="$(HIPFILE_GIT_URL)" \
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

# BENCH_ENV passes arbitrary NAME=VALUE pairs into the container, which is how
# UCX knobs get set for a run: UCX reads its configuration from the environment
# of the process that opens the context, so setting them outside `docker run`
# has no effect at all.
#   make bench BACKEND=UCX BENCH_ENV="UCX_TLS=rocm_ipc,rocm_copy,tcp,sysv,posix,cma"
BENCH_ENV ?=
_BENCH_ENV_FLAGS = $(foreach v,$(BENCH_ENV),-e '$(v)')

bench:
	docker run $(DOCKER_RUN_FLAGS) $(_SNOOP_FLAGS) $(_BENCH_ENV_FLAGS) \
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

# Sweep the storage backends across a config matrix (see the script header for
# what each set answers).  /mnt is bind-mounted whole rather than one drive at a
# time, because the point of the multi-drive rows is to spread one transfer
# across sixteen block devices.
SWEEP_SET ?= quick
SWEEP_OUT ?= /work/logs/storage-sweep-$(SWEEP_SET).csv
storage-sweep:                 # AIS vs AIS_MT vs POSIX across drives/threads/ops
	@mkdir -p logs
	docker run $(DOCKER_RUN_FLAGS) -v /mnt:/mnt \
		-e SWEEP_SET=$(SWEEP_SET) -e SWEEP_OUT=$(SWEEP_OUT) \
		$(if $(SWEEP_ITER),-e SWEEP_ITER=$(SWEEP_ITER),) \
		$(if $(TIMEOUT),-e TIMEOUT=$(TIMEOUT),) \
		"$(IMAGE_REF)" bash /work/docker/scripts/storage-sweep.sh

# The same drives, through fio's libhipfile engine instead of NIXL -- the
# control for every AIS number storage-sweep produces.  See fio-sweep.sh.
FIO_SET ?= quick
FIO_OUT ?= /work/logs/fio-sweep.csv
fio-sweep:                     # hipFile via fio: NIXL-free ground truth
	@mkdir -p logs
	docker run $(DOCKER_RUN_FLAGS) -v /mnt:/mnt \
		-e FIO_SET=$(FIO_SET) -e FIO_OUT=$(FIO_OUT) \
		$(if $(FIO_SIZE),-e FIO_SIZE=$(FIO_SIZE),) \
		$(if $(FIO_RUNTIME),-e FIO_RUNTIME=$(FIO_RUNTIME),) \
		$(if $(TIMEOUT),-e TIMEOUT=$(TIMEOUT),) \
		"$(IMAGE_REF)" bash /work/docker/scripts/fio-sweep.sh

# The same sweeps, but with an hsa-snoop sidecar tracing underneath all of them
# for the whole session.  HSA_SNOOP=1 on `make bench` cannot do this: it starts
# a collector inside the benchmark container, and a sweep is forty-odd
# containers, so every counter would reset at every sweep point.  See
# docker/compose/bench-stack.yml.
SWEEP_SETS ?= quick
snoop-sweep:                   # storage-sweep sets with a persistent hsa-snoop sidecar
	@mkdir -p logs
	IMAGE_REF="$(IMAGE_REF)" SWEEP_SETS="$(SWEEP_SETS)" \
		$(if $(RUN_TAG),RUN_TAG=$(RUN_TAG),) \
		bash $(REPO_ROOT)/.slurm/snoop-sweep.sh

snoop-up:                      # bring up just the collector, leave it running
	IMAGE_REF="$(IMAGE_REF)" docker compose -f docker/compose/bench-stack.yml up -d hsa-snoop

snoop-down:
	IMAGE_REF="$(IMAGE_REF)" docker compose -f docker/compose/bench-stack.yml down --remove-orphans

# Prometheus + Grafana over the collector, NIXL's own exporter and (with
# MONITOR_PROFILES including `exporters`) node-exporter.  Separate from
# snoop-up because the collector is what a sweep needs and this is what a human
# watching one needs.
MONITOR_PROFILES ?= monitoring exporters
monitor-up:                    # prometheus + grafana + exporters (http://localhost:3000)
	IMAGE_REF="$(IMAGE_REF)" docker compose -f docker/compose/bench-stack.yml \
		$(foreach p,$(MONITOR_PROFILES),--profile $(p)) up -d
	@echo "grafana:    http://localhost:$${GRAFANA_PORT:-3000}  (anonymous viewer)"
	@echo "prometheus: http://localhost:$${PROM_PORT:-9090}"

monitor-down:
	IMAGE_REF="$(IMAGE_REF)" docker compose -f docker/compose/bench-stack.yml \
		$(foreach p,$(MONITOR_PROFILES),--profile $(p)) down --remove-orphans

# The TSDB lives in a docker volume on a node Slurm is going to take back, so
# this is the step that turns a monitored run into something that still exists
# tomorrow.  RUN_TAG picks the output directory; PROM_START/PROM_END narrow the
# window (epoch seconds) and default to the last hour.
snoop-dump:                    # prometheus TSDB -> CSV under logs/
	@mkdir -p logs
	PROM_START="$(PROM_START)" PROM_END="$(PROM_END)" PROM_STEP="$(PROM_STEP)" \
		bash $(REPO_ROOT)/docker/scripts/prom-dump.sh \
		logs/$(or $(RUN_TAG),prom-$(shell date +%Y%m%d-%H%M%S))

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

_README_ENV := IMAGE_NAME="$(IMAGE_NAME)" IMAGE_REF="$(IMAGE_REF)" \
	ROCM_VERSION="$(ROCM_VERSION)" \
	NIXL_REF="$(NIXL_REF)" NIXL_GIT_URL="$(NIXL_GIT_URL)" \
	MORI_REF="$(MORI_REF)" MORI_GIT_URL="$(MORI_GIT_URL)" \
	UCX_REF="$(UCX_REF)" UCX_GIT_URL="$(UCX_GIT_URL)" \
	LIBFABRIC_REF="$(LIBFABRIC_REF)" LIBFABRIC_GIT_URL="$(LIBFABRIC_GIT_URL)" \
	HIPFILE_REF="$(HIPFILE_REF)" HIPFILE_GIT_URL="$(HIPFILE_GIT_URL)" \
	FIO_REF="$(FIO_REF)" FIO_GIT_URL="$(FIO_GIT_URL)" \
	PERFTEST_REF="$(PERFTEST_REF)" PERFTEST_GIT_URL="$(PERFTEST_GIT_URL)"

readme:
	@$(_README_ENV) "$(REPO_ROOT)/docker/scripts/readme-pins.sh"

readme-check:
	@$(_README_ENV) "$(REPO_ROOT)/docker/scripts/readme-pins.sh" --check

# Print one pin's resolved value. For pins-check, which has to see what the
# Makefile actually derived rather than what the bake file says.
print-pin:
	@echo "$($(PIN))"

# Fail if the Makefile or the Dockerfile disagrees with docker-bake.hcl about
# a pin. See the script for why all three can hold a value and only one is the
# source of truth.
pins-check:
	@"$(REPO_ROOT)/docker/scripts/pins-check.sh"

# Everything that can be checked without building or a cluster. What CI runs.
check: pins-check readme-check patch-check

print-config:
	@echo "VERSION        = $(VERSION)"
	@echo "IMAGE_REF      = $(IMAGE_REF)"
	@echo "ROCM_VERSION   = $(ROCM_VERSION)"
	@echo "ROCM_ARCH      = $(ROCM_ARCH)"
	@echo "NIXL           = $(NIXL_GIT_URL) @ $(NIXL_REF)"
	@echo "MORI           = $(MORI_GIT_URL) @ $(MORI_REF)"
	@echo "UCX            = $(UCX_GIT_URL) @ $(UCX_REF)"
	@echo "hipFile        = $(HIPFILE_GIT_URL) @ $(or $(HIPFILE_REF),<ROCm-packaged>)"
	@echo "fio            = $(FIO_GIT_URL) @ $(or $(FIO_REF),<not built>)"
	@echo "BUILD_JOBS     = $(BUILD_JOBS)"
	@echo "TLS_CERT       = $(TLS_CERT)"

clean:
	rm -rf "$(REPO_ROOT)/build" "$(REPO_ROOT)/dist"

clean-images:
	-docker images --format '{{.Repository}}:{{.Tag}}' | grep '^$(IMAGE_NAME):' | xargs -r docker rmi

# ---- Test cluster (qemu-tool) ----------------------------------------------
# An N-VM ernic mesh with a GPU and an NVMe per guest, for exercising the NIXL
# backends without real hardware. See cluster/README.md.
#
# qemu-tool has to come from the qemu-minimal source tree: the packaged build
# has no gen-compose.

QEMU_MINIMAL ?= $(HOME)/Projects/qemu-minimal
QEMU_TOOL    ?= /tmp/qt/bin/qemu-tool
CLUSTER_DIR  ?= /data/qemu-tool-stebates/stacks/nixl-mori
CLUSTER_ENV  := --env-file $(QEMU_MINIMAL)/qemu/env/scale-out \
                --env-file $(REPO_ROOT)/cluster/env/nixl-mori

# Fetch VM_BACKING_IMAGE and build the guest disks and the fleet key from it.
# Everything needed to turn the pins in cluster/env/nixl-mori back into a
# running cluster; run this before the first cluster-up. Idempotent, keyed on
# the image tag; FORCE=1 refetches.
cluster-images:
	@"$(REPO_ROOT)/cluster/scripts/cluster.sh" images

# Build the rocm-ernic image whose RDMA CM works. Needed for LIBFABRIC: the
# published ernic this cluster pins predates ROCm/rocm-ernic#315. See
# docker/rocm-ernic/Dockerfile.
ernic-image:
	docker buildx bake ernic

cluster-tool:
	@test -x "$(QEMU_TOOL)" || { \
	    echo "Installing qemu-tool from $(QEMU_MINIMAL) into /tmp/qt"; \
	    python3 -m venv /tmp/qt && /tmp/qt/bin/pip install -q "$(QEMU_MINIMAL)/qemu"; }
	@"$(QEMU_TOOL)" gen-compose --help >/dev/null 2>&1 || { \
	    echo "ERROR: $(QEMU_TOOL) has no gen-compose; it is not the source build" >&2; \
	    exit 1; }

cluster-gen: cluster-tool
	@mkdir -p "$(CLUSTER_DIR)"
	"$(QEMU_TOOL)" gen-compose $(CLUSTER_ENV) --output-dir "$(CLUSTER_DIR)"

# `-` on the compose step: it creates every container and then refuses to
# start anything gated on the mesh manager's healthcheck, which cannot pass
# against our ernic image. cluster-boot starts them in dependency order
# instead. See cmd_boot in cluster/scripts/cluster.sh.
cluster-up: cluster-gen
	-"$(QEMU_TOOL)" compose --stack-dir "$(CLUSTER_DIR)" $(CLUSTER_ENV) up -d
	@$(MAKE) --no-print-directory cluster-boot

# Start an already-created stack in dependency order.
cluster-boot:
	@"$(REPO_ROOT)/cluster/scripts/cluster.sh" boot

cluster-down: cluster-tool
	-"$(QEMU_TOOL)" compose --stack-dir "$(CLUSTER_DIR)" $(CLUSTER_ENV) down -v

cluster-status:
	@"$(REPO_ROOT)/cluster/scripts/cluster.sh" status

# memlock, ionic queue count and the fabric address on every guest, persisted.
# Everything the guests need that their image does not ship: memlock, the
# fabric address, the patched ionic provider, and the GPU. The playbook is the
# definition; cluster-prep and cluster-gpu below are shorthands for two of its
# tag sets, kept because they are what the READMEs and muscle memory reach for.
#
#   make cluster-ansible                 everything
#   make cluster-ansible ANSIBLE_TAGS=gpu   just the GPU (needed once per boot)
#   make cluster-ansible ANSIBLE_ARGS=--limit=vm1
ANSIBLE_DIR ?= $(REPO_ROOT)/ansible
ANSIBLE_TAGS ?=
ANSIBLE_ARGS ?=

cluster-ansible:
	@cd "$(ANSIBLE_DIR)" && ansible-playbook guest-setup.yml \
	    $(if $(ANSIBLE_TAGS),--tags $(ANSIBLE_TAGS),) $(ANSIBLE_ARGS)

# Needed after any bring-up that reboots the guests -- without it the ernic
# link stays down and nothing crosses the fabric. Idempotent.
cluster-prep:
	@$(MAKE) --no-print-directory cluster-ansible ANSIBLE_TAGS=prep

# ip_discovery.bin + amdgpu-probe on every guest. Needed once per boot, and
# MORI_IO cannot start without it. Idempotent.
cluster-gpu:
	@$(MAKE) --no-print-directory cluster-ansible ANSIBLE_TAGS=gpu

# ext4 + mount the local NVMe on every guest. Idempotent.
cluster-nvme:
	@"$(REPO_ROOT)/cluster/scripts/cluster.sh" nvme

# Export /opt from the slim image and install it on every guest. Needs
# `docker buildx bake slim` first.
cluster-stack:
	@"$(REPO_ROOT)/cluster/scripts/cluster.sh" stack

# Turn one rail of one VM into an S3-over-RDMA object store, so NIXL's OBJ
# backend has something to talk to. One guest and one rail are a complete
# object fabric -- the store, the HTTP endpoint and the RDMA mover all live in
# the rocm-ernic process. VM= and RAIL= choose which; `restore` puts the rail
# back in the TCP mesh. Re-probe the GPUs afterwards: the VM is restarted.
#
#   make cluster-s3                       # vm4 rail 2 serves S3
#   make cluster-gpu ANSIBLE_ARGS='--limit vm4'
#   make cluster-s3-restore               # back to the mesh
cluster-s3:
	@VM="$(VM)" RAIL="$(RAIL)" "$(REPO_ROOT)/cluster/scripts/s3-server.sh" start

cluster-s3-restore:
	@VM="$(VM)" RAIL="$(RAIL)" "$(REPO_ROOT)/cluster/scripts/s3-server.sh" restore
