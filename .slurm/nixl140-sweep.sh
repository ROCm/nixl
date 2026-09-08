#!/bin/bash
#
# One-shot validation of the NIXL v1.4.0 bump on the storage MI300X node:
# load the image built on the CPU node, then smoke test, GPU compare and the
# full NVMe sweep, back to back.
#
# Submitted as a single job rather than dist-load + dist-bench + storage-sweep
# because the storage partition is contended: each separate submission would
# queue for the node independently, and the image would have to survive between
# them.  One job means one wait.
set -uo pipefail

REPO_ROOT="${REPO_ROOT:?}"
IMAGE_REF="${IMAGE_REF:?}"
TARBALL="${TARBALL:?}"

cd "${REPO_ROOT}"
echo "=== node: $(hostname)  $(date -Is) ==="

if docker image inspect "${IMAGE_REF}" > /dev/null 2>&1; then
	echo "${IMAGE_REF} already present; skipping load"
else
	echo "=== docker load ${TARBALL} ==="
	time docker load -i "${TARBALL}" || exit 1
fi
docker image inspect "${IMAGE_REF}" --format 'loaded: {{.RepoTags}}'

rc=0

echo
echo "=== [1/3] smoke test ==="
make test IMAGE_TAG="${IMAGE_REF#*:}" || { echo "SMOKE TEST FAILED"; rc=1; }

# Keep going even if the smoke test trips: a plugin that fails to load still
# leaves the other two stages' numbers worth having.
echo
echo "=== [2/3] bench-compare: UCX vs MORI_IO, VRAM ==="
make bench-compare SEG_TYPE=VRAM IMAGE_TAG="${IMAGE_REF#*:}" || rc=1

echo
echo "=== [3/3] storage sweep: full ==="
make storage-sweep SWEEP_SET=full IMAGE_TAG="${IMAGE_REF#*:}" || rc=1

echo
echo "=== done $(date -Is) rc=${rc} ==="
exit "${rc}"
