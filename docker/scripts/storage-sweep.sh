#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Sweep the NVMe storage backends across a config matrix and emit one CSV.
#
# Session 1 measured a single point per backend -- one drive, WRITE only,
# one thread, VRAM -- and concluded "AIS_MT and POSIX both saturate the drive,
# the difference is submit cost".  That is true and also the least interesting
# thing that can be said, because it is a statement about one drive.  This node
# has sixteen, each separately mounted, so the question worth answering is where
# each path stops scaling and why.
#
# Runs INSIDE the container: one `docker run`, many nixlbench pairs, so the
# matrix costs container startup once rather than once per point.
#
# Each row of the matrix becomes many CSV rows, one per block size, with the
# config columns repeated so the result can be pivoted any which way.
#
# Environment:
#   SWEEP_OUT      CSV output path            (default: /work/storage-sweep.csv)
#   SWEEP_SET      which matrix to run: quick | full | drives | threads | rw
#                                             (default: full)
#   SWEEP_MOUNTS   glob for the NVMe mounts   (default: /mnt/nixl-nvme-*)
#   SWEEP_ITER     --num_iter                 (default: 1000)
#   TIMEOUT        per-point timeout, seconds (default: 600)
set -uo pipefail

SWEEP_OUT="${SWEEP_OUT:-/work/storage-sweep.csv}"
SWEEP_SET="${SWEEP_SET:-full}"
SWEEP_ITER="${SWEEP_ITER:-1000}"
TIMEOUT="${TIMEOUT:-600}"

shopt -s nullglob
MOUNTS=(${SWEEP_MOUNTS:-/mnt/nixl-nvme-*})
shopt -u nullglob
if [[ "${#MOUNTS[@]}" -eq 0 ]]; then
	echo "ERROR: no NVMe mounts matched ${SWEEP_MOUNTS:-/mnt/nixl-nvme-*}" >&2
	echo "       the container needs them bind-mounted (-v /mnt:/mnt)" >&2
	exit 2
fi
echo "sweep: ${#MOUNTS[@]} NVMe mounts visible: ${MOUNTS[0]} ... ${MOUNTS[-1]}"

# One file per drive, so `nixlbench --filenames a,b,c` spreads the I/O over
# distinct block devices rather than distinct inodes on the same one.  This is
# the whole point of the multi-drive rows: a single drive tops out around
# 7 GB/s and any conclusion drawn there is a conclusion about the drive.
build_filenames() {
	local n="$1" i out=""
	for ((i = 0; i < n; i++)); do
		local m="${MOUNTS[i % ${#MOUNTS[@]}]}"
		mkdir -p "${m}/sweep" 2> /dev/null || true
		out+="${out:+,}${m}/sweep/nixlbench.${i}.dat"
	done
	printf '%s\n' "${out}"
}

if [[ ! -f "${SWEEP_OUT}" ]]; then
	echo "label,backend,api,seg_type,op_type,threads,files,direct,block_bytes,batch,bw_gbps,lat_us,prep_us,post_us,tx_us,p99_tx_us" \
		> "${SWEEP_OUT}"
fi

npoints=0
nfail=0

