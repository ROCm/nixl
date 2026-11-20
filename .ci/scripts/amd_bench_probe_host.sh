#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Keeps the outcome of each check a job of .github/workflows/amd-bench-probe.yml
# makes, and writes them out at the end of the job.
#
#   amd_bench_probe_host.sh record CHECK STATUS [DETAIL]
#   amd_bench_probe_host.sh ingest FILE
#   amd_bench_probe_host.sh runner LABEL
#   amd_bench_probe_host.sh finish PROBE [LANE]
#   amd_bench_probe_host.sh failures
#
# record  adds one outcome. STATUS is pass, fail, warn, skip or info: fail is a
#         check the job must not pass without, warn one that may fail.
# ingest  adds the outcomes a guest script printed, one per line of FILE that
#         reads PROBE<tab>CHECK<tab>STATUS<tab>DETAIL.
# runner  adds the free disk space, CPU count and memory of the machine it
#         runs on, as info, under LABEL.
# finish  writes them to $PROBE_DIR/checks.json and prints them as a Markdown
#         table.
# failures prints the checks recorded as fail and exits 1 if there are any.
#
# The outcomes are kept in checks.tsv under $PROBE_DIR, which must be set. Only
# bash, coreutils and awk are used, so it runs in a bare job container.

set -euo pipefail

: "${PROBE_DIR:?set PROBE_DIR to the directory that holds the results}"
tsv="${PROBE_DIR}/checks.tsv"

# One line of printable ASCII, so that a field needs no more than its
# backslashes and quotes escaped to be a JSON string.
clean() {
  printf '%s' "$1" | LC_ALL=C tr '\t\r\n' '   ' | LC_ALL=C tr -cd '\40-\176'
}

json_string() {
  local s=$1
  s=${s//\\/\\\\}
  s=${s//\"/\\\"}
  printf '"%s"' "$s"
}

record() {
  local check status detail
  check=$(clean "$1")
  check=${check:-unnamed}
  status=$(clean "$2")
  detail=$(clean "${3:-}")
  case "$status" in
    pass|fail|warn|skip|info) ;;
    *)
      echo "amd_bench_probe_host.sh: '${status}' is not a status (check ${check})" >&2
      return 2
      ;;
  esac
  mkdir -p "$PROBE_DIR"
  printf '%s\t%s\t%s\n' "$check" "$status" "$detail" >> "$tsv"
  case "$status" in
    fail) echo "::error title=AMD Bench Probe::${check}: ${detail}" ;;
    warn) echo "::warning title=AMD Bench Probe::${check}: ${detail}" ;;
    *) echo "${status}: ${check}: ${detail}" ;;
  esac
}

ingest() {
  local tag check status detail
  while IFS=$'\t' read -r tag check status detail || [ -n "$tag" ]; do
    [ "$tag" = PROBE ] || continue
    record "$check" "$status" "$detail" \
      || record "$check" warn "the guest reported the status '$(clean "$status")': ${detail}"
  done < "$1"
}

runner() {
  local label=$1 path size used avail total available
  for path in / /var/lib/qemu-tool; do
    [ -d "$path" ] || continue
    read -r size used avail < <(df -BM --output=size,used,avail "$path" | tail -n 1)
    record "runner.${label}.df ${path}" info "size ${size}, used ${used}, available ${avail}"
  done
  total=$(awk '/^MemTotal:/ { print $2 }' /proc/meminfo)
  available=$(awk '/^MemAvailable:/ { print $2 }' /proc/meminfo)
  record "runner.${label}.cpu-mem" info \
    "nproc $(nproc), MemTotal ${total} kB, MemAvailable ${available} kB"
}

finish() {
  local probe=$1 lane=${2:-} check status detail sep=""
  mkdir -p "$PROBE_DIR"
  touch "$tsv"
  {
    printf '{\n  "schema": 1,\n'
    printf '  "probe": %s,\n' "$(json_string "$(clean "$probe")")"
    printf '  "lane": %s,\n' "$(json_string "$(clean "$lane")")"
    printf '  "run_id": %s,\n' "$(json_string "$(clean "${GITHUB_RUN_ID:-}")")"
    printf '  "run_attempt": %s,\n' "$(json_string "$(clean "${GITHUB_RUN_ATTEMPT:-}")")"
    printf '  "sha": %s,\n' "$(json_string "$(clean "${GITHUB_SHA:-}")")"
    printf '  "checks": ['
    while IFS=$'\t' read -r check status detail; do
      printf '%s\n    {"check": %s, "status": %s, "detail": %s}' "$sep" \
        "$(json_string "$check")" "$(json_string "$status")" "$(json_string "$detail")"
      sep=","
    done < "$tsv"
    printf '\n  ]\n}\n'
  } > "${PROBE_DIR}/checks.json"

  echo "| Check | Result | Detail |"
  echo "| --- | --- | --- |"
  while IFS=$'\t' read -r check status detail; do
    if [ "${#detail}" -gt 300 ]; then detail="${detail:0:300}..."; fi
    echo "| ${check//|/\\|} | ${status} | ${detail//|/\\|} |"
  done < "$tsv"
}

failures() {
  [ -f "$tsv" ] || return 0
  awk -F '\t' '$2 == "fail" { print $1; n++ } END { exit n > 0 }' "$tsv"
}

usage() {
  echo "usage: amd_bench_probe_host.sh record CHECK STATUS [DETAIL] | ingest FILE | runner LABEL | finish PROBE [LANE] | failures" >&2
  exit 2
}

case "${1:-}" in
  record)
    [ "$#" -ge 3 ] || usage
    record "$2" "$3" "${4:-}"
    ;;
  ingest)
    [ "$#" -eq 2 ] || usage
    ingest "$2"
    ;;
  runner)
    [ "$#" -eq 2 ] || usage
    runner "$2"
    ;;
  finish)
    [ "$#" -ge 2 ] || usage
    finish "$2" "${3:-}"
    ;;
  failures)
    failures
    ;;
  *)
    usage
    ;;
esac
