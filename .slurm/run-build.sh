#!/bin/bash
#
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Run the image build on a CPU-only Slurm node instead of the login node, and
# make the result reachable from GPU nodes.
#
# Why: the login node is shared and the build is heavy (UCX + NIXL + nixlbench
# + MORI, ~30 GB of image, all cores for several minutes).  Nothing in the
# build needs a GPU -- MORI's kernels are JIT-compiled at first use and NIXL is
# host code -- so a CPUONLY node is the right place for it.
#
# Distribution: each node keeps its images in its own local docker storage, so
# an image built on one node is invisible everywhere else.  /scratch is shared,
# so the flow is  build -> docker save to /scratch -> docker load on each
# target.  Every target reads the one shared tarball; there is no per-node copy.
#
# Usage (from the repo root):
#
#   .slurm/run-build.sh build              # build + save the tarball
#   .slurm/run-build.sh load               # load the tarball on NM_TARGETS
#   NM_TARGETS=node-a,node-b .slurm/run-build.sh all
#   .slurm/run-build.sh build MAKE_TARGET=build-nixl   # any make target
#
# Or through the Makefile:  make dist-build / make dist-load NM_TARGETS=...
#
# Commands:
#   build   Build on a CPU-only node, then `docker save` to NM_IMAGE_DIR
#   load    `docker load` the saved tarball on every node in NM_TARGETS
#   all     build then load
#
# Environment:
#   NM_ROCM_ARCH        gfx arch(es) baked into MORI.  REQUIRED in spirit: a
#                       CPU-only node has no GPU to detect, so this is what the
#                       image is compiled for.  ';'-separated for multi-arch.
#                       (default: gfx942)
#   NM_BUILD_PARTITION  Slurm partition for the build job     (default: am)
#   NM_BUILD_CONSTRAINT -C feature expression for the build node
#                                                             (default: CPUONLY)
#   NM_BUILD_NODE       pin an exact node via --nodelist (overrides the
#                       constraint)                           (default: unset)
#   NM_BUILD_CPUS       --cpus-per-task for the build job     (default: 32)
#   NM_BUILD_MEM        --mem for the build job               (default: 64G)
#   NM_BUILD_TIME       build job time limit                  (default: 03:00:00)
#   NM_LOAD_TIME        per-node load job time limit          (default: 00:30:00)
#   NM_IMAGE_DIR        shared dir for the tarball
#                                            (default: /scratch/$USER/nixl-mori-images)
#   NM_TARGETS          comma-separated nodes to load onto (required for load/all)
#   NM_MIN_DISK_GB      refuse to start if the build node has less free space
#                       than this on the docker filesystem    (default: 120)
#   NM_BUILD_LOCAL      1 = build on THIS host, no Slurm      (default: unset)
#   MAKE_TARGET         make target to run on the build node  (default: build)
#   MAKE_ARGS           extra args appended to the make invocation
#
# Any Makefile variable can also be passed through MAKE_ARGS, e.g.
#   MAKE_ARGS='NIXL_REF=v1.3.1 INSTALL_TORCH=0' .slurm/run-build.sh build
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

NM_ROCM_ARCH="${NM_ROCM_ARCH:-gfx942}"
NM_BUILD_PARTITION="${NM_BUILD_PARTITION:-am}"
NM_BUILD_CONSTRAINT="${NM_BUILD_CONSTRAINT:-CPUONLY}"
NM_BUILD_NODE="${NM_BUILD_NODE:-}"
NM_BUILD_CPUS="${NM_BUILD_CPUS:-32}"
NM_BUILD_MEM="${NM_BUILD_MEM:-64G}"
NM_BUILD_TIME="${NM_BUILD_TIME:-03:00:00}"
NM_LOAD_TIME="${NM_LOAD_TIME:-00:30:00}"
NM_IMAGE_DIR="${NM_IMAGE_DIR:-/scratch/${USER}/nixl-mori-images}"
NM_TARGETS="${NM_TARGETS:-}"
NM_MIN_DISK_GB="${NM_MIN_DISK_GB:-120}"
NM_BUILD_LOCAL="${NM_BUILD_LOCAL:-}"
MAKE_TARGET="${MAKE_TARGET:-build}"
MAKE_ARGS="${MAKE_ARGS:-}"

log() { printf '[run-build %(%H:%M:%S)T] %s\n' -1 "$*" >&2; }
die() {
	printf '[run-build] ERROR: %s\n' "$*" >&2
	exit 1
}

# The tag is derived from the same pins the build uses, so the tarball name and
# the image ref cannot drift apart.
IMAGE_REF="$(cd "${REPO_ROOT}" && make -s print-tag ROCM_ARCH="${NM_ROCM_ARCH}" ${MAKE_ARGS})"
[[ -n "${IMAGE_REF}" ]] || die "could not derive the image ref (make print-tag failed)"
TARBALL="${NM_IMAGE_DIR}/${IMAGE_REF//[:\/]/_}.tar"

