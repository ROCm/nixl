#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Host-side dry run of every patchset against its pinned upstream tag, without
# building an image.  Clones each component into $WORK_DIR (cached between
# runs, re-cloned when the ref changes) and `git apply --check`s the patches in
# the same lexical order the Dockerfile uses.
#
# Run it after bumping NIXL_REF/MORI_REF, or after adding a patch, to find
# rebases in seconds instead of after a 30-minute image build.
#
#   make patch-check                 # both components
#   make patch-check COMPONENT=nixl  # just one
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
DOCKERFILE="${DOCKERFILE:-${REPO_ROOT}/docker/Dockerfile}"
WORK_DIR="${WORK_DIR:-${REPO_ROOT}/build/patch-check}"
COMPONENT="${COMPONENT:-all}"

_arg() {
	if [[ -v "$1" ]]; then
		printf '%s\n' "${!1}"
	else
		grep -E "^ARG $1=" "${DOCKERFILE}" | head -1 | cut -d= -f2-
	fi
}

git config --global advice.detachedHead false 2>/dev/null || true
mkdir -p "${WORK_DIR}"

rc=0

# Undo a previous run: `git checkout -- .` only restores tracked files, so a
# patch that ADDS files (a new plugin tree, say) would leave them behind and the
# next run would fail with "already exists in working directory" -- a false
# FAIL that looks exactly like a genuine rebase conflict.  git clean removes
# them; -e keeps the ref marker this script writes.
reset_tree() {
	git -C "$1" checkout --quiet -- . 2>/dev/null || true
	git -C "$1" clean --quiet -fd -e .patch-check-ref 2>/dev/null || true
}

check_one() {
	local name="$1" url="$2" ref="$3"
	local patch_dir="${REPO_ROOT}/patches/${name}"
	local src="${WORK_DIR}/${name}"

	echo "=== ${name} ${ref} ==="

	# Re-clone when the checkout is missing or pinned to a different ref.
	local have=""
	[[ -d "${src}/.git" ]] && have="$(cat "${src}/.patch-check-ref" 2>/dev/null || true)"
	if [[ "${have}" != "${ref}" ]]; then
		rm -rf "${src}"
		if ! git clone --quiet --depth 1 --branch "${ref}" "${url}" "${src}" 2>/dev/null; then
			git clone --quiet "${url}" "${src}"
			git -C "${src}" checkout --quiet --detach "${ref}"
		fi
		printf '%s\n' "${ref}" > "${src}/.patch-check-ref"
	fi
	reset_tree "${src}"

	shopt -s nullglob
	local patches=("${patch_dir}"/*.patch)
	shopt -u nullglob

	if [[ "${#patches[@]}" -eq 0 ]]; then
		echo "  (no patches in patches/${name}/ -- pristine ${ref})"
		return 0
	fi

	# --check only, and applied cumulatively so patch N is validated against
	# the tree patches 1..N-1 produce -- exactly what the image build does.
	local p
	for p in "${patches[@]}"; do
		if git -C "${src}" apply --check "${p}" 2>/dev/null && git -C "${src}" apply "${p}"; then
			echo "  OK    $(basename "${p}")"
		else
			echo "  FAIL  $(basename "${p}")  (does not apply to ${name} ${ref})"
			git -C "${src}" apply --check --verbose "${p}" 2>&1 | sed 's/^/        /' || true
			rc=1
		fi
	done
	reset_tree "${src}"
}

if [[ "${COMPONENT}" == "all" || "${COMPONENT}" == "nixl" ]]; then
	check_one nixl "$(_arg NIXL_GIT_URL)" "$(_arg NIXL_REF)"
fi
if [[ "${COMPONENT}" == "all" || "${COMPONENT}" == "mori" ]]; then
	check_one mori "$(_arg MORI_GIT_URL)" "$(_arg MORI_REF)"
fi

if [[ "${rc}" -ne 0 ]]; then
	echo "patch-check: FAILED -- rebase the patches above against the pinned refs" >&2
else
	echo "patch-check: all patchsets apply cleanly"
fi
exit "${rc}"
