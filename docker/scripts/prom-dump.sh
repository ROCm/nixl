#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Dump a run's metrics out of Prometheus into CSV.
#
# Prometheus here is a scratch TSDB in a docker volume on a compute node that
# will be handed back to Slurm, so nothing in it survives the run.  The durable
# artefact has to be a file next to the sweep CSV it explains, and this is what
# produces it.  It is also what makes the monitoring profile worth turning on
# at all for an unattended run: the Grafana dashboard is for watching, this is
# for keeping.
#
# One CSV per query, long format (t,iso,<label columns>,value), because that
# pivots in pandas or a spreadsheet without any further reshaping and joins
# against storage-sweep.csv on wallclock -- the same join key snoop-sample.sh
# was written to provide.
#
# Usage:
#   prom-dump.sh <outdir> [start_epoch] [end_epoch] [step_seconds]
#
# With no times, dumps the last hour.  Both times accept epoch seconds; the
# t_start of the first sweep row and the t_end of the last are the natural
# values, e.g.
#
#   prom-dump.sh logs/snoop-XYZ/prom \
#       "$(awk -F, 'NR==2{print $17}' logs/snoop-XYZ/storage-sweep-full.csv)" \
#       "$(awk -F, 'END{print $18}'   logs/snoop-XYZ/storage-sweep-full.csv)"
#
# Environment:
#   PROM_URL   default http://127.0.0.1:9090
set -uo pipefail

OUT_DIR="${1:?usage: prom-dump.sh <outdir> [start] [end] [step]}"
# Positional args win, environment second.  The environment path exists because
# the Makefile cannot pass an empty positional without shifting everything after
# it out of place, which would silently reinterpret an end time as a start time.
END="${3:-${PROM_END:-$(date +%s)}}"
START="${2:-${PROM_START:-$((END - 3600))}}"
STEP="${4:-${PROM_STEP:-2}}"
PROM_URL="${PROM_URL:-http://127.0.0.1:9090}"

mkdir -p "${OUT_DIR}"

if ! curl -fsS --max-time 5 "${PROM_URL}/-/ready" > /dev/null 2>&1; then
	echo "ERROR: no Prometheus at ${PROM_URL}" >&2
	echo "       start it with: docker compose -f docker/compose/bench-stack.yml --profile monitoring up -d" >&2
	exit 1
fi

# name|promql.  Kept as data rather than a pile of near-identical function
# calls so adding a series is a one-line change.
#
# Rates use a 10s window against a 2s scrape: five samples, which is the
# shortest window that is not dominated by a single scrape's jitter and still
# resolves inside a sweep point lasting tens of seconds.
QUERIES=(
	"ais_write_bps_by_pcie|sum by (pcie_id, gpu_id) (rate(ais_tx_bytes_total[10s]))"
	"ais_read_bps_by_pcie|sum by (pcie_id, gpu_id) (rate(ais_rx_bytes_total[10s]))"
	"ais_write_ops|sum by (pcie_id) (rate(ais_tx_ops_total[10s]))"
	"ais_read_ops|sum by (pcie_id) (rate(ais_rx_ops_total[10s]))"
	"ais_write_iosize_mean|sum by (pcie_id) (rate(ais_tx_io_size_bytes_sum[10s])) / clamp_min(sum by (pcie_id) (rate(ais_tx_io_size_bytes_count[10s])), 1)"
	"ais_write_lat_p99|histogram_quantile(0.99, sum by (le, pcie_id) (rate(ais_tx_latency_seconds_bucket[10s])))"
	"ais_read_lat_p99|histogram_quantile(0.99, sum by (le, pcie_id) (rate(ais_rx_latency_seconds_bucket[10s])))"
	"nvme_write_bps|sum by (device) (rate(node_disk_written_bytes_total{device=~\"nvme.*\"}[10s]))"
	"nvme_read_bps|sum by (device) (rate(node_disk_read_bytes_total{device=~\"nvme.*\"}[10s]))"
	"nixl_registered_bytes|agent_memory_registered_total"
	# NOT rate().  Despite the _total suffix these are not cumulative: they
	# rise and fall back to zero within a run (measured 0 -> 7.17e9 -> 0 ->
	# 1.78e9 -> 0), and rate() reads every fall as a counter reset and drops
	# it, rendering a 10 GB counter as a flat line.  Dump the raw value and
	# let whoever analyses it decide what the semantics are.
	"nixl_tx_bytes_raw|agent_tx_bytes_total"
	"nixl_tx_reqs_raw|agent_tx_requests_num_total"
	"nixl_xfer_time_raw|agent_xfer_time_total"
	"nixl_errors|agent_errors_total"
	# `up` last and always: it brackets every nixlbench invocation, so it is
	# the series that makes the others interpretable even when they are empty.
	"up|up"
)

