#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# The same drives as storage-sweep.sh, through fio instead of NIXL.
#
# Every AIS and AIS_MT number this tree produces is a NIXL number, and NIXL is
# a thick layer: agent, backend engine, descriptor lists, a Taskflow pool or a
# stream pool, then hipFile.  When AIS plateaus around 5.5 GB/s there is no way
# from inside nixlbench to say whether that is hipFile's ceiling on this
# hardware or NIXL's submit path.  fio's libhipfile engine drives the identical
# hipFile calls with none of that above it, so it separates the two.
#
# The mapping is one-for-one, which is what makes the comparison worth running:
#
#   hipfile_mode=sync    hipFileRead/hipFileWrite            <-> AIS_MT
#   hipfile_mode=stream  hipFileReadAsync/hipFileWriteAsync  <-> AIS
#   hipfile_mode=batch   hipFileBatchIOSubmit/GetStatus      <-> (no plugin)
#
# `batch` used to be expected to fail -- upstream hipFile accepts a submission,
# performs no I/O, and returns "Not Implemented" from GetStatus.
# patches/hipfile/01-hipfile-batch-worker-pool.patch implements it, so batch is
# now a real measurement.  It is the interesting one: `stream` is capped near
# 5 GB/s by HIP servicing every host function from a single thread per process
# (docker/scripts/hostfunc-serial.hip.cpp), and batch is the only hipFile
# submission API that is not subject to that cap.
#
# Runs INSIDE the container (fio is at /opt/fio/bin/fio, linked against the
# source-built /opt/hipfile, same library the AIS plugins use).
#
# Environment:
#   FIO_OUT      CSV output path             (default: /work/logs/fio-sweep.csv)
#   FIO_SET      which matrix: quick | modes | bs | depth | drives | full
#                                            (default: quick)
#   FIO_MOUNTS   glob for the NVMe mounts    (default: /mnt/nixl-nvme-*)
#   FIO_SIZE     per-job file size           (default: 4g)
#   FIO_RUNTIME  per-point seconds           (default: 20)
#   FIO_GPU      gpu_dev_ids value           (default: 0)
#   TIMEOUT      per-point hard timeout      (default: 300)
set -uo pipefail

FIO_OUT="${FIO_OUT:-/work/logs/fio-sweep.csv}"
FIO_SET="${FIO_SET:-quick}"
FIO_SIZE="${FIO_SIZE:-4g}"
FIO_RUNTIME="${FIO_RUNTIME:-20}"
FIO_GPU="${FIO_GPU:-0}"
TIMEOUT="${TIMEOUT:-300}"
FIO_BIN="${FIO_BIN:-fio}"

if ! command -v "${FIO_BIN}" > /dev/null 2>&1; then
	echo "ERROR: no fio in this image (built with FIO_REF empty?)" >&2
	exit 2
fi
if ! "${FIO_BIN}" --enghelp 2> /dev/null | grep -qx '[[:space:]]*libhipfile'; then
	echo "ERROR: this fio has no libhipfile ioengine:" >&2
	"${FIO_BIN}" --enghelp 2>&1 | sed 's/^/      | /' >&2
	exit 2
fi

shopt -s nullglob
MOUNTS=(${FIO_MOUNTS:-/mnt/nixl-nvme-*})
shopt -u nullglob
if [[ "${#MOUNTS[@]}" -eq 0 ]]; then
	echo "ERROR: no NVMe mounts matched ${FIO_MOUNTS:-/mnt/nixl-nvme-*}" >&2
	exit 2
fi
echo "fio-sweep: $("${FIO_BIN}" --version), ${#MOUNTS[@]} mounts, hipfile_mode=$(cat /opt/fio/share/fio-hipfile-async.txt 2> /dev/null || echo '?')"

# fio round-robins numjobs over a colon-separated directory list, so N jobs
# across M directories spreads over M block devices the same way
# storage-sweep.sh's --filenames does.
build_dirs() {
	local n="$1" i out=""
	for ((i = 0; i < n; i++)); do
		local m="${MOUNTS[i % ${#MOUNTS[@]}]}"
		mkdir -p "${m}/fio" 2> /dev/null || true
		out+="${out:+:}${m}/fio"
	done
	printf '%s\n' "${out}"
}

