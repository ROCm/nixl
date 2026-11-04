#!/bin/bash
# Run one nixlbench configuration N times and report the median.
#
# WHY THIS EXISTS
#
# This host is shared. It has 32 threads and several hundred logged-in
# users, and its load average swings between about 50 and 180 on other
# people's work. A nixlbench figure taken here is a function of the cluster
# AND of whoever else is on the machine, and the second term is larger than
# most of the differences anyone wants to measure:
#
#   * eight back-to-back runs of one configuration spread 1.03x to 1.36x
#   * the SAME configuration, sampled twice twenty minutes apart, read
#     1.591 and 1.796 one time and medianed 2.103 the next
#
# That 1.5x is nothing but neighbours. A two-run sample on this machine
# reported a 15-33% fleet-wide regression across three transports that did
# not exist, and cost an afternoon chasing it through image pins.
#
# So: run it eight times in one burst, take the median, and print the load
# either side so the number can be discounted if the machine was busy. Put
# that in a script rather than in a paragraph, because the paragraph was
# already there and did not stop it happening.
#
# Usage:
#   cluster/scripts/bench.sh UCX VRAM
#   RUNS=16 BLOCK=262144 cluster/scripts/bench.sh LIBFABRIC DRAM
#   cluster/scripts/bench.sh MORI_IO VRAM
#   OP=READ BLOCK=131072 cluster/scripts/bench.sh NVME_KV DRAM
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/../env/nixl-mori}"

envget() {
	local v
	v="$(sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1)"
	printf '%s' "${v:-$2}"
}

BACKEND="${1:-UCX}"
SEG="${2:-DRAM}"
RUNS="${RUNS:-8}"
BLOCK="${BLOCK:-1048576}"
BUFFER="${BUFFER:-16777216}"
# vm1 and vm2 rail 1 by default: the pair the tables in cluster/README.md
# are measured on, so results stay comparable with them.
T_VM="${T_VM:-1}"
I_VM="${I_VM:-2}"
PORT_BASE="${PORT_BASE:-9900}"

# NVME_KV is not a transport and does not take a pair of guests: one VM and
# one in-process target are the whole fabric, so it runs a single process
# against a namespace character device. OP is WRITE (Store) or READ
# (Retrieve); a READ needs block == value size, because a KV value is atomic
# and there is no ranged Retrieve.
KV_VM="${KV_VM:-4}"
KV_DEV="${KV_DEV:-/dev/ng2n1}"
OP="${OP:-WRITE}"

SUBNET="$(envget ERNIC_GUEST_SUBNET 192.168.80)"
ERNIC_PER_VM="$(envget ERNIC_PER_VM 1)"
SSH_BASE="$(envget VM_SSH_PORT_BASE 2400)"
STACK="${STACK:-/opt/nixl-stack/opt}"

rail_ip() { printf '%s.%d' "${SUBNET}" $(( 10 + ($1 - 1) * ERNIC_PER_VM + 1 )); }
ssh_port() { echo $(( SSH_BASE + $1 - 1 )); }

ssh_key() {
	local images backing stamp
	images="$(envget VM_IMAGES_DIR /data/qemu-tool-stebates/images)"
	backing="$(envget VM_BACKING_IMAGE '')"
	stamp="${backing##*:}"; stamp="${stamp%%.*}"
	printf '%s' "${SSH_KEY:-$(dirname "${images}")/artifacts/${stamp}/id_rsa}"
}

KEY="$(ssh_key)"
SSH_OPTS=(-i "${KEY}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=10)

T_IP="$(rail_ip "${T_VM}")"
I_IP="$(rail_ip "${I_VM}")"
T_PORT="$(ssh_port "${T_VM}")"
I_PORT="$(ssh_port "${I_VM}")"

# The two flags that are not optional and are not defaults.  LIBFABRIC
# deadlocks without a progress thread -- see ansible/guest-setup.yml for the
# mechanism -- and MORI_IO copies whatever address it auto-detects into the
# descriptor its peer dials back, which here is QEMU's NAT address and the
# same on every guest.
extra_for() {
	case "${BACKEND}" in
	LIBFABRIC) echo "--enable_pt --progress_threads 1" ;;
	MORI_IO)   echo "--mori_host $1" ;;
	*)         echo "" ;;
	esac
}

