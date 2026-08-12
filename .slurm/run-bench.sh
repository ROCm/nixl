#!/bin/bash
#
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Run the nixlbench pair on real GPU hardware via Slurm, for one backend or for
# both back to back.
#
# The login node has one GPU and no RDMA NIC, so `make bench` there can only
# ever measure loopback.  A number worth quoting needs a node with GPUs and a
# NIC -- on this cluster, an MI300X + ConnectX-7 node such as
# ctr-rack31-mi300x-2/3, reached through the `defq` partition with -C GFX942.
#
# NOTE ON THE QUEUE: as of 2026-08-11 every GFX942 node is booked roughly two
# weeks out (`sbatch --test-only` reports a start date ~13 days ahead), so this
# will not return quickly.  Use NM_BENCH_NODELIST to pin a node you already
# hold an allocation on, or run `run-nixlbench` directly inside an salloc.
#
#   .slurm/run-bench.sh                       # UCX then MORI_IO, 1 node, 2 GPUs
#   NM_BACKENDS=MORI_IO .slurm/run-bench.sh   # just one
#   NM_BENCH_NODES=2 .slurm/run-bench.sh      # initiator and target on 2 nodes
#
# With NM_BENCH_NODES=2 the two ranks land on different nodes and rendezvous
# over the fabric, which is the configuration that actually exercises RDMA.
# Rank 0 is the target (it binds); rank 1 is the initiator and connects to
# rank 0's address, which srun tells us via SLURM_NODELIST.
#
# The image is `docker load`ed from the shared tarball on each node if it is
# not already present, so `make dist-build` is the only prerequisite.
#
# Environment:
#   NM_BACKENDS        space/comma list of backends   (default: "UCX MORI_IO")
#   NM_SEG_TYPE        VRAM or DRAM                   (default: VRAM)
#   NM_BENCH_NODES     1 (both ranks local) or 2      (default: 1)
#   NM_BENCH_PARTITION Slurm partition                (default: defq)
#   NM_BENCH_CONSTRAINT -C expression for the nodes   (default: GFX942)
#   NM_BENCH_ACCOUNT   Slurm account, if yours needs one (default: unset)
#   NM_BENCH_NODELIST  pin exact nodes (overrides the constraint)
#   NM_BENCH_TIME      job time limit                 (default: 01:00:00)
#   NM_BENCH_GPUS      GPUs per node, via --gres=gpu:N (default: 2 for 1-node, 1 for 2-node)
#   NM_IMAGE_DIR       where the tarball lives        (default: /scratch/$USER/nixl-mori-images)
#   NM_BENCH_EXTRA     extra nixlbench flags, both ranks
#   NM_ASIO_PORT       rendezvous port for the 2-node case (default: 18515)
#   NM_FORCE_LOAD      1 = docker load even if the node looks up to date
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

NM_BACKENDS="${NM_BACKENDS:-UCX MORI_IO}"
NM_SEG_TYPE="${NM_SEG_TYPE:-VRAM}"
NM_BENCH_NODES="${NM_BENCH_NODES:-1}"
NM_BENCH_PARTITION="${NM_BENCH_PARTITION:-defq}"
# GFX942 alone, not GFX942&RDMA: the RDMA feature is not advertised on every
# partition and an unknown feature is rejected outright ("Invalid feature
# specification") rather than just matching nothing.
NM_BENCH_CONSTRAINT="${NM_BENCH_CONSTRAINT:-GFX942}"
NM_BENCH_NODELIST="${NM_BENCH_NODELIST:-}"
NM_BENCH_TIME="${NM_BENCH_TIME:-01:00:00}"
NM_IMAGE_DIR="${NM_IMAGE_DIR:-/scratch/${USER}/nixl-mori-images}"
NM_BENCH_EXTRA="${NM_BENCH_EXTRA:-}"
NM_ASIO_PORT="${NM_ASIO_PORT:-18515}"
NM_ROCM_ARCH="${NM_ROCM_ARCH:-gfx942}"
NM_BENCH_ACCOUNT="${NM_BENCH_ACCOUNT:-}"