mkdir -p "$(dirname "${FIO_OUT}")"
if [[ ! -f "${FIO_OUT}" ]]; then
	# Deliberately shaped like storage-sweep.csv: same label/rw/threads/direct
	# /block_bytes/bw_gbps/t_start/t_end spellings, so the two files
	# concatenate for plotting with `engine` as the distinguishing column.
	echo "label,engine,mode,op_type,jobs,dirs,direct,block_bytes,iodepth,bw_gbps,iops,lat_us,p99_us,t_start,t_end,status" \
		> "${FIO_OUT}"
fi

npoints=0
nfail=0

run_point() {
	local label="$1" mode="$2" rw="$3" jobs="$4" ndirs="$5" bs="$6" depth="$7"

	local dirs
	dirs="$(build_dirs "${ndirs}")"

	local args=(
		--name="${label}"
		--ioengine=libhipfile
		--rocm_io=hipfile
		--hipfile_mode="${mode}"
		--gpu_dev_ids="${FIO_GPU}"
		--directory="${dirs}"
		# Mandatory with rocm_io=hipfile; the engine rejects buffered I/O.
		--direct=1
		# Threads, not processes: hipMalloc fails once too many processes
		# attach to the same GPU, which is how a numjobs>8 fork run dies.
		--thread
		--rw="${rw}"
		--bs="${bs}"
		--iodepth="${depth}"
		--numjobs="${jobs}"
		--size="${FIO_SIZE}"
		--runtime="${FIO_RUNTIME}"
		--time_based
		--group_reporting=1
		--output-format=json
	)

	echo "--- $(date -Is) ${label}: ${mode} ${rw} jobs=${jobs} dirs=${ndirs} bs=${bs} qd=${depth}"

	local out_t t0 t1 rc
	out_t="$(mktemp)"
	t0="$(date +%s)"
	timeout "${TIMEOUT}" "${FIO_BIN}" "${args[@]}" > "${out_t}" 2>&1
	rc=$?
	t1="$(date +%s)"

	# fio writes its JSON to stdout and its errors as plain text to the same
	# place, so a failed run is a file that does not parse.  Let python decide
	# rather than grepping for "error".
	local row
	row="$(python3 - "${out_t}" "${label}" "${mode}" "${rw}" "${jobs}" "${ndirs}" "${bs}" "${depth}" "${t0}" "${t1}" <<- 'PY'
		import json, sys

		path, label, mode, rw, jobs, ndirs, bs, depth, t0, t1 = sys.argv[1:11]
		try:
		    with open(path) as fh:
		        doc = json.load(fh)
		except Exception:
		    sys.exit(1)

		job = doc["jobs"][0]
		# fio reports read and write separately and zeroes the unused side, so
		# picking the one with bytes moved works for every rw value here.
		side = job["read"] if job["read"]["io_bytes"] else job["write"]
		bw_gbps = side["bw_bytes"] / 1e9
		# Which object holds the real latency depends on the submission mode.
		# For hipfile_mode=stream fio measures completion (clat); for sync the
		# whole call is submission, clat is ~0, and only lat_ns is meaningful.
		# Reading clat unconditionally reports a 0.3us p99 against a 278us mean.
		# Pick whichever object actually has time in it.
		cands = [side[k] for k in ("lat_ns", "clat_ns") if k in side]
		lat = max(cands, key=lambda d: d.get("mean", 0)) if cands else {}
		mean_us = lat.get("mean", 0) / 1000.0
		# p99 comes from the SAME object as the mean or not at all.  fio only
		# computes percentiles for clat, so a sync row gets a blank p99 rather
		# than clat's 0.3us sitting next to lat's 278us mean as if the two
		# described one distribution.
		pct = lat.get("percentile")
		p99_us = f"{pct['99.000000'] / 1000.0:.1f}" if pct else ""
		err = job.get("error", 0)
		bs_bytes = {"k": 1024, "m": 1024 ** 2, "g": 1024 ** 3}.get(bs[-1].lower(), 1)
		bs_bytes = int(bs[:-1]) * bs_bytes if bs[-1].isalpha() else int(bs)

		print(",".join(str(x) for x in [
		    label, "fio", mode, rw.upper(), jobs, ndirs, 1, bs_bytes, depth,
		    f"{bw_gbps:.3f}", f"{side['iops']:.0f}", f"{mean_us:.1f}",
		    p99_us, t0, t1, "ok" if not err else f"err{err}",
		]))
	PY
	)"

	if [[ -z "${row}" ]]; then
		mkdir -p /work/logs/fio-fail
		local logname="/work/logs/fio-fail/${label}-${mode}-${rw}-j${jobs}-d${ndirs}-${bs}-q${depth}.log"
		cp "${out_t}" "${logname}"
		echo "    FAILED (rc=${rc}, no parseable JSON) -> ${logname}"
		if [[ "${mode}" == "batch" ]]; then
			# Not tolerated any more: with patches/hipfile applied, batch
			# works, so a failure here means the patch did not reach this
			# image (or regressed) rather than "upstream is a stub".
			echo "      batch is implemented by patches/hipfile -- check that" \
				"/opt/hipfile is the source-built one"
		fi
		sed -n '1,12p' "${out_t}" | sed 's/^/      | /'
		nfail=$((nfail + 1))
		rm -f "${out_t}"
		return 1
	fi

	printf '%s\n' "${row}" >> "${FIO_OUT}"
	printf '    %s GB/s, %s IOPS, lat %s us mean / %s us p99 [%s]\n' \
		"$(cut -d, -f10 <<< "${row}")" "$(cut -d, -f11 <<< "${row}")" \
		"$(cut -d, -f12 <<< "${row}")" "$(cut -d, -f13 <<< "${row}")" \
		"$(cut -d, -f16 <<< "${row}")"
	npoints=$((npoints + 1))
	rm -f "${out_t}"
	return 0
}

