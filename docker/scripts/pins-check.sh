#!/usr/bin/env bash
# Fail if anything disagrees with docker-bake.hcl about a component pin.
#
# docker-bake.hcl is the source of truth. The same pin is reachable from three
# places and they used to be written out in all three:
#
#   docker-bake.hcl   what `docker buildx bake` builds           <- the truth
#   Makefile          what `make build` passes as a build arg
#   docker/Dockerfile the ARG default, used when the Dockerfile is built
#                     directly and read by image-tag.sh when a pin is unset
#
# LIBFABRIC_REF was bumped in two of them and left at v1.22.0 in the Makefile.
# Nothing failed. `docker buildx bake` and `make build` compiled different
# libfabrics from the same tree, and the generated README pin table -- which
# reads the Makefile -- advertised a version nothing on the cluster was
# running. That is the failure this exists to make loud.
#
# Two different rules, because the two files have different jobs:
#
#   Makefile    must not define a literal default for a bake variable at all.
#               It derives them now; a re-added literal is drift waiting to
#               happen even while it still agrees.
#   Dockerfile  must keep its ARG defaults -- they are what a direct
#               `docker build` and image-tag.sh fall back to -- but they have
#               to MATCH bake.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
BAKE="${ROOT}/docker-bake.hcl"
MAKEFILE="${ROOT}/Makefile"
DOCKERFILE="${ROOT}/docker/Dockerfile"

rc=0
fail() { printf 'pins-check: %s\n' "$*" >&2; rc=1; }

