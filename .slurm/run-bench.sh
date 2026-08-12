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

# Docker flags: ROCm devices, the verbs devices for RDMA, IPC_LOCK for pinned
# memory registration, host networking so the two nodes can see each other.
DOCKER_FLAGS='--rm --device=/dev/kfd --device=/dev/dri --cap-add=IPC_LOCK
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
PREAMBLE
	cat << 'BODY'

set -euo pipefail
echo "bench nodes: ${SLURM_JOB_NODELIST:-$(hostname)}"

# Make sure every node in the job has the image before any rank starts, so a
# slow `docker load` on one node cannot look like a rendezvous timeout.
load_image() {
    if docker image inspect "${IMAGE_REF}" >/dev/null 2>&1; then
        echo "$(hostname): ${IMAGE_REF} already present"
        return 0
    fi
    [ -f "${TARBALL}" ] || { echo "$(hostname): no tarball at ${TARBALL} (run make dist-build)" >&2; exit 1; }
    echo "$(hostname): loading ${TARBALL}"
    docker load -i "${TARBALL}" >/dev/null
}
export -f load_image
export IMAGE_REF TARBALL

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
        # shellcheck disable=SC2086
        docker run ${DOCKER_FLAGS} \
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
