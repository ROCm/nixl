#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Run one or more storage-sweep sets with the hsa-snoop sidecar collecting
# underneath them.
#
# This is the thing `make storage-sweep` cannot be: it has to own a lifetime
# longer than a single sweep.  The collector comes up once, the host-side
# scraper samples it on a fixed cadence for the whole session, and the sweep
# sets run in between -- so a plateau in a sweep row can be checked against
# what the GPU and the PCIe devices were actually doing at that wallclock.
# Bringing the collector up and down around each sweep would reset its
# counters and defeat the point.
#
# Run it on a node you already hold, e.g.
#   srun --overlap --jobid=<held job> bash -s < .slurm/snoop-sweep.sh
# or under sbatch.  It does not allocate anything itself.
#
# Environment:
#   IMAGE_REF     required; `make print-tag` prints it
#   SWEEP_SETS    space-separated storage-sweep sets (default "quick")
#   RUN_TAG       subdirectory under logs/ (default: a timestamp)
#   SAMPLE_SEC    scrape interval for the sidecar (default 2)
#   HSA_SNOOP_PORT  default 9488
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)"
# BASH_SOURCE is empty under `bash -s`, which is the documented way to run this
# on a compute node, so fall back to the known checkout rather than resolving
# to "/" and writing logs into the root of the filesystem.
[[ -f "${REPO_ROOT}/Makefile" ]] || REPO_ROOT="/home/AMD/stebates/Projects/nixl-mori-test"
cd "${REPO_ROOT}"

IMAGE_REF="${IMAGE_REF:-$(make -s print-tag 2>/dev/null | tail -1)}"
SWEEP_SETS="${SWEEP_SETS:-quick}"
RUN_TAG="${RUN_TAG:-$(date +%Y%m%d-%H%M%S)}"
SAMPLE_SEC="${SAMPLE_SEC:-2}"
HSA_SNOOP_PORT="${HSA_SNOOP_PORT:-9488}"
COMPOSE="docker compose -f docker/compose/bench-stack.yml"

OUT_DIR="${REPO_ROOT}/logs/snoop-${RUN_TAG}"
mkdir -p "${OUT_DIR}"

export IMAGE_REF HSA_SNOOP_PORT

if ! docker image inspect "${IMAGE_REF}" > /dev/null 2>&1; then
	echo "ERROR: ${IMAGE_REF} is not loaded on $(hostname)" >&2
	exit 1
fi

echo "=== snoop-sweep ${RUN_TAG} on $(hostname) ==="
echo "image:  ${IMAGE_REF}"
echo "sets:   ${SWEEP_SETS}"
echo "out:    ${OUT_DIR}"
df -h /mnt/nixl-nvme-* 2>/dev/null | sed 's/^/  /'

sampler_pid=""
cleanup() {
	[[ -n "${sampler_pid}" ]] && kill "${sampler_pid}" 2> /dev/null
	# The collector is stopped but its logs are kept: an hsa-snoop that died
	# mid-run is the single most likely reason for a gap in the trace, and its
	# stderr is the only place that says so.
	${COMPOSE} logs --no-color hsa-snoop > "${OUT_DIR}/hsa-snoop.log" 2>&1
	${COMPOSE} down --remove-orphans > /dev/null 2>&1
	echo "=== stopped, artefacts in ${OUT_DIR} ==="
}
trap cleanup EXIT INT TERM

${COMPOSE} up -d hsa-snoop || { echo "ERROR: sidecar did not start" >&2; exit 1; }

# Wait for /metrics rather than trusting `up -d`.  hsa-snoop has to attach its
# kprobes and spin up the HTTP server before it reports anything, and a sweep
# started too early produces rows with no trace behind them -- the failure is
# invisible until analysis, which is the worst time to find it.
for _ in $(seq 30); do
	curl -fsS --max-time 2 "http://127.0.0.1:${HSA_SNOOP_PORT}/metrics" > /dev/null 2>&1 && break
	sleep 2
done
if ! curl -fsS --max-time 2 "http://127.0.0.1:${HSA_SNOOP_PORT}/metrics" > /dev/null 2>&1; then
	echo "ERROR: no /metrics on :${HSA_SNOOP_PORT} after 60s" >&2
	${COMPOSE} logs --no-color hsa-snoop | tail -40 >&2
	exit 1
fi
echo "sidecar up, $(curl -fsS "http://127.0.0.1:${HSA_SNOOP_PORT}/metrics" | grep -vc '^#') series exported"

bash docker/scripts/snoop-sample.sh "${OUT_DIR}/snoop-samples.tsv" "${SAMPLE_SEC}" &
sampler_pid=$!

rc_all=0
for set_name in ${SWEEP_SETS}; do
	echo
	echo "########## sweep set: ${set_name}  $(date -Is) ##########"
	SWEEP_SET="${set_name}" \
		SWEEP_OUT="/work/logs/snoop-${RUN_TAG}/storage-sweep-${set_name}.csv" \
		${COMPOSE} run --rm sweep 2>&1 |
		tee "${OUT_DIR}/sweep-${set_name}.log"
	rc=${PIPESTATUS[0]}
	echo "########## ${set_name} finished rc=${rc} $(date -Is) ##########"
	[[ ${rc} -ne 0 ]] && rc_all=1
done

echo
echo "=== summary ==="
for f in "${OUT_DIR}"/storage-sweep-*.csv; do
	[[ -f "${f}" ]] || continue
	echo "$(basename "${f}"): $(($(wc -l < "${f}") - 1)) rows"
done
echo "snoop samples: $(($(wc -l < "${OUT_DIR}/snoop-samples.tsv" 2>/dev/null || echo 1) - 1)) lines"
exit ${rc_all}