# Run one matrix point, then scrape the result table.
#
# ONE process, unlike the UCX/MORI benchmarks.  nixlbench prints "Using null
# runtime for storage backend without ETCD ... expecting 1 total" -- a storage
# backend has no remote peer, the file is the far end, so there is nobody to
# pair with.  Starting a second rank the way `make bench` does just puts two
# processes on the same file and the loser dies in registerMem.
run_point() {
	local label="$1" backend="$2" api="$3" seg="$4" op="$5" threads="$6" files="$7" direct="$8"

	local filenames
	filenames="$(build_filenames "${files}")"

	# num_iter must be divisible by num_threads or nixlbench silently rounds it
	# and the warning is easy to miss in a 40-row sweep.
	local iter=$((SWEEP_ITER - (SWEEP_ITER % threads)))

	local args=(
		--backend "${backend}"
		--initiator_seg_type "${seg}" --target_seg_type "${seg}"
		--op_type "${op}"
		--num_threads "${threads}"
		--num_files "${files}"
		--filenames "${filenames}"
		--storage_enable_direct "$([[ "${direct}" == "1" ]] && echo true || echo false)"
		--num_iter "${iter}"
		--start_block_size 4096 --max_block_size 67108864
	)
	[[ "${backend}" == "POSIX" ]] && args+=(--posix_api_type "${api}")

	echo "--- ${label}: ${backend}${api:+/${api}} seg=${seg} op=${op} thr=${threads} files=${files} direct=${direct}"

	local out_t
	out_t="$(mktemp)"
	HIP_VISIBLE_DEVICES=0 timeout "${TIMEOUT}" nixlbench "${args[@]}" > "${out_t}" 2>&1
	local rc_t=$?

	local rows
	rows="$(cat "${out_t}" | awk -v L="${label}" -v B="${backend}" -v A="${api}" \
		-v S="${seg}" -v O="${op}" -v T="${threads}" -v F="${files}" -v D="${direct}" '
		/^[0-9]+[ \t]+[0-9]+[ \t]+[0-9.]+/ {
			printf "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n",
				L,B,A,S,O,T,F,D,$1,$2,$3,$4,$5,$7,$9,$10
		}')"

	if [[ -z "${rows}" ]]; then
		echo "    FAILED (rc=${rc_t}, no result rows)"
		sed -n '1,14p;$p' "${out_t}" | sed 's/^/      | /'
		mkdir -p /work/logs/sweep-fail
		cp "${out_t}" "/work/logs/sweep-fail/${label}-${backend}${api}-${seg}-${op}-t${threads}-f${files}.log"
		nfail=$((nfail + 1))
		rm -f "${out_t}"
		return 1
	fi
	printf '%s\n' "${rows}" >> "${SWEEP_OUT}"
	# Peak is the headline; print it so a tail -f of the log is informative.
	printf '    peak %s GB/s @ %s\n' \
		"$(printf '%s\n' "${rows}" | awk -F, '{if($11>m){m=$11;b=$9}}END{printf "%.2f",m}')" \
		"$(printf '%s\n' "${rows}" | awk -F, '{if($11>m){m=$11;b=$9}}END{printf "%d B",b}')"
	npoints=$((npoints + 1))
	rm -f "${out_t}"
	return 0
}

# --- the matrices -----------------------------------------------------------
#
# Kept small and named, because the full cross product is hours and most of it
# is uninformative.  Each set answers one question.
#
# SEGMENT TYPE IS NOT A FREE VARIABLE.  Each backend supports exactly one:
#
#   AIS_MT  VRAM only.  It registers the buffer with hipFileBufRegister, which
#           rejects host memory: "hipFileBufRegister failed (err=5013); set
#           HIPFILE_ALLOW_COMPAT_MODE=true to allow fallback".
#   POSIX   DRAM only.  registerMem fails outright on a VRAM segment; the
#           backend is read()/write()/io_uring against host buffers.
#
# So "AIS_MT vs POSIX" is unavoidably "GPU-direct from VRAM vs host I/O from
# DRAM", and the POSIX column is the optimistic one: a real KV-cache offload
# whose data lives in VRAM would need a device-to-host copy that these POSIX
# numbers do not include.  Comparisons below are written with that in mind.