if [[ "${NM_BENCH_NODES}" == "1" ]]; then
	NM_BENCH_GPUS="${NM_BENCH_GPUS:-2}"
else
	NM_BENCH_GPUS="${NM_BENCH_GPUS:-1}"
fi

log() { printf '[run-bench %(%H:%M:%S)T] %s\n' -1 "$*" >&2; }
die() {
	printf '[run-bench] ERROR: %s\n' "$*" >&2
	exit 1
}

IMAGE_REF="$(cd "${REPO_ROOT}" && make -s print-tag ROCM_ARCH="${NM_ROCM_ARCH}")"
[[ -n "${IMAGE_REF}" ]] || die "could not derive the image ref"
TARBALL="${NM_IMAGE_DIR}/${IMAGE_REF//[:\/]/_}.tar"

BACKENDS="${NM_BACKENDS//,/ }"

# Docker flags: ROCm devices, host networking so two nodes can see each other,
# and the two things GPUDirect registration needs -- CAP_IPC_LOCK and an
# unlimited memlock ulimit.  Docker's 64 KiB memlock default is what makes UCX
# report "failed to register address ... (rocm) ... Input/output error" and
# then fall back to a transport orders of magnitude slower.
DOCKER_FLAGS='--rm --device=/dev/kfd --device=/dev/dri --cap-add=IPC_LOCK
    --ulimit memlock=-1:-1
    --cap-add=SYS_PTRACE --security-opt seccomp=unconfined --ipc=host
    --network=host --shm-size=16g'

mkdir -p "${REPO_ROOT}/logs"

body="$(
	cat << PREAMBLE
IMAGE_REF='${IMAGE_REF}'
TARBALL='${TARBALL}'
BACKENDS='${BACKENDS}'
SEG_TYPE='${NM_SEG_TYPE}'
NODES='${NM_BENCH_NODES}'
ASIO_PORT='${NM_ASIO_PORT}'
BENCH_EXTRA='${NM_BENCH_EXTRA}'
DOCKER_FLAGS='${DOCKER_FLAGS}'
FORCE_LOAD='${NM_FORCE_LOAD:-0}'
PREAMBLE
	cat << 'BODY'

set -euo pipefail
echo "bench nodes: ${SLURM_JOB_NODELIST:-$(hostname)}"

# Make sure every node in the job has the image before any rank starts, so a
# slow `docker load` on one node cannot look like a rendezvous timeout.
# "Is the tag present" is NOT a sufficient test for whether to load.  The image
# tag encodes the component versions (ROCm/NIXL/MORI), not the state of this
# tree, so a rebuild with new patches produces the SAME tag -- and a node that
# already holds the previous build will happily skip the load and run stale
# code.  That has bitten twice: a benchmark reporting a bug that was already
# fixed, because the node was running yesterday's image.
#
# So compare timestamps: reload whenever the tarball is newer than the image
# that is loaded.  NM_FORCE_LOAD=1 reloads unconditionally.
load_image() {
    [ -f "${TARBALL}" ] || { echo "$(hostname): no tarball at ${TARBALL} (run make dist-build)" >&2; exit 1; }

    if [ "${FORCE_LOAD}" != "1" ] && docker image inspect "${IMAGE_REF}" >/dev/null 2>&1; then
        img_epoch="$(date -d "$(docker image inspect --format '{{.Created}}' "${IMAGE_REF}")" +%s 2>/dev/null || echo 0)"
        tar_epoch="$(stat -c %Y "${TARBALL}" 2>/dev/null || echo 0)"
        if [ "${img_epoch}" -ge "${tar_epoch}" ] && [ "${img_epoch}" -gt 0 ]; then
            echo "$(hostname): ${IMAGE_REF} is current (image $(date -d @${img_epoch} +%H:%M) >= tarball $(date -d @${tar_epoch} +%H:%M))"
            return 0
        fi
        echo "$(hostname): loaded image ($(date -d @${img_epoch} +%H:%M)) is older than the tarball ($(date -d @${tar_epoch} +%H:%M)) -- reloading"
    fi
    echo "$(hostname): loading ${TARBALL}"
    docker load -i "${TARBALL}" >/dev/null
}
export -f load_image
export IMAGE_REF TARBALL FORCE_LOAD

