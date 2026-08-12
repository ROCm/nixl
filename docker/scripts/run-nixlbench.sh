#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Run a two-process nixlbench pair inside the image and print the result.
#
# nixlbench is always initiator + target.  The ETCD runtime is the upstream
# default, but this image is built without etcd-cpp-api, so we use the ASIO
# runtime: two processes rendezvous over a plain TCP socket
# (--asio_address/--asio_port), which is all a single-node or two-rank pair
# needs and keeps a coordination service out of the picture.
#
# Both backends are driven through the same harness, so the only difference
# between the two runs is the --backend flag:
#
#   BACKEND=UCX     run-nixlbench.sh
#   BACKEND=MORI_IO run-nixlbench.sh
#
# Environment:
#   BACKEND        NIXL backend to drive        (default: UCX)
#   SEG_TYPE       DRAM or VRAM, both sides     (default: VRAM)
#   ASIO_ADDR      rendezvous address           (default: 127.0.0.1)
#   ASIO_PORT      rendezvous port              (default: 12345)
#   INITIATOR_GPU  HIP device for the initiator (default: 0)
#   TARGET_GPU     HIP device for the target    (default: 1 if >1 GPU, else 0)
#   EXTRA_ARGS     appended to both invocations
#   TIMEOUT        seconds before giving up     (default: 300)
#   ROLE           initiator|target|both        (default: both)
#   FILEPATH       directory for storage backends (POSIX/GDS/...); required
#                  for those, ignored otherwise
#   POSIX_API      AIO, URING or POSIXAIO       (default: AIO)
#   DIRECT_IO      1 = O_DIRECT, bypass the page cache (default: 1)
#
# ROLE lets the two halves run on different NODES: start `ROLE=target` on one
# and `ROLE=initiator ASIO_ADDR=<target-host>` on the other.  `both` runs the
# pair locally and is what `make bench` uses.
set -uo pipefail

BACKEND="${BACKEND:-UCX}"
SEG_TYPE="${SEG_TYPE:-VRAM}"
ASIO_ADDR="${ASIO_ADDR:-127.0.0.1}"
ASIO_PORT="${ASIO_PORT:-12345}"
TIMEOUT="${TIMEOUT:-300}"
ROLE="${ROLE:-both}"
EXTRA_ARGS="${EXTRA_ARGS:-}"

_gpus="$(rocm_agent_enumerator 2>/dev/null | grep -c '^gfx' || echo 0)"
INITIATOR_GPU="${INITIATOR_GPU:-0}"
if [[ -z "${TARGET_GPU:-}" ]]; then
	TARGET_GPU=$(((_gpus > 1) ? 1 : 0))
fi

# shellcheck disable=SC2206  # intentional word split
_extra=(${EXTRA_ARGS})

# Storage backends address a file, not a peer's memory.  nixlbench needs
# --filepath for them and fails with a bare "No such file or directory" if the
# directory does not exist, so check it here where the message can be useful.
_STORAGE_BACKENDS=" POSIX GDS GDS_MT HF3FS OBJ GUSLI AZURE_BLOB INFINIA "
if [[ "${_STORAGE_BACKENDS}" == *" ${BACKEND} "* ]]; then
	if [[ -z "${FILEPATH:-}" ]]; then
		echo "ERROR: ${BACKEND} is a storage backend -- set FILEPATH to a writable directory" >&2
		echo "       e.g. FILEPATH=/mnt/nixl-nvme-0/bench (an NVMe mount on the node)" >&2
		exit 2
	fi
	if ! mkdir -p "${FILEPATH}" 2> /dev/null || [[ ! -w "${FILEPATH}" ]]; then
		echo "ERROR: FILEPATH=${FILEPATH} is not writable from inside the container" >&2
		exit 2
	fi
	_extra+=(--filepath "${FILEPATH}")
	# O_DIRECT by default: without it the first run warms the page cache and
	# every later one measures RAM, which is not what an NVMe test is for.
	_extra+=(--storage_enable_direct "$([[ "${DIRECT_IO:-1}" == "1" ]] && echo true || echo false)")
	[[ "${BACKEND}" == "POSIX" ]] && _extra+=(--posix_api_type "${POSIX_API:-AIO}")
	echo "storage backend: filepath=${FILEPATH} direct_io=${DIRECT_IO:-1}" \
		"${POSIX_API:+api=${POSIX_API}}"