# Prometheus returns one result object per label set, each with its own
# [timestamp, value] list.  Flatten to long format and union the label keys
# across every result so one file has one header.
flatten() {
	python3 -c '
import csv, json, sys, datetime

doc = json.load(sys.stdin)
if doc.get("status") != "success":
    sys.stderr.write("query failed: %s\n" % doc.get("error", "?"))
    sys.exit(2)
results = doc["data"]["result"]
if not results:
    sys.exit(3)

keys = sorted({k for r in results for k in r["metric"] if k != "__name__"})
w = csv.writer(sys.stdout)
w.writerow(["t", "iso", *keys, "value"])
for r in results:
    m = r["metric"]
    row_labels = [m.get(k, "") for k in keys]
    for ts, val in r["values"]:
        iso = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()
        w.writerow([f"{ts:.0f}", iso, *row_labels, val])
'
}

echo "=== prom-dump ${PROM_URL} ==="
printf 'window: %s .. %s (%ss, step %ss)\n' \
	"$(date -Is -d "@${START}")" "$(date -Is -d "@${END}")" "$((END - START))" "${STEP}"

nwritten=0
nempty=0
for entry in "${QUERIES[@]}"; do
	name="${entry%%|*}"
	expr="${entry#*|}"
	out="${OUT_DIR}/${name}.csv"

	body="$(curl -fsS --max-time 60 -G "${PROM_URL}/api/v1/query_range" \
		--data-urlencode "query=${expr}" \
		--data-urlencode "start=${START}" \
		--data-urlencode "end=${END}" \
		--data-urlencode "step=${STEP}s" 2> /dev/null)"
	rc=$?
	if [[ ${rc} -ne 0 || -z "${body}" ]]; then
		printf '  %-24s HTTP FAILED\n' "${name}"
		continue
	fi

	printf '%s' "${body}" | flatten > "${out}.tmp" 2> /dev/null
	case $? in
		0)
			mv "${out}.tmp" "${out}"
			printf '  %-24s %s rows\n' "${name}" "$(($(wc -l < "${out}") - 1))"
			nwritten=$((nwritten + 1))
			;;
		3)
			# No series matched.  Expected and not an error for whole
			# families -- ais_* is empty if no GPU-direct I/O happened,
			# nvme_* if the exporters profile was off -- so say so and
			# leave no misleading header-only file behind.
			rm -f "${out}.tmp"
			printf '  %-24s (no series)\n' "${name}"
			nempty=$((nempty + 1))
			;;
		*)
			rm -f "${out}.tmp"
			printf '  %-24s QUERY ERROR\n' "${name}"
			;;
	esac
done

# The raw exposition text of every target, kept alongside the CSVs.  Metric
# names and labels drift between hsa-snoop and NIXL releases, and six months
# from now this is the only thing that says what the names meant at the time.
for port_name in "9488:hsa-snoop" "19090:nixl" "9100:node-exporter"; do
	port="${port_name%%:*}"
	who="${port_name#*:}"
	curl -fsS --max-time 5 "http://127.0.0.1:${port}/metrics" \
		> "${OUT_DIR}/metrics-${who}.txt" 2> /dev/null ||
		rm -f "${OUT_DIR}/metrics-${who}.txt"
done

echo "=== ${nwritten} series files, ${nempty} empty, in ${OUT_DIR} ==="
[[ ${nwritten} -gt 0 ]]
