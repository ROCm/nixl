#!/bin/bash
#
# End-to-end validation of one built image on the storage MI300X node: load it,
# check the plugins are the ones we think they are, then smoke test, capability
# probe, GPU compare and the NVMe sweeps, back to back.
#
# Submitted as a single job rather than dist-load + dist-bench + storage-sweep
# because the storage partition is contended: each separate submission would
# queue for the node independently, and the image would have to survive between
# them.  One job means one wait.
#
# Deliberately not named after a pin -- it validates whatever IMAGE_REF points
# at, whether that is a NIXL bump, a hipFile bump or a new plugin.
#
# Environment:
#   REPO_ROOT   repo checkout, must be reachable from the compute node
#   IMAGE_REF   image tag to validate (make print-tag)
#   TARBALL     shared-filesystem tarball to load if the image is absent
#   NVME_DIR    scratch directory on an NVMe mount for the capability probe
#               (default: the first /mnt/nixl-nvme-* on the node)
#   SKIP_FULL   1 = stop after the quick sweep, do not run SWEEP_SET=full
set -uo pipefail

REPO_ROOT="${REPO_ROOT:?}"
IMAGE_REF="${IMAGE_REF:?}"
TARBALL="${TARBALL:?}"
SKIP_FULL="${SKIP_FULL:-}"

cd "${REPO_ROOT}"
TAG="${IMAGE_REF#*:}"
echo "=== node: $(hostname)  $(date -Is) ==="

if docker image inspect "${IMAGE_REF}" > /dev/null 2>&1; then
	echo "${IMAGE_REF} already present; skipping load"
else
	echo "=== docker load ${TARBALL} ==="
	time docker load -i "${TARBALL}" || exit 1
fi
docker image inspect "${IMAGE_REF}" --format 'loaded: {{.RepoTags}}'

rc=0
step=0
total=6
[[ -n "${SKIP_FULL}" ]] && total=5

banner() {
	step=$((step + 1))
	echo
	echo "=== [${step}/${total}] $* ==="
}

# ---------------------------------------------------------------------------
# 1. What is actually in the image.
#
# The build asserts this too, but the build node is not this node: this is the
# cheap check that the tarball that arrived here is the one that was built, and
# that libhipfile resolves to the source-built copy rather than the one the
# ROCm base image packages.  Two libhipfile.so exist in the image and only the
# newer one has the async API the AIS plugin needs, so getting the wrong one is
# a plausible failure, not a paranoid one.
# ---------------------------------------------------------------------------
banner "image inventory: plugins and hipFile linkage"
docker run --rm --entrypoint bash "${IMAGE_REF}" -c '
	set -u
	pdir="$(dirname "$(find /opt/nixl -name "libplugin_UCX.so" | head -1)")"
	echo "plugin dir: ${pdir}"
	ls -1 "${pdir}" | sed "s/^/  /"
	echo
	for p in UCX MORI_IO AIS AIS_MT POSIX; do
		if [ -e "${pdir}/libplugin_${p}.so" ]; then
			echo "present: ${p}"
		else
			echo "MISSING: ${p}"; exit 1
		fi
	done
	echo
	echo "libhipfile resolution for the AIS plugins:"
	for p in AIS AIS_MT; do
		lib="$(ldd "${pdir}/libplugin_${p}.so" | awk "/libhipfile/ {print \$3}")"
		echo "  ${p}: ${lib:-<none>}"
		case "${lib}" in
			/opt/hipfile/*) ;;
			*) echo "  WARNING: ${p} is not linked against the source-built hipFile" ;;
		esac
	done
' || { echo "IMAGE INVENTORY FAILED"; rc=1; }

banner "smoke test"
make test IMAGE_TAG="${TAG}" || { echo "SMOKE TEST FAILED"; rc=1; }

# ---------------------------------------------------------------------------
# 3. Does AIS move bytes at all.
#
# Worth its own stage before any sweep, because the failure mode it catches is
# quiet: hipFile's batch API validates its parameters and returns success while
# issuing no I/O, and the async path this plugin uses could in principle do the
# same on a backend that does not implement it.  One tiny transfer, one file --
# if this reports a plausible bandwidth the plugin is really talking to the
# drive, and the sweep numbers below mean something.
# ---------------------------------------------------------------------------
banner "AIS capability probe (1 file)"
probe_dir="${NVME_DIR:-$(ls -d /mnt/nixl-nvme-* 2> /dev/null | head -1)}"
if [[ -z "${probe_dir}" ]]; then
	echo "SKIPPED: no /mnt/nixl-nvme-* on this node"
else
	probe_path="${probe_dir}/nixlbench-${USER}-aisprobe"
	echo "probe path: ${probe_path}"
	make bench IMAGE_TAG="${TAG}" BACKEND=AIS SEG_TYPE=VRAM \
		FILEPATH="${probe_path}" BENCH_EXTRA="--num_files 1" \
		|| { echo "AIS CAPABILITY PROBE FAILED"; rc=1; }
	rm -rf "${probe_path}"
fi

banner "bench-compare: UCX vs MORI_IO, VRAM"
make bench-compare SEG_TYPE=VRAM IMAGE_TAG="${TAG}" || rc=1

# ---------------------------------------------------------------------------
# 5. Regression guard before the expensive sweep.
#
# AIS_MT is unchanged by any of this, so its quick-set numbers should match the
# ~56 GB/s plateau recorded in d0ad82c.  If they do not, the hipFile bump
# regressed the existing path and the AIS-vs-AIS_MT comparison in the full
# sweep is measuring two things at once.
# ---------------------------------------------------------------------------
banner "storage sweep: quick (AIS_MT regression guard)"
make storage-sweep SWEEP_SET=quick IMAGE_TAG="${TAG}" || rc=1

if [[ -z "${SKIP_FULL}" ]]; then
	banner "storage sweep: full"
	make storage-sweep SWEEP_SET=full IMAGE_TAG="${TAG}" || rc=1
fi

echo
echo "=== done $(date -Is) rc=${rc} ==="
exit "${rc}"