fi

# The pair rendezvouses by "first one to bind wins, the other connects".  If
# something ELSE already holds the port -- easy with --network=host on a shared
# login node, where 12345 is a popular choice -- both ranks fail to bind, both
# fall back to connect(), both decide they are the target, and the run dies on
# "ASIO Receive timeout" with no hint as to why.  So pick a port we can actually
# bind before starting anything.  Only meaningful for ROLE=both; a cross-node
# pair must agree on the port out of band.
if [[ "${ROLE}" == "both" ]]; then
	_free_port="$(python3 - "${ASIO_PORT}" <<'PY'
import socket, sys
start = int(sys.argv[1])
for port in range(start, start + 200):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        s.bind(("127.0.0.1", port))
    except OSError:
        continue
    finally:
        s.close()
    print(port)
    break
PY
	)"
	if [[ -z "${_free_port}" ]]; then
		echo "ERROR: no free port in [${ASIO_PORT}, ${ASIO_PORT}+200) to rendezvous on" >&2
		exit 1
	fi
	if [[ "${_free_port}" != "${ASIO_PORT}" ]]; then
		echo "note: port ${ASIO_PORT} is in use; rendezvousing on ${_free_port} instead"
		ASIO_PORT="${_free_port}"
	fi
fi

common_args=(
	--runtime_type ASIO
	--asio_address "${ASIO_ADDR}"
	--asio_port "${ASIO_PORT}"
	--backend "${BACKEND}"
	--initiator_seg_type "${SEG_TYPE}"
	--target_seg_type "${SEG_TYPE}"
)

echo "=== nixlbench: backend=${BACKEND} seg=${SEG_TYPE} role=${ROLE} ==="
echo "    rendezvous ${ASIO_ADDR}:${ASIO_PORT}   gpus visible: ${_gpus}"
echo "    initiator gpu=${INITIATOR_GPU}  target gpu=${TARGET_GPU}"
[[ "${SEG_TYPE}" == "VRAM" && "${_gpus}" -lt 2 && "${ROLE}" == "both" ]] && \
	echo "    NOTE: only ${_gpus} GPU visible, so both ranks share it -- a" \
	     "loopback measurement, not a real device-to-device number."
echo

run_one() {
	local role="$1" gpu="$2"
	# The target must be listening before the initiator connects, so `both`
	# starts the target first and gives it a moment.
	HIP_VISIBLE_DEVICES="${gpu}" \
		timeout "${TIMEOUT}" nixlbench "${common_args[@]}" "${_extra[@]}" 2>&1 |
		sed "s/^/[${role}] /"
	return "${PIPESTATUS[0]}"
}

case "${ROLE}" in
	target)
		run_one target "${TARGET_GPU}"
		exit $?
		;;
	initiator)
		run_one initiator "${INITIATOR_GPU}"
		exit $?
		;;
	both) ;;
	*)
		echo "ERROR: ROLE must be initiator, target or both (got '${ROLE}')" >&2
		exit 2
		;;
esac

run_one target "${TARGET_GPU}" &
target_pid=$!
sleep 2
run_one initiator "${INITIATOR_GPU}"
init_rc=$?
wait "${target_pid}"
target_rc=$?

echo
echo "=== exit: initiator=${init_rc} target=${target_rc} ==="
[[ "${init_rc}" -eq 0 && "${target_rc}" -eq 0 ]] || exit 1
