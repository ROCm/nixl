#!/bin/bash
# Turn one rail of one VM into an S3-over-RDMA object store, and back again.
#
# WHY A SCRIPT RATHER THAN A COMPOSE CHANGE
#
# rocm-ernic's `--backend s3` runs the object store, the HTTP endpoint and the
# RDMA data mover inside the server process, so one guest and one rail are a
# complete object fabric -- no second VM, no second server, no storage backend.
# That is what makes it the right target for NIXL's OBJ plugin here.
#
# But gen-compose hardcodes every ernic's backend to the TCP mesh
# (`tcp:manager:listen:` for node 1, `tcp:worker:` for the rest), with no hook
# for anything else, so the rendered compose cannot express it. Rather than
# fork gen-compose, this replaces the one container, keeping the socket path,
# MAC, fleet address, hostname and uid the rendered one used -- qemu dials the
# socket by path and the guest knows the device by MAC, so nothing else cares.
#
# The displaced container is renamed aside rather than deleted, so `restore`
# puts the rail back in the mesh without a `cluster-boot`.
#
# WHAT YOU HAVE TO KNOW
#
#   * Restarting an ernic under a running guest wedges the ionic driver
#     ("FW stopped 0xff"), so the whole VM is restarted, in order: device
#     servers first with fresh sockets, then qemu. The rocjitsu servers have
#     to be restarted too, because a vfio-user server serves one client and
#     refuses the next with "Address already in use" on a stale socket.
#
#   * The guest needs a host route. Every rail sits in the same
#     ERNIC_GUEST_SUBNET /20, so the endpoint address is on-link for ALL of
#     them and the kernel picks the first -- the request leaves on rail 1 and
#     gets "No route to host" from a wire with no S3 server on it. The route
#     pins it to the rail that serves it.
#
#   * The GPUs have to be re-probed after the restart (make cluster-gpu).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENV_FILE="${ENV_FILE:-${HERE}/../env/nixl-mori}"

envget() {
	local v
	v="$(sed -n "s/^$1=//p" "${ENV_FILE}" | tail -1)"
	printf '%s' "${v:-$2}"
}

PROJECT="${PROJECT:-nixl-mori}"
VM="${VM:-4}"
RAIL="${RAIL:-2}"
SUBNET="$(envget ERNIC_GUEST_SUBNET 192.168.80)"
# Overridable from the environment so a locally built server can be put on
# one rail without editing the pins the rest of the fleet runs.
ERNIC_IMAGE="${ERNIC_IMAGE:-$(envget ERNIC_IMAGE '')}"
ERNIC_PER_VM="$(envget ERNIC_PER_VM 1)"
SSH_BASE="$(envget VM_SSH_PORT_BASE 2400)"

S3_IP="${S3_IP:-${SUBNET}.200}"
S3_PORT="${S3_PORT:-9000}"
S3_BUCKET="${S3_BUCKET:-ernic}"
S3_SIZE="${S3_SIZE:-1G}"

# NVMe Key Value. `--backend nvmeof:kv=1` turns the NVMe-oF target's namespace
# into a KV one, and like s3 it runs inside the server process -- so one guest
# and one rail are a complete KV fabric. maxval is what the plugin can Store in
# one command; MDTS caps the transfer below it anyway (5, so 128 KiB here).
KV_IP="${KV_IP:-${SUBNET}.201}"
KV_PORT="${KV_PORT:-4420}"
KV_SIZE="${KV_SIZE:-256M}"
KV_KEYS="${KV_KEYS:-4096}"
KV_MAXVAL="${KV_MAXVAL:-1M}"
KV_NQN="${KV_NQN:-nvmet-test}"

# The block namespace of a mixed controller.
BLK_SIZE="${BLK_SIZE:-1G}"
BLK_BS="${BLK_BS:-4096}"

# Everything below is backend-agnostic: the rail swap, the restart ordering,
# the socket handling and the host route are the same whichever one is served.
MODE="${MODE:-s3}"
case "${MODE}" in
	s3)
		BACKEND="s3:ip=${S3_IP},port=${S3_PORT},size=${S3_SIZE},bucket=${S3_BUCKET}"
		TARGET_IP="${S3_IP}"
		ENDPOINT="http://${S3_IP}:${S3_PORT}/${S3_BUCKET}/"
		;;
	kv)
		BACKEND="nvmeof:kv=1,size=${KV_SIZE},keys=${KV_KEYS},maxval=${KV_MAXVAL},ip=${KV_IP}"
		TARGET_IP="${KV_IP}"
		ENDPOINT="nvme://${KV_IP}:${KV_PORT}/${KV_NQN}"
		;;
	mixed)
		# One controller, two namespaces, two command sets: nsid 1 blocks
		# and nsid 2 Key Value.  The guest gets /dev/nvmeXn1 for the first
		# and /dev/ngXn2 for the second off a single `nvme connect`.
		BACKEND="nvmeof:size=${BLK_SIZE},bs=${BLK_BS},ip=${KV_IP}"
		BACKEND="${BACKEND},ns=2,kv=1,size=${KV_SIZE}"
		BACKEND="${BACKEND},keys=${KV_KEYS},maxval=${KV_MAXVAL}"
		TARGET_IP="${KV_IP}"
		ENDPOINT="nvme://${KV_IP}:${KV_PORT}/${KV_NQN} (nsid 1 block, nsid 2 kv)"
		;;
	*) echo "ERROR: MODE must be s3, kv or mixed, not '${MODE}'" >&2; exit 2 ;;