GUEST_ENV="NIXL_PLUGIN_DIR=${STACK}/nixl/lib/x86_64-linux-gnu/plugins"
GUEST_ENV="${GUEST_ENV} FI_UNIVERSE_SIZE=16 UCX_ROCM_COPY_DMABUF=try"

if [[ "${BACKEND}" == "NVME_KV" ]]; then
	echo "${BACKEND} ${OP} ${SEG}, ${BLOCK} B values, vm${KV_VM} ${KV_DEV}"
else
	echo "${BACKEND} ${SEG}, ${BLOCK} B blocks, ${BUFFER} B buffer, vm${T_VM} <-> vm${I_VM}"
fi
echo "load before: $(cut -d' ' -f1-3 /proc/loadavg)"

vals=()
for i in $(seq 1 "${RUNS}"); do
	port=$(( PORT_BASE + i ))
	# One line, deliberately. A folded string keeps its newlines, and over
	# ssh every continuation line becomes its own command -- "--asio_port:
	# command not found", with nixlbench having silently run on its
	# defaults (an 8 GiB registration) rather than on these arguments.
	common="--backend ${BACKEND} --runtime_type ASIO --asio_address ${T_IP} --asio_port ${port} --initiator_seg_type ${SEG} --target_seg_type ${SEG} --start_block_size ${BLOCK} --max_block_size ${BLOCK} --total_buffer_size ${BUFFER}"

	if [[ "${BACKEND}" == "NVME_KV" ]]; then
		# Root-only: passthrough needs it, and sudo drops the
		# environment, so the plugin directory goes through explicitly
		# rather than through sudo -E.
		kv="--backend NVME_KV --nvme_kv_device=${KV_DEV} --op_type ${OP} --initiator_seg_type ${SEG} --start_block_size ${BLOCK} --max_block_size ${BLOCK} --total_buffer_size ${BLOCK} --num_iter 64 --warmup_iter 16"
		timeout 240 ssh "${SSH_OPTS[@]}" -p "$(ssh_port "${KV_VM}")" \
			ubuntu@localhost \
			"sudo env ${GUEST_ENV} ${STACK}/nixlbench/bin/nixlbench ${kv}" \
			>/tmp/bench-t.log 2>&1 || true
		: >/tmp/bench-i.log
	else
		timeout 240 ssh "${SSH_OPTS[@]}" -p "${T_PORT}" ubuntu@localhost \
			"env ${GUEST_ENV} ${STACK}/nixlbench/bin/nixlbench ${common} $(extra_for "${T_IP}")" \
			>/tmp/bench-t.log 2>&1 &
		sleep 4
		timeout 240 ssh "${SSH_OPTS[@]}" -p "${I_PORT}" ubuntu@localhost \
			"env ${GUEST_ENV} ${STACK}/nixlbench/bin/nixlbench ${common} $(extra_for "${I_IP}")" \
			>/tmp/bench-i.log 2>&1 || true
		wait 2>/dev/null || true
	fi

	# Whichever side bound first prints the table; the other prints none.
	# `|| true` is load-bearing: under `set -e` with `pipefail` a grep that
	# matches nothing fails the whole pipeline and kills the script, which
	# turns one failed run into no output at all.
	v="$(grep -hE "^${BLOCK} " /tmp/bench-t.log /tmp/bench-i.log 2>/dev/null |
	     head -1 | awk '{print $3}' || true)"
	if [[ -n "${v}" ]]; then
		vals+=("${v}")
		printf '  run %-2d %s GB/s\n' "${i}" "${v}"
	else
		printf '  run %-2d FAILED\n' "${i}"
	fi
done

echo "load after:  $(cut -d' ' -f1-3 /proc/loadavg)"

if [[ ${#vals[@]} -eq 0 ]]; then
	echo "every run failed; see /tmp/bench-t.log and /tmp/bench-i.log" >&2
	exit 1
fi

printf '%s\n' "${vals[@]}" | sort -n | awk -v n="${#vals[@]}" -v total="${RUNS}" '
	{a[NR]=$1}
	END {
		printf "\n%d/%d runs completed\n", n, total
		printf "  min %.3f  median %.3f  max %.3f  spread %.2fx\n",
		       a[1], a[int((n+1)/2)], a[n], a[n]/a[1]
		if (a[n]/a[1] > 1.25)
			printf "  NOTE: spread above 1.25x -- the host was probably busy; re-run\n"
	}'
