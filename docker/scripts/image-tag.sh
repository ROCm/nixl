#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Derive the image tag from the component version build args, falling back to
# the versions pinned in the Dockerfile.
#
# Emits just the tag component (no image name), e.g.
#   0.1.0-rocm7.14.0-nixl1.3.2-mori1.2.2
#
# Usage:  image-tag.sh [path/to/Dockerfile]
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DOCKERFILE="${1:-${SCRIPT_DIR}/../Dockerfile}"
VERSION_FILE="${REPO_ROOT}/VERSION"

[[ -r "${DOCKERFILE}" ]] || {
	echo "image-tag: cannot read ${DOCKERFILE}" >&2
	exit 1
}
[[ -r "${VERSION_FILE}" ]] || {
	echo "image-tag: cannot read ${VERSION_FILE}" >&2
	exit 1
}

version="$(tr -d ' \t\r\n' < "${VERSION_FILE}")"

# A same-named environment variable is a user-provided build-arg override.
# Honour even an explicitly empty override so validation below fails instead of
# silently producing a tag for the Dockerfile default.
_arg() {
	if [[ -v "$1" ]]; then
		printf '%s\n' "${!1}"
	else
		grep -E "^ARG $1=" "${DOCKERFILE}" | head -1 | cut -d= -f2-
	fi
}

rocm="$(_arg ROCM_VERSION)"
# Refs are git tags like v1.3.2, so drop the leading v.  A SHA/branch ref is
# used verbatim with anything but [A-Za-z0-9._-] squashed to '-'.
_reftag() { _arg "$1" | sed -e 's#^v\([0-9]\)#\1#' -e 's#[^A-Za-z0-9._-]#-#g'; }
nixl="$(_reftag NIXL_REF)"
mori="$(_reftag MORI_REF)"

for _v in version rocm nixl mori; do
	[[ -n "${!_v}" ]] || {
		echo "image-tag: could not resolve ${_v}" >&2
		exit 1
	}
done

# hipFile is deliberately not in the required list: an empty HIPFILE_REF is a
# supported configuration meaning "use the libhipfile the ROCm base image
# packages", and that case keeps the historical four-component tag.  When it is
# pinned it is a 40-char SHA (hipFile has no usable tags), so only the first
# seven go in the tag -- enough to keep two hipFile pins from colliding without
# making the tag unreadable.
hipfile="$(_arg HIPFILE_REF | sed -e 's#[^A-Za-z0-9._-]#-#g' | cut -c1-7)"
# Same treatment for fio, and for the same reason: it is pinned to a SHA on a
# ROCm topic branch with no tags, an empty value is the supported "no fio in
# this image" configuration, and an image with fio is not interchangeable with
# one without it.
fio="$(_arg FIO_REF | sed -e 's#[^A-Za-z0-9._-]#-#g' | cut -c1-7)"

printf '%s-rocm%s-nixl%s-mori%s%s%s\n' "${version}" "${rocm}" "${nixl}" "${mori}" \
	"${hipfile:+-hipfile${hipfile}}" "${fio:+-fio${fio}}"