esac

MESH="${PROJECT}-ernic-${VM}-${RAIL}-1"
SVC="${PROJECT}-ernic-${VM}-${RAIL}-${MODE}"
SAVED="${PROJECT}-ernic-${VM}-${RAIL}-mesh-saved"

# Every mode's container name, so switching modes on a rail displaces the
# previous server instead of leaving it running: two servers on one socket
# look like a guest that will not come up, and the loser is silent about it.
ALL_SVC=()
for _m in s3 kv mixed; do
	ALL_SVC+=("${PROJECT}-ernic-${VM}-${RAIL}-${_m}")
done

drop_services() {
	local c
	for c in "${ALL_SVC[@]}"; do
		docker rm -f "${c}" >/dev/null 2>&1 || true
	done
}
QEMU="${PROJECT}-qemu-${VM}-1"
SOCK="/run/vfu/ernic-${VM}-${RAIL}.sock"
SSH_PORT=$(( SSH_BASE + VM - 1 ))

# The guest NIC that rail N presents, in the guest's own naming. Rails are
# enumerated from the global NIC ordinal, the same way ansible/inventory.py
# does it, so this has to agree with that.
guest_rail_ip() { printf '%s.%d' "${SUBNET}" $(( 10 + (VM - 1) * ERNIC_PER_VM + RAIL )); }

# Everything the rendered container carried that the replacement must keep.
# Read from the live container rather than recomputed, so a change in
# gen-compose's addressing does not silently produce a mismatched stand-in.
inspect() { docker inspect "$1" --format "$2"; }

do_start() {
	local src
	if docker inspect "${MESH}" >/dev/null 2>&1; then
		src="${MESH}"
	elif docker inspect "${SAVED}" >/dev/null 2>&1; then
		src="${SAVED}"
	else
		echo "ERROR: no ${MESH} to take over from; run 'make cluster-boot' first" >&2
		exit 1
	fi

	local user hostname fleet_ip mac gids
	user="$(inspect "${src}" '{{.Config.User}}')"
	hostname="$(inspect "${src}" '{{.Config.Hostname}}')"
	fleet_ip="$(inspect "${src}" '{{index .NetworkSettings.Networks "'"${PROJECT}"'_fleet" "IPAddress"}}')"
	# The MAC is on the rocm-ernic command line, not in the container config.
	mac="$(inspect "${src}" '{{json .Config.Entrypoint}}' | grep -o "'-m' '[^']*'" | cut -d"'" -f4)"
	gids="$(inspect "${src}" '{{range .Config.Env}}{{println .}}{{end}}' \
		| sed -n 's/^ERNIC_TCP_GUEST_GIDS=//p')"

	[[ -n "${mac}" ]] || { echo "ERROR: could not read the MAC from ${src}" >&2; exit 1; }

	echo "stopping the guest and the mesh ernic on vm${VM} rail ${RAIL}"
	docker stop "${QEMU}" >/dev/null 2>&1 || true
	docker stop "${src}" >/dev/null 2>&1 || true
	[[ "${src}" == "${SAVED}" ]] || docker rename "${src}" "${SAVED}"
	drop_services

	# The displaced server owns the socket inode; a new one cannot bind it.
	docker run --rm -v "${PROJECT}_vfu-sockets:/run/vfu" alpine rm -f "${SOCK}"

	echo "starting the ${MODE} server at ${ENDPOINT}"
	docker run -d --name "${SVC}" \
		--user "${user}" --hostname "${hostname}" \
		--network "${PROJECT}_fleet" --ip "${fleet_ip}" \
		-v "${PROJECT}_vfu-sockets:/run/vfu" \
		-e "ERNIC_TCP_GUEST_GIDS=${gids}" \
		--restart on-failure \
		"${ERNIC_IMAGE}" \
		rocm-ernic -s "${SOCK}" -b "${BACKEND}" \
			-m "${mac}" >/dev/null

	restart_guest
	echo
	echo "vm${VM} rail ${RAIL} now serves ${MODE} at ${ENDPOINT}"
	echo "next: make cluster-gpu ANSIBLE_ARGS='--limit vm${VM}'"
	[[ "${MODE}" == "kv" || "${MODE}" == "mixed" ]] && cat <<-EOF

		Then, in the guest -- the neighbour entry is the unobvious one. The
		controller is not a host on the segment and has no MAC, so address
		resolution has nothing to resolve to and the connect fails with
		"rdma connection establishment failed (-104)" and nothing at all in
		the target's log. Any link-layer address will do; the emulator routes
		by queue pair, not by MAC.

		  sudo ip neigh replace ${KV_IP} lladdr 02:00:00:00:c0:01 dev <rail> nud permanent
		  sudo nvme connect -t rdma -a ${KV_IP} -s ${KV_PORT} -n ${KV_NQN}

		Linux hides the block device for a CSI 1 namespace and keeps /dev/ngXnY,
		which is what the NVME_KV plugin opens.
	EOF
	return 0
}

