#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Clone one upstream component at a pinned ref, then apply every *.patch from
# its patchset directory in lexical order.  Used for both NIXL and MORI so the
# clone/patch semantics are identical for each.
#
# Required env:
#   SRC_NAME       Component name, used in log/error messages (e.g. nixl)
#   SRC_GIT_URL    HTTPS clone URL (git@ is rejected -- no ssh keys in layers)
#   SRC_REF        Tag, branch, or full SHA to build from
#   SRC_DEST       Checkout destination (wiped first)
# Optional env:
#   SRC_PATCH_DIR  Directory of *.patch files; missing/empty = no patches
#   SRC_SUBMODULES 1 to also init submodules (shallow, recursive)
set -euo pipefail

: "${SRC_NAME:?SRC_NAME is required}"
: "${SRC_GIT_URL:?SRC_GIT_URL is required}"
: "${SRC_REF:?SRC_REF is required}"
: "${SRC_DEST:?SRC_DEST is required}"
SRC_PATCH_DIR="${SRC_PATCH_DIR:-}"
SRC_SUBMODULES="${SRC_SUBMODULES:-0}"

if [[ "${SRC_GIT_URL}" == git@* ]]; then
	echo "ERROR: git@ ${SRC_NAME^^}_GIT_URL is not supported in Dockerfile layers; use HTTPS." >&2
	exit 1
fi

git config --global advice.detachedHead false

rm -rf "${SRC_DEST}"

# --depth 1 --branch works for tags and branches but not for a raw SHA; fall
# back to a full clone + checkout so SRC_REF can also be a commit.
if ! git clone --depth 1 --branch "${SRC_REF}" "${SRC_GIT_URL}" "${SRC_DEST}" 2>/dev/null; then
	echo "[${SRC_NAME}] shallow clone of ref '${SRC_REF}' failed; retrying as a full clone"
	rm -rf "${SRC_DEST}"
	git clone "${SRC_GIT_URL}" "${SRC_DEST}"
	git -C "${SRC_DEST}" checkout --detach "${SRC_REF}"
fi

echo "[${SRC_NAME}] cloned ${SRC_GIT_URL} ref=${SRC_REF} sha=$(git -C "${SRC_DEST}" rev-parse HEAD)"

if [[ "${SRC_SUBMODULES}" == "1" ]]; then
	# --depth 1 keeps the 3rdparty checkouts small.  MORI's optional SPDK
	# submodule is excluded here: it is huge and only needed for
	# BUILD_UMBP_SPDK=ON, which drives its own selective checkout.
	# shellcheck disable=SC2046  # word splitting into separate paths is intended
	git -C "${SRC_DEST}" submodule update --init --recursive --depth 1 \
		-- $(git -C "${SRC_DEST}" config --file .gitmodules --get-regexp '^submodule\..*\.path$' |
			awk '$2 != "3rdparty/spdk" { print $2 }')
	echo "[${SRC_NAME}] submodules initialised"
fi

if [[ -z "${SRC_PATCH_DIR}" || ! -d "${SRC_PATCH_DIR}" ]]; then
	echo "[${SRC_NAME}] no patch directory -- building pristine ${SRC_REF}"
	exit 0
fi

shopt -s nullglob
patches=("${SRC_PATCH_DIR}"/*.patch)
shopt -u nullglob

if [[ "${#patches[@]}" -eq 0 ]]; then
	echo "[${SRC_NAME}] ${SRC_PATCH_DIR} holds no *.patch -- building pristine ${SRC_REF}"
	exit 0
fi

# Lexical order: the NN- filename prefixes fix the sequence.
for patch in "${patches[@]}"; do
	if git -C "${SRC_DEST}" apply --verbose "${patch}"; then
		echo "[${SRC_NAME}] applied $(basename "${patch}")"
	else
		echo "ERROR: ${SRC_NAME} patch does not apply cleanly: ${patch}" >&2
		echo "The ${SRC_NAME} pin (${SRC_REF}) likely drifted; rebase this patch against it." >&2
		exit 1
	fi
done

echo "[${SRC_NAME}] ${#patches[@]} patch(es) applied on top of ${SRC_REF}"