case "${SWEEP_SET}" in
	quick) # smoke test: does each path still work at all
		run_point base AIS_MT "" VRAM WRITE 1 1 1
		run_point base POSIX AIO DRAM WRITE 1 1 1
		;;

	rw) # READ vs WRITE.  Session 1 only ever measured WRITE (the nixlbench
		# default), and NVMe reads are typically ~2x writes, so the read
		# direction -- the one a KV-cache load actually uses -- is unmeasured.
		for op in WRITE READ; do
			run_point "rw" AIS_MT "" VRAM "${op}" 1 1 1
			run_point "rw" POSIX AIO DRAM "${op}" 1 1 1
			run_point "rw" POSIX URING DRAM "${op}" 1 1 1
			run_point "rw" POSIX POSIXAIO DRAM "${op}" 1 1 1
		done
		;;

	threads) # where each backend's submit path stops scaling.  Single drive,
		# so anything above ~7 GB/s is impossible and the question is
		# purely how few threads it takes to get there.
		for thr in 1 2 4 8 16; do
			run_point "threads" AIS_MT "" VRAM WRITE "${thr}" 1 1
			run_point "threads" POSIX AIO DRAM WRITE "${thr}" 1 1
			run_point "threads" POSIX URING DRAM WRITE "${thr}" 1 1
		done
		;;

	drives) # the real question: aggregate bandwidth.  One drive caps near
		# 7 GB/s, so a single-drive result says nothing about whether
		# either submit path scales out across sixteen.
		#
		# Thread count is not free here: nixlbench allocates one buffer
		# per thread and refuses to start if there are fewer buffers than
		# files ("number of buffers (8) needs to be bigger or equal to
		# the number of files (16)").  So threads tracks files upward.
		for f in 1 2 4 8 16; do
			thr=$((f > 8 ? f : 8))
			run_point "drives" AIS_MT "" VRAM WRITE "${thr}" "${f}" 1
			run_point "drives" POSIX AIO DRAM WRITE "${thr}" "${f}" 1
			run_point "drives" POSIX URING DRAM WRITE "${thr}" "${f}" 1
		done
		;;

	wide) # Disentangle two things that moved together in the `drives` set.
		# POSIX peaked at 48 GB/s on 8 drives / 8 threads and fell to
		# 23 GB/s on 16 drives / 16 threads, while AIS_MT held at 56.
		# Either the second eight drives are slower (they are a different
		# model), or POSIX's submit path degrades past 8 threads.  Vary
		# one at a time.
		run_point "wide" POSIX AIO DRAM WRITE 16 8 1   # 16 threads, first 8 drives
		run_point "wide" POSIX AIO DRAM WRITE 32 16 1  # more threads than drives
		run_point "wide" AIS_MT "" VRAM WRITE 16 8 1
		run_point "wide" AIS_MT "" VRAM WRITE 32 16 1
		;;

	readscale) # READ is the direction a KV-cache load uses, and it is ~2x
		# WRITE on one drive, so the aggregate ceiling has to be
		# measured separately rather than inferred from the write
		# scaling curve.  Run the write set first: READ needs the
		# files to already exist.
		for f in 1 4 8 16; do
			thr=$((f > 8 ? f : 8))
			run_point "readscale" AIS_MT "" VRAM READ "${thr}" "${f}" 1
			run_point "readscale" POSIX AIO DRAM READ "${thr}" "${f}" 1
		done
		;;

	direct) # O_DIRECT on vs off.  Buffered I/O reads back out of page cache
		# and reports numbers that are about DRAM, not about the drive;
		# worth quantifying once so the size of the lie is on record.
		for d in 1 0; do
			run_point "direct" AIS_MT "" VRAM WRITE 8 4 "${d}"
			run_point "direct" POSIX AIO DRAM WRITE 8 4 "${d}"
		done
		;;

	full)
		for op in WRITE READ; do
			for f in 1 4 16; do
				run_point "full" AIS_MT "" VRAM "${op}" 8 "${f}" 1
				run_point "full" POSIX AIO DRAM "${op}" 8 "${f}" 1
			done
		done
		;;
	*)
		echo "ERROR: unknown SWEEP_SET=${SWEEP_SET}" >&2
		exit 2
		;;
esac

echo
echo "=== sweep ${SWEEP_SET}: ${npoints} points ok, ${nfail} failed -> ${SWEEP_OUT} ==="
exit 0