do_restore() {
	docker inspect "${SAVED}" >/dev/null 2>&1 || {
		echo "ERROR: no ${SAVED} to restore" >&2; exit 1; }
	echo "restoring vm${VM} rail ${RAIL} to the TCP mesh"
	docker stop "${QEMU}" >/dev/null 2>&1 || true
	drop_services
	docker run --rm -v "${PROJECT}_vfu-sockets:/run/vfu" alpine rm -f "${SOCK}"
	docker rename "${SAVED}" "${MESH}"
	docker start "${MESH}" >/dev/null

	# The mesh server needs tap0, and tap0 lives in ITS network namespace,
	# created by a tapsetup container that runs once and exits.  Restarting
	# the ernic gives it a fresh namespace with no tap0 in it, so the
	# server spins on "tap0 never appeared", never binds the vfio-user
	# socket, and the guest never comes back -- which reads as a VM that
	# failed to boot rather than as a missing sidecar.  The s3, kv and
	# mixed servers do not take -T and so do not need this; only the mesh
	# does, which is why it bites on the way back and not on the way out.
	local tap="${PROJECT}-tapsetup-${VM}-${RAIL}-1"
	if docker inspect "${tap}" >/dev/null 2>&1; then
		echo "re-running ${tap} for tap0"
		docker start "${tap}" >/dev/null
		wait_for_tap
	fi

	restart_guest
}

# tapsetup exits as soon as it has made the interface, so "started" is not
# "ready".  Wait for the thing itself rather than sleeping.
wait_for_tap() {
	local i
	for i in $(seq 1 30); do
		if docker exec "${MESH}" test -e /sys/class/net/tap0 2>/dev/null; then
			return 0
		fi
		sleep 1
	done
	echo "WARNING: tap0 did not appear in ${MESH}; the guest will not boot" >&2
	return 1
}

restart_guest() {
	# A vfio-user server serves one client, and a stale socket makes the
	# restarted one fail with "Address already in use" -- so the sockets go
	# before the servers, and the servers before qemu.
	local rj
	for rj in $(seq 1 "$(envget ROCJITSU_PER_VM 0)"); do
		docker run --rm -v "${PROJECT}_vfu-sockets:/run/vfu" alpine \
			rm -f "/run/vfu/rocjitsu-${VM}-${rj}.sock"
	done
	for rj in $(seq 1 "$(envget ROCJITSU_PER_VM 0)"); do
		docker restart "${PROJECT}-rocjitsu-${VM}-${rj}-1" >/dev/null
	done
	sleep 8
	docker start "${QEMU}" >/dev/null
	echo -n "waiting for vm${VM}"
	local i
	for i in $(seq 1 60); do
		if ssh_guest true 2>/dev/null; then echo " up"; break; fi
		echo -n .
		sleep 6
	done
	add_route
}

# Without this the endpoint is on-link on every rail and the kernel answers
# with the first one. See the header.
add_route() {
	local dev
	dev="$(ssh_guest "ip -br -4 a | awk '\$3 ~ /^$(guest_rail_ip)\//{print \$1}'" || true)"
	[[ -n "${dev}" ]] || { echo "WARNING: no guest interface holds $(guest_rail_ip)" >&2; return; }
	ssh_guest "sudo ip route replace ${TARGET_IP}/32 dev ${dev} src $(guest_rail_ip)"
	echo "vm${VM}: ${TARGET_IP} pinned to ${dev}"
}

# The key comes out of the artifacts tree named for VM_BACKING_IMAGE's build
# stamp, derived the same way cluster.sh derives it -- a hardcoded path would
# keep resolving to the previous image's key after a pin bump, and the failure
# would read as "every guest refuses the key".
ssh_guest() {
	local images backing stamp key
	images="$(envget VM_IMAGES_DIR /data/qemu-tool-stebates/images)"
	backing="$(envget VM_BACKING_IMAGE '')"
	stamp="${backing##*:}"; stamp="${stamp%%.*}"
	key="${SSH_KEY:-$(dirname "${images}")/artifacts/${stamp}/id_rsa}"
	ssh -i "${key}" -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
		-o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=10 \
		-p "${SSH_PORT}" ubuntu@localhost "$@"
}

case "${1-}" in
	start)   do_start ;;
	restore) do_restore ;;
	route)   add_route ;;
	*) echo "usage: [MODE=s3|kv|mixed] VM=4 RAIL=2 $0 {start|restore|route}" >&2; exit 2 ;;
esac