if [ "${NODES}" = "1" ]; then
    load_image
else
    srun --ntasks="${SLURM_JOB_NUM_NODES}" --ntasks-per-node=1 \
        bash -c 'load_image'
fi

for backend in ${BACKENDS}; do
    echo
    echo "############ backend=${backend} seg=${SEG_TYPE} nodes=${NODES} ############"
    if [ "${NODES}" = "1" ]; then
        # Both ranks in one container on one node; run-nixlbench picks a free
        # rendezvous port itself and pairs GPU 0 with GPU 1.
        # /dev/infiniband must be passed through here too, not just in the
        # 2-node branch: without it UCX has no IB transport and MORI's RDMA
        # backend cannot initialise, so a single-node run silently measures
        # whatever slow fallback is left.
        # shellcheck disable=SC2086
        docker run ${DOCKER_FLAGS} \
            $([ -d /dev/infiniband ] && echo --device=/dev/infiniband) \
            -e BACKEND="${backend}" -e SEG_TYPE="${SEG_TYPE}" \
            -e EXTRA_ARGS="${BENCH_EXTRA}" \
            "${IMAGE_REF}" run-nixlbench || echo "backend ${backend} FAILED"
    else
        # Rank 0 = target (binds), rank 1 = initiator (connects to rank 0).
        # Both need the SAME port, so it is fixed rather than auto-picked.
        target_host="$(scontrol show hostnames "${SLURM_JOB_NODELIST}" | head -1)"
        echo "target host: ${target_host}  port: ${ASIO_PORT}"
        srun --ntasks=2 --ntasks-per-node=1 --export=ALL \
            bash -c '
                if [ "${SLURM_PROCID}" = "0" ]; then role=target; else role=initiator; sleep 3; fi
                # shellcheck disable=SC2086
                docker run '"${DOCKER_FLAGS}"' \
                    $([ -d /dev/infiniband ] && echo --device=/dev/infiniband) \
                    -e BACKEND="'"${backend}"'" -e SEG_TYPE="'"${SEG_TYPE}"'" \
                    -e EXTRA_ARGS="'"${BENCH_EXTRA}"'" \
                    -e ROLE="${role}" \
                    -e ASIO_ADDR="'"${target_host}"'" -e ASIO_PORT="'"${ASIO_PORT}"'" \
                    "'"${IMAGE_REF}"'" run-nixlbench
            ' || echo "backend ${backend} FAILED"
    fi
done
BODY
)"

sel=()
if [[ -n "${NM_BENCH_NODELIST}" ]]; then
	sel=(--nodelist="${NM_BENCH_NODELIST}")
elif [[ -n "${NM_BENCH_CONSTRAINT}" ]]; then
	sel=(--constraint="${NM_BENCH_CONSTRAINT}")
fi

script="$(mktemp "${TMPDIR:-/tmp}/nm-bench-XXXXXX.sh")"
{
	echo '#!/bin/bash'
	echo "${body}"
} > "${script}"
chmod +x "${script}"

log "submitting bench (partition=${NM_BENCH_PARTITION} nodes=${NM_BENCH_NODES} backends=${BACKENDS})"
rc=0
sbatch --wait \
	--job-name=nm-bench \
	--partition="${NM_BENCH_PARTITION}" \
	--nodes="${NM_BENCH_NODES}" \
	--ntasks-per-node=1 \
	--gres=gpu:"${NM_BENCH_GPUS}" \
	${NM_BENCH_ACCOUNT:+--account="${NM_BENCH_ACCOUNT}"} \
	--time="${NM_BENCH_TIME}" \
	--output="${REPO_ROOT}/logs/bench-%j.out" \
	"${sel[@]}" \
	"${script}" || rc=$?
rm -f "${script}"

latest="$(ls -t "${REPO_ROOT}"/logs/bench-*.out 2> /dev/null | head -1 || true)"
if [[ -n "${latest}" ]]; then
	log "----- ${latest} -----"
	cat "${latest}"
fi
exit "${rc}"
