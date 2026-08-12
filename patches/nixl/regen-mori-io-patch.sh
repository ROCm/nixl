#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Regenerate 01-nixl-mori-io-backend.patch from a NIXL work tree.
#
# The patch is large and mostly new files, so it is developed as a real
# checkout rather than by editing the .patch: clone NIXL at the pinned ref,
# `git apply` the current patch, edit, then run this to export it again with
# its comment header preserved.
#
#   WORK=/tmp/nixl-mori-work/nixl
#   git clone --depth 1 --branch v1.3.2 https://github.com/ai-dynamo/nixl.git "$WORK"
#   git -C "$WORK" apply patches/nixl/01-nixl-mori-io-backend.patch
#   # ...edit $WORK...
#   patches/nixl/regen-mori-io-patch.sh "$WORK"
#
# Then `make patch-check COMPONENT=nixl` and `make build-nixl`.
set -euo pipefail

PATCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PATCH="${PATCH_DIR}/01-nixl-mori-io-backend.patch"
WORK="${1:-/tmp/nixl-mori-work/nixl}"

# Paths the patch owns.  Keep in sync with the patch header's regenerate note.
PATHS=(meson.build meson_options.txt src/plugins/meson.build src/plugins/mori_io)

[[ -d "${WORK}/.git" ]] || {
	echo "usage: $0 <nixl-work-tree>   (not a git checkout: ${WORK})" >&2
	exit 1
}
[[ -f "${PATCH}" ]] || {
	echo "missing ${PATCH}" >&2
	exit 1
}

# The comment header above the first `diff --git` is hand-written prose that
# git cannot regenerate, so carry it across verbatim.
header="$(sed '/^diff --git /,$d' "${PATCH}")"

git -C "${WORK}" add -A -- "${PATHS[@]}"
{
	printf '%s\n' "${header}"
	git -C "${WORK}" diff --cached --no-renames -- "${PATHS[@]}"
} > "${PATCH}.tmp"

mv -f "${PATCH}.tmp" "${PATCH}"
echo "regenerated ${PATCH} ($(wc -l < "${PATCH}") lines)"
