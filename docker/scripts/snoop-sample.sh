#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Poll the hsa-snoop sidecar's Prometheus endpoint into a flat timestamped TSV.
#
# There is no Prometheus server on these nodes and standing one up for an
# overnight run is more moving parts than the run is worth, so this is the
# scrape loop: one line per metric per sample, with a wallclock epoch on the
# front.  Correlating it with a sweep is then a join on time -- which is why
# storage-sweep's rows need timestamps too, and why the run wrapper tees the
# sweep's own output through a timestamper.
#
# Zero-valued series are dropped.  hsa-snoop --all exports several hundred
# series, the large majority of which stay at zero for any given workload
# (there is no RDMA traffic during a storage sweep, no SDMA during a POSIX
# run), and keeping them turns a useful file into one that has to be grepped
# before it can be read.  The cost is that a counter which is genuinely zero
# is indistinguishable from one that was never exported; for the throughput
# and latency series this report is built on, that distinction does not arise.
#
# Usage: snoop-sample.sh <out.tsv> [interval_seconds]
set -uo pipefail

OUT="${1:?usage: snoop-sample.sh <out.tsv> [interval]}"
INTERVAL="${2:-2}"
PORT="${HSA_SNOOP_PORT:-9488}"
URL="http://127.0.0.1:${PORT}/metrics"

printf 'epoch\tiso\tmetric\tvalue\n' > "${OUT}"

# Deliberately no `set -e` and no exit on a failed scrape: the sweep is the
# thing that matters, and a collector that has momentarily stopped answering
# must not take the sampler down with it and leave the rest of the night
# untraced.  A missed sample shows up as a gap in the timeline, which is
# honest and recoverable; a dead sampler is neither.
while true; do
	now="$(date +%s)"
	iso="$(date -Is)"
	if body="$(curl -fsS --max-time 5 "${URL}" 2>/dev/null)"; then
		printf '%s' "${body}" | awk -v e="${now}" -v i="${iso}" '
			/^#/ { next }
			NF < 2 { next }
			{
				v = $NF
				if (v == 0 || v == "0" || v+0 == 0) next
				name = $0
				sub(/[ \t]+[^ \t]+$/, "", name)
				printf "%s\t%s\t%s\t%s\n", e, i, name, v
			}
		' >> "${OUT}"
	else
		printf '%s\t%s\t_scrape_failed\t1\n' "${now}" "${iso}" >> "${OUT}"
	fi
	sleep "${INTERVAL}"
done
