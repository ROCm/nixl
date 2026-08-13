#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Bisect which rocm_ipc capability flag makes UCP's GPU RMA path hang.
#
# Session 1 established two half-fixes for the intra-node GPU RMA gap:
#   1. a cuda_ipc-parity flag set on rocm_ipc, which makes it eligible for an
#      rma_bw lane, and
#   2. UCX_RMA_PPLN_ENABLE=y, which makes UCP's protocol layer pick the RMA
#      rendezvous protocol instead of software emulation.
# Either alone is harmless and useless.  Both together hang.
#
# The parity patch changes six things at once, so "the parity patch hangs" is
# not a finding.  This runs the cross product of flag subsets against the gate,
# using the runtime-gated build (patches/ucx/experiments/), and reports for each
# cell: the protocol UCP chose, the bandwidth, or HANG.
#
# Runs INSIDE the container.  Needs the runtime-gated UCX build; with stock UCX
# every row is identical and that is the tell.
#
# Environment:
#   BISECT_SIZE   message size            (default: 8388608)
#   BISECT_ITERS  iterations              (default: 200)
#   BISECT_TMO    per-cell timeout, sec   (default: 60)
#   BISECT_TEST   ucx_perftest -t value   (default: ucp_put_bw)
#   BISECT_FLAGS  space list of ROCM_IPC_EXP values to try
#   BISECT_PPLN   space list of UCX_RMA_PPLN_ENABLE values (default: "n y")
set -uo pipefail

BISECT_SIZE="${BISECT_SIZE:-8388608}"
BISECT_ITERS="${BISECT_ITERS:-200}"
BISECT_TMO="${BISECT_TMO:-60}"
BISECT_TEST="${BISECT_TEST:-ucp_put_bw}"
BISECT_PPLN="${BISECT_PPLN:-n y}"

# The subsets are ordered so the first hang localises the cause:
#   ""      stock upstream, the control
#   e       iface ERRHANDLE_PEER_FAILURE alone
#   ir      INVALIDATE + INVALIDATE_RMA -- the minimum for rma_bw eligibility
#   ira     + INVALIDATE_AMO
#   irk     + MD RKEY_PTR, without the component flag
#   irkc    + UCT_COMPONENT_FLAG_RKEY_PTR, which is what actually lets UCP
#           build an rkey_ptr lane; prime suspect for the hang
#   ire     the minimal set plus peer failure -- the combination NIXL needs
#   irake   everything EXCEPT the component flag
#   irakec  everything, i.e. the session-1 parity patch
BISECT_FLAGS="${BISECT_FLAGS:-_ e ir ire ira irk irkc irake irakec}"

# DEVICE ISOLATION: ROCR_VISIBLE_DEVICES, *not* HIP_VISIBLE_DEVICES.
# UCX talks to ROCr/HSA directly and never goes through the HIP runtime, so
# HIP_VISIBLE_DEVICES does not mask the HSA agent list -- both ranks end up on
# the same physical GPU and the "peer" number is really a same-device copy.
# That is how session 1 came to believe rocm_ipc did 511 GB/s GPU-to-GPU on a
# node whose measured peer ceiling is 45.7 GB/s.

# rocm_ipc must be reachable, and self must be excluded or UCX shortcuts the
# whole question by using the loopback transport.
export UCX_TLS="${UCX_TLS:-rocm_ipc,rocm_copy,tcp,sysv,posix,cma}"

printf '%-8s %-6s %-10s %-12s %s\n' FLAGS PPLN RESULT "BW(GB/s)" PROTOCOL
printf '%s\n' "--------------------------------------------------------------------------------"

for ppln in ${BISECT_PPLN}; do
	for flags in ${BISECT_FLAGS}; do
		# "_" stands for the empty string: an empty word cannot survive the
		# unquoted word split that builds this list.
		exp=""
		[[ "${flags}" != "_" ]] && exp="${flags}"

		srv="$(mktemp)"
		cli="$(mktemp)"
		port=$((23000 + RANDOM % 2000))

		ROCM_IPC_EXP="${exp}" UCX_RMA_PPLN_ENABLE="${ppln}" UCX_PROTO_INFO=y \
			ROCR_VISIBLE_DEVICES=0 \
			timeout -k 5 "${BISECT_TMO}" ucx_perftest -p "${port}" \
			-t "${BISECT_TEST}" -m rocm,rocm -s "${BISECT_SIZE}" -n "${BISECT_ITERS}" \
			> "${srv}" 2>&1 &
		srv_pid=$!
		sleep 2
		ROCM_IPC_EXP="${exp}" UCX_RMA_PPLN_ENABLE="${ppln}" UCX_PROTO_INFO=y \
			ROCR_VISIBLE_DEVICES=1 \
			timeout -k 5 "${BISECT_TMO}" ucx_perftest -p "${port}" localhost \
			-t "${BISECT_TEST}" -m rocm,rocm -s "${BISECT_SIZE}" -n "${BISECT_ITERS}" \
			> "${cli}" 2>&1 &
		cli_pid=$!

		wait "${srv_pid}"
		srv_rc=$?
		wait "${cli_pid}"
		cli_rc=$?

		# `timeout` reports 124; that is the hang signature this whole script
		# exists to localise, so it is a result and not an error.
		result=ok
		if [[ "${srv_rc}" == "124" || "${cli_rc}" == "124" ]]; then
			result=HANG
		elif [[ "${srv_rc}" != "0" && "${cli_rc}" != "0" ]]; then
			result="err${cli_rc}"
		fi

		# perftest's summary line:
		#   Final:  <iters> <50%ile> <avg> <overall> <avg MB/s> <overall MB/s> ...
		# Take the overall bandwidth column and report GB/s.
		bw="$(grep -h '^Final:' "${cli}" "${srv}" 2> /dev/null |
			awk '{ printf "%.1f", $7/1000; exit }')"

		# The protocol UCP picked for the put, from UCX_PROTO_INFO.  This is the
		# line that matters: "software emulation | tcp" is the bug, anything
		# naming rocm_ipc is the fix.
		# The protocol chosen for the rocm->rocm put specifically.  There are
		# several ucp_put tables (host->host, host->rocm, ...) and only the
		# rocm-to-rocm one answers the question, so match on that header and
		# take the config for the largest size range under it.
		proto="$(awk '
			/remote memory write by ucp_put.*from rocm.*to rocm/ { want=1; next }
			want && /\| *[0-9]+\.\.inf/ {
				n = split($0, f, "|")
				desc = f[n-2]; cfg = f[n-1]
				gsub(/^ +| +$/, "", desc); gsub(/^ +| +$/, "", cfg)
				print desc " | " cfg; exit
			}
			want && /\+---/ && seen++ > 2 { want=0 }
		' "${cli}" "${srv}" 2> /dev/null | head -1 | cut -c1-56)"

		printf '%-8s %-6s %-10s %-12s %s\n' "${flags}" "${ppln}" "${result}" "${bw:--}" "${proto:--}"

		# Keep the raw output of anything anomalous; the summary line is not
		# enough to debug a hang from.
		if [[ "${result}" != "ok" ]]; then
			mkdir -p /work/logs/bisect-logs
			cp "${srv}" "/work/logs/bisect-logs/${BISECT_TEST}-${flags}-ppln${ppln}-srv.log"
			cp "${cli}" "/work/logs/bisect-logs/${BISECT_TEST}-${flags}-ppln${ppln}-cli.log"
		fi
		rm -f "${srv}" "${cli}"
	done
done
