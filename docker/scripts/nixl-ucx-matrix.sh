#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Does the UCX intra-node GPU fix carry through to NIXL?
#
# The ucx_perftest bisect showed UCX_RMA_PPLN_ENABLE=y is sufficient on its own:
# every rocm_ipc capability-flag subset gave the same ~43.7 GB/s, including the
# empty one.  But ucx_perftest does not request peer error handling and NIXL
# does, and peer error handling is exactly what pulls UCT_MD_FLAG_INVALIDATE_RMA
# into ucp_wireup_add_rma_bw_lanes' criteria -- the filter the capability-parity
# patch exists to clear.  So the flags may still be load-bearing for NIXL even
# though they are dead weight for perftest.
#
# This runs the 2x2x2: {ppln on, off} x {err peer, none} x {flags ir, none},
# plus MORI_IO as the reference, and prints peak bandwidth per cell.
#
# Runs INSIDE the container.
#
# Environment:
#   MATRIX_SEG    VRAM or DRAM       (default: VRAM)
#   MATRIX_TMO    per-cell timeout   (default: 300)
set -uo pipefail

MATRIX_SEG="${MATRIX_SEG:-VRAM}"
MATRIX_TMO="${MATRIX_TMO:-300}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ground truth for this node, so every row can be read as a fraction of what the
# hardware can actually do rather than as a bare number.
echo "reference: hipMemcpyPeer GPU0->GPU1 measured at 45.7 GB/s on this node"
echo

printf '%-10s %-6s %-7s %-8s %-10s %s\n' BACKEND PPLN ERRMODE FLAGS "PEAK GB/s" NOTE
printf '%s\n' "---------------------------------------------------------------------------"

cell() {
	local backend="$1" ppln="$2" errmode="$3" flags="$4"
	local log
	log="$(mktemp)"

	# run-nixlbench.sh owns the pair setup, the free-port probe and -- since the
	# HIP/ROCR mix-up was found -- the device isolation.  Driving it rather than
	# nixlbench directly keeps this script honest about measuring the same thing
	# `make bench` measures.
	UCX_RMA_PPLN_ENABLE="${ppln}" \
		ROCM_IPC_EXP="${flags}" \
		UCX_TLS="rocm_ipc,rocm_copy,tcp,sysv,posix,cma" \
		BACKEND="${backend}" SEG_TYPE="${MATRIX_SEG}" TIMEOUT="${MATRIX_TMO}" \
		EXTRA_ARGS="${errmode:+--ucx_error_handling_mode ${errmode}}" \
		bash "${SCRIPT_DIR}/run-nixlbench.sh" > "${log}" 2>&1
	local rc=$?

	# Peak over the block-size sweep: column 3 of the result table.
	local peak
	peak="$(grep -hE '^\[[a-z]+\] [0-9]+ +[0-9]+ +[0-9.]+' "${log}" |
		awk '{ if ($4 > m) m = $4 } END { if (m > 0) printf "%.1f", m }')"

	# Which transport UCP actually used, so a fast row can be attributed and a
	# slow one explained.
	local note=""
	if [[ "${backend}" == "UCX" ]]; then
		grep -q 'rocm_ipc' "${log}" && note="rocm_ipc" || note="no rocm_ipc"
	fi
	[[ -z "${peak}" ]] && { peak="FAIL"; note="rc=${rc}"; }

	printf '%-10s %-6s %-7s %-8s %-10s %s\n' \
		"${backend}" "${ppln}" "${errmode:-default}" "${flags:-none}" "${peak}" "${note}"

	if [[ "${peak}" == "FAIL" ]]; then
		mkdir -p /work/logs/matrix-logs
		cp "${log}" "/work/logs/matrix-logs/${backend}-${ppln}-${errmode:-def}-${flags:-none}.log"
	fi
	rm -f "${log}"
}

# The two rows that matter most are the first and the last UCX row: NIXL as it
# ships today, and NIXL with the one-line gate flipped.
cell UCX n peer ""
cell UCX n peer "ir"
cell UCX y peer ""
cell UCX y peer "ir"
cell UCX y none ""
cell UCX y peer "irake"

# MORI_IO does not go through UCX at all, so it is the control: whatever it
# reports is what this node's IPC path is worth end to end through nixlbench.
cell MORI_IO n "" ""
