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

if [[ "${rc}" -eq 0 ]]; then
	echo "pins-check: docker-bake.hcl, Makefile and Dockerfile agree"
fi
exit "${rc}"