NDRIVES="${#MOUNTS[@]}"

case "${FIO_SET}" in
	quick) # does each submission mode work at all
		run_point quick sync write 1 1 1m 1
		run_point quick stream write 1 1 1m 16
		run_point quick batch write 1 1 1m 16
		;;

	modes) # the headline comparison: sync vs stream vs batch at the shape
		# the AIS sweeps use, on every drive at once.  If stream tops out
		# here where AIS tops out in nixlbench, the ceiling is hipFile's
		# -- and batch says whether that ceiling is the submission API's
		# or the drives'.
		for rw in write read; do
			run_point modes sync "${rw}" 8 "${NDRIVES}" 1m 1
			run_point modes stream "${rw}" 8 "${NDRIVES}" 1m 16
			run_point modes batch "${rw}" 8 "${NDRIVES}" 1m 16
		done
		;;

	bs) # block size curve, matching storage-sweep's 4K..64M range
		for bs in 4k 64k 256k 1m 4m 16m; do
			run_point bs sync write 8 "${NDRIVES}" "${bs}" 1
			run_point bs stream write 8 "${NDRIVES}" "${bs}" 16
			run_point bs batch write 8 "${NDRIVES}" "${bs}" 16
		done
		;;

	depth) # stream and batch only: sync has no queue.  This is the knob
		# that corresponds to the AIS plugin's stream pool size, and the
		# one most likely to explain a plateau.  Stream is flat here --
		# HIP serialises its host functions, so depth buys nothing --
		# which is exactly the contrast batch is in the sweep for.
		for qd in 1 2 4 8 16 32 64; do
			run_point depth stream write 8 "${NDRIVES}" 1m "${qd}"
			run_point depth batch write 8 "${NDRIVES}" 1m "${qd}"
		done
		;;

	drives) # scale-out, the same question storage-sweep's `drives` set asks
		for d in 1 2 4 "${NDRIVES}"; do
			local_jobs=$((d > 8 ? d : 8))
			run_point drives sync write "${local_jobs}" "${d}" 1m 1
			run_point drives stream write "${local_jobs}" "${d}" 1m 16
			run_point drives batch write "${local_jobs}" "${d}" 1m 16
		done
		;;

	full)
		for rw in write read; do
			for d in 1 4 "${NDRIVES}"; do
				jobs=$((d > 8 ? d : 8))
				run_point full sync "${rw}" "${jobs}" "${d}" 1m 1
				run_point full stream "${rw}" "${jobs}" "${d}" 1m 16
				run_point full batch "${rw}" "${jobs}" "${d}" 1m 16
			done
		done
		;;
	*)
		echo "ERROR: unknown FIO_SET=${FIO_SET}" >&2
		exit 2
		;;
esac

echo
echo "=== fio-sweep ${FIO_SET}: ${npoints} ok, ${nfail} failed -> ${FIO_OUT} ==="
[[ "${nfail}" -eq 0 ]]