# NAME<TAB>value for every `variable "X" { default = "..." }` in the bake file.
mapfile -t bake_lines < <(
	sed -n 's/^variable[[:space:]]*"\([A-Z_0-9]*\)"[[:space:]]*{[[:space:]]*default[[:space:]]*=[[:space:]]*"\([^"]*\)".*/\1\t\2/p' "${BAKE}"
)
[[ ${#bake_lines[@]} -gt 0 ]] || { fail "no variables parsed out of ${BAKE##*/}"; exit 1; }

# Pins only. VERSION, IMAGE_TAG and the base-image overrides are computed or
# supplied per-invocation rather than pinned, so they are not comparable.
PINS=" ROCM_VERSION NIXL_REF MORI_REF UCX_REF LIBFABRIC_REF HIPFILE_REF FIO_REF IMAGE_NAME "

for line in "${bake_lines[@]}"; do
	name="${line%%$'\t'*}"
	value="${line#*$'\t'}"
	[[ "${PINS}" == *" ${name} "* ]] || continue

	# The Makefile reads these through $(call bake_default,...). A word-split
	# on whitespace is what that does, so a value containing any would be
	# silently truncated there.
	if [[ "${value}" != "${value%%[[:space:]]*}" ]]; then
		fail "${name}'s default contains whitespace, which the Makefile's word-split cannot carry"
	fi

	# Rule 1: no literal default in the Makefile.
	#
	# Column 0 only, and only when the value is not a $(...) expansion. A make
	# variable assignment starts at column 0; a recipe line starts with a tab,
	# so `NIXL_REF="$(NIXL_REF)" \` -- passing the pin as a build arg, which is
	# exactly what the Makefile is supposed to do -- is not an assignment and
	# must not be flagged.
	hits="$(awk -v n="${name}" '
		/^\t/ { next }
		$0 ~ "^" n "[[:space:]]*[?:]?=" {
			v = $0; sub("^" n "[[:space:]]*[?:]?=[[:space:]]*", "", v)
			if (v !~ /^\$\(/ && v != "") print FNR ": " $0
		}' "${MAKEFILE}")"
	if [[ -n "${hits}" ]]; then
		fail "${name} has a literal default in Makefile; it must come from docker-bake.hcl"
		printf '           %s\n' "${hits}" >&2
	fi

	# Rule 2: the Dockerfile ARG default, where there is one, must match.
	arg="$(sed -n "s/^ARG[[:space:]]\+${name}=\(.*\)$/\1/p" "${DOCKERFILE}" | head -1)"
	if [[ -n "${arg}" && "${arg}" != "${value}" ]]; then
		fail "${name}: docker-bake.hcl says '${value}', docker/Dockerfile ARG says '${arg}'"
	fi
done

# The Makefile must actually be deriving them, not just silently empty -- a
# typo in the sed would leave every pin blank and every build would then take
# the Dockerfile ARG, which is exactly the drift this is meant to catch.
for name in ROCM_VERSION NIXL_REF MORI_REF UCX_REF LIBFABRIC_REF FIO_REF IMAGE_NAME; do
	got="$(make -s -C "${ROOT}" print-pin PIN="${name}" 2>/dev/null || true)"
	if [[ -z "${got}" ]]; then
		fail "${name} resolves to empty in the Makefile"
	fi
done

# ---------------------------------------------------------------------------
# The rocm-ernic image. Same class of bug, three more places to state a value,
# plus one cross-file invariant that is the entire reason the image exists.
# ---------------------------------------------------------------------------
ERNIC_DOCKERFILE="${ROOT}/docker/rocm-ernic/Dockerfile"
ENV_FILE="${ROOT}/cluster/env/nixl-mori"

bake_var() {
	sed -n "s/^variable[[:space:]]*\"$1\"[[:space:]]*{[[:space:]]*default[[:space:]]*=[[:space:]]*\"\([^\"]*\)\".*/\1/p" "${BAKE}" | head -1
}
dockerfile_arg() {
	sed -n "s/^ARG[[:space:]]\+$1=\(.*\)$/\1/p" "${ERNIC_DOCKERFILE}" | head -1
}
env_var() { sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1; }

# A variable declared across multiple lines reads as absent to the single-line
# regex both this script and the Makefile use, so a check against it silently
# passes. Catch the shape rather than the symptom.
multi="$(grep -nE '^variable[[:space:]]*"[A-Z_0-9]+"[[:space:]]*\{[[:space:]]*$' "${BAKE}" || true)"
if [[ -n "${multi}" ]]; then
	fail "docker-bake.hcl has multi-line variable declarations; they read as absent"
	printf '           %s\n' "${multi}" >&2
fi

if [[ -f "${ERNIC_DOCKERFILE}" ]]; then
	for name in ERNIC_REF ERNIC_BASE_IMAGE; do
		b="$(bake_var "${name}")"
		d="$(dockerfile_arg "${name}")"
		if [[ -n "${b}" && -n "${d}" && "${b}" != "${d}" ]]; then
			fail "${name}: docker-bake.hcl says '${b}', docker/rocm-ernic/Dockerfile ARG says '${d}'"
		fi
	done

	# cluster/env/nixl-mori has to name the tag bake actually produces.
	# Bumping ERNIC_REF changes the tag, and a stale ERNIC_IMAGE then points
	# at an image that still exists locally -- so the cluster comes up on the
	# OLD device and LIBFABRIC quietly stops working again.
	ref="$(bake_var ERNIC_REF)"
	iname="$(bake_var ERNIC_IMAGE_NAME)"
	want="${iname}:${ref:0:7}"
	have="$(env_var ERNIC_IMAGE)"
	if [[ -n "${have}" && "${have}" != "${want}" ]]; then
		fail "ERNIC_IMAGE in cluster/env/nixl-mori is '${have}', but bake builds '${want}'"
	fi

	# The invariant the image exists to satisfy: the server inherits its
	# libvfio-user from ERNIC_BASE_IMAGE and must speak the same vfio-user
	# wire protocol as the QEMU it serves. gen-compose checks QEMU against
	# ERNIC_IMAGE, which is now ours and carries no stamp, so nothing else
	# checks this at all.
	base_vfu=""; qemu_vfu=""
	for part in $(tr '-' ' ' <<< "$(bake_var ERNIC_BASE_IMAGE)"); do
		[[ "${part}" == vfu.* ]] && base_vfu="${part}"
	done
	for part in $(tr '-' ' ' <<< "$(env_var QEMU_IMAGE)"); do
		[[ "${part}" == vfu.* ]] && qemu_vfu="${part}"
	done
	if [[ -n "${base_vfu}" && -n "${qemu_vfu}" && "${base_vfu}" != "${qemu_vfu}" ]]; then
		fail "libvfio-user mismatch: ERNIC_BASE_IMAGE is ${base_vfu}, QEMU_IMAGE is ${qemu_vfu}"
		fail "  the vfio-user wire protocol is not compatible across revisions"
	fi
fi

if [[ "${rc}" -eq 0 ]]; then
	echo "pins-check: bake, Makefile, Dockerfiles and cluster env agree"
fi
exit "${rc}"