# ---------------------------------------------------------------------------
# sbatch dispatch: submit BODY as a job and stream its output here.
# --wait blocks until the job finishes and propagates its exit status, so this
# script behaves like a synchronous build from the caller's point of view.
# ---------------------------------------------------------------------------
submit() {
	local name="$1" body="$2"
	shift 2
	command -v sbatch > /dev/null 2>&1 \
		|| die "sbatch not found; set NM_BUILD_LOCAL=1 to build on this host"

	local logdir="${REPO_ROOT}/logs"
	mkdir -p "${logdir}"
	local script
	script="$(mktemp "${TMPDIR:-/tmp}/nm-${name}-XXXXXX.sh")"
	{
		echo '#!/bin/bash'
		echo 'set -euo pipefail'
		echo "${body}"
	} > "${script}"
	chmod +x "${script}"

	local out="${logdir}/${name}-%j.out"
	log "submitting ${name} (partition=${NM_BUILD_PARTITION} $*)"
	local rc=0
	sbatch --wait \
		--job-name="nm-${name}" \
		--partition="${NM_BUILD_PARTITION}" \
		--output="${out}" \
		--open-mode=append \
		"$@" \
		"${script}" || rc=$?
	rm -f "${script}"

	# --wait prints the job id on submission; find the log it actually wrote.
	local latest
	latest="$(ls -t "${logdir}/${name}"-*.out 2> /dev/null | head -1 || true)"
	if [[ -n "${latest}" ]]; then
		log "----- ${latest} -----"
		cat "${latest}"
		log "----- end ${latest} -----"
	fi
	return "${rc}"
}

node_selection() {
	if [[ -n "${NM_BUILD_NODE}" ]]; then
		printf '%s\n' "--nodelist=${NM_BUILD_NODE}"
	elif [[ -n "${NM_BUILD_CONSTRAINT}" ]]; then
		printf '%s\n' "--constraint=${NM_BUILD_CONSTRAINT}"
	fi
}

cmd_build() {
	mkdir -p "${NM_IMAGE_DIR}"

	# Heredoc is quoted: the body must expand on the compute node, not here,
	# except for the values we deliberately inline via the preamble below.
	local body
	body="$(
		cat << PREAMBLE
REPO_ROOT='${REPO_ROOT}'
IMAGE_REF='${IMAGE_REF}'
TARBALL='${TARBALL}'
NM_ROCM_ARCH='${NM_ROCM_ARCH}'
NM_MIN_DISK_GB='${NM_MIN_DISK_GB}'
MAKE_TARGET='${MAKE_TARGET}'
MAKE_ARGS='${MAKE_ARGS}'
PREAMBLE
		cat << 'BODY'

echo "build node: $(hostname)  cores: $(nproc)"
command -v docker >/dev/null || { echo "ERROR: docker not on this node" >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo "ERROR: cannot talk to the docker daemon" >&2; exit 1; }

# A build that dies half way through on a full disk wastes far more time than
# the check costs -- the base image alone is ~20 GB.
docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || echo /var/lib/docker)"
avail_gb="$(df -BG --output=avail "${docker_root}" 2>/dev/null | tail -1 | tr -dc '0-9')"
echo "docker root ${docker_root}: ${avail_gb}G free (need ${NM_MIN_DISK_GB}G)"
if [ -n "${avail_gb}" ] && [ "${avail_gb}" -lt "${NM_MIN_DISK_GB}" ]; then
    echo "ERROR: not enough free space on ${docker_root}" >&2
    exit 1
fi

cd "${REPO_ROOT}"
# ROCM_ARCH is passed explicitly: this node has no GPU, so the Makefile's
# rocm_agent_enumerator autodetect finds nothing.
# shellcheck disable=SC2086
make "${MAKE_TARGET}" \
    ROCM_ARCH="${NM_ROCM_ARCH}" \
    BUILD_JOBS="$(nproc)" \
    PROGRESS=plain \
    ${MAKE_ARGS}

# Only the full runtime image is worth shipping; the partial --target builds
# are for local iteration.
case "${MAKE_TARGET}" in
    build)
        echo "saving ${IMAGE_REF} -> ${TARBALL}"
        mkdir -p "$(dirname "${TARBALL}")"
        # Write to a temp name and rename, so a reader on another node never
        # sees a half-written tarball.
        docker save "${IMAGE_REF}" -o "${TARBALL}.partial"
        mv -f "${TARBALL}.partial" "${TARBALL}"
        ls -lh "${TARBALL}"
        ;;
    *)
        echo "MAKE_TARGET=${MAKE_TARGET} is a partial build; not saving a tarball"
        ;;
esac
BODY
	)"

	if [[ -n "${NM_BUILD_LOCAL}" ]]; then
		log "NM_BUILD_LOCAL set -- building on $(hostname), no Slurm"
		bash -c "set -euo pipefail; ${body}"
		return
	fi

	local sel=()
	mapfile -t sel < <(node_selection)
	submit build "${body}" \
		--cpus-per-task="${NM_BUILD_CPUS}" \
		--mem="${NM_BUILD_MEM}" \
		--time="${NM_BUILD_TIME}" \
		"${sel[@]}"
}

cmd_load() {
	[[ -n "${NM_TARGETS}" ]] || die "NM_TARGETS is required for load (comma-separated node list)"
	[[ -f "${TARBALL}" ]] || die "tarball not found: ${TARBALL} (run 'build' first)"

	local node rc=0
	for node in ${NM_TARGETS//,/ }; do
		local body
		body="$(
			cat << PREAMBLE
IMAGE_REF='${IMAGE_REF}'
TARBALL='${TARBALL}'
PREAMBLE
			cat << 'BODY'
echo "loading on $(hostname)"
if docker image inspect "${IMAGE_REF}" >/dev/null 2>&1; then
    echo "${IMAGE_REF} already present; skipping load"
else
    docker load -i "${TARBALL}"
fi
docker image inspect "${IMAGE_REF}" --format 'loaded: {{.RepoTags}} {{.Id}}'
BODY
		)"
		submit "load-${node}" "${body}" \
			--nodelist="${node}" \
			--time="${NM_LOAD_TIME}" \
			--cpus-per-task=2 || rc=$?
	done
	return "${rc}"
}

case "${1:-all}" in
	build) cmd_build ;;
	load) cmd_load ;;
	all)
		cmd_build
		cmd_load
		;;
	*) die "unknown command: $1 (expected build, load or all)" ;;
esac
