#!/usr/bin/env bash
# Operate on the qemu-tool test cluster: status, local NVMe, NIXL stack.
#
# The cluster's guests are plain VMs with no Docker, so the NIXL stack is
# installed as a tarball of /opt taken from the slim runtime image. See
# cluster/README.md.
#
# Copyright (c) Advanced Micro Devices, Inc.
# SPDX-License-Identifier: MIT

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
ENV_FILE="${REPO_ROOT}/cluster/env/nixl-mori"

# Read a key out of the env overlay, falling back to a default. Only the keys
# this script needs; qemu-tool owns the rest.
#
# A value already in the environment wins over the file, so these commands can
# be pointed at a stack that was rendered from a different overlay -- a second
# cluster, or an ad-hoc one -- without editing the file they would otherwise
# read.
envget() {
	local key="$1" def="${2-}" val
	val="${!key-}"
	if [[ -n "${val}" ]]; then
		printf '%s' "${val}"
		return
	fi
	val="$(sed -n "s/^${key}=//p" "${ENV_FILE}" | tail -1)"
	printf '%s' "${val:-$def}"
}

VM_COUNT="$(envget VM_COUNT 2)"
SSH_BASE="$(envget VM_SSH_PORT_BASE 2400)"
SUBNET="$(envget ERNIC_GUEST_SUBNET 192.168.220)"
IMAGE_NAME="${IMAGE_NAME:-nixl-mori-test}"
SLIM_TAG="${SLIM_TAG:-${IMAGE_NAME}:latest-slim}"

# The guests are reached through the host that runs the stack. Local by
# default; set CLUSTER_HOST to drive a remote one.
CLUSTER_HOST="${CLUSTER_HOST:-}"
# The key baked into VM_BACKING_IMAGE, not a key of ours: gen-vm does no
# cloud-init pass over a published backing image, so the guests trust only
# what the image shipped with. It comes out of the conventional (non -qcow2)
# tag of the same artifact, under /output, which the oras pull of the -qcow2
# tag does not carry. Move this whenever VM_BACKING_IMAGE moves.
SSH_KEY="${SSH_KEY:-/data/qemu-tool-stebates/artifacts/20261030/id_rsa}"
SSH_OPTS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
          -o LogLevel=ERROR -o ConnectTimeout=8 -o BatchMode=yes)

# Run a command on the host that owns the stack.
on_host() {
	if [[ -n "${CLUSTER_HOST}" ]]; then
		ssh -o BatchMode=yes -o ConnectTimeout=15 "${CLUSTER_HOST}" "$@"
	else
		bash -c "$*"
	fi
}

# Run a command inside guest $1 (1-based).
in_guest() {
	local n="$1"; shift
	local port=$(( SSH_BASE + n - 1 ))
	if [[ -n "${CLUSTER_HOST}" ]]; then
		ssh -o BatchMode=yes -o ConnectTimeout=15 "${CLUSTER_HOST}" \
			"ssh -i ${SSH_KEY} ${SSH_OPTS[*]} -p ${port} ubuntu@localhost '$*'"
	else
		ssh -i "${SSH_KEY}" "${SSH_OPTS[@]}" -p "${port}" ubuntu@localhost "$@"
	fi
}

copy_to_guest() {
	local n="$1" src="$2" dst="$3"
	local port=$(( SSH_BASE + n - 1 ))
	if [[ -n "${CLUSTER_HOST}" ]]; then
		scp -q -o BatchMode=yes "${src}" "${CLUSTER_HOST}:/tmp/$(basename "${src}")"
		ssh -o BatchMode=yes "${CLUSTER_HOST}" \
			"scp -q -i ${SSH_KEY} ${SSH_OPTS[*]} -P ${port} /tmp/$(basename "${src}") ubuntu@localhost:${dst}"
	else
		scp -q -i "${SSH_KEY}" "${SSH_OPTS[@]}" -P "${port}" "${src}" "ubuntu@localhost:${dst}"
	fi
}

cmd_status() {
	echo "=== containers ==="
	on_host "docker ps -a --filter name=nixl-mori --format '{{.Names}}\t{{.Status}}' | sort" || true
	echo
	echo "=== guests ==="
	for n in $(seq 1 "${VM_COUNT}"); do
		local port=$(( SSH_BASE + n - 1 ))
		printf 'vm%-3s ssh:%-6s ' "${n}" "${port}"
		if in_guest "${n}" true >/dev/null 2>&1; then
			printf 'up   rdma:%-18s ip:%-18s nvme:%s\n' \
				"$(in_guest "${n}" 'ls -A /sys/class/infiniband/ 2>/dev/null | tr "\n" " "' 2>/dev/null || echo none)" \
				"$(in_guest "${n}" 'ip -br addr show 2>/dev/null | grep -o "'"${SUBNET}"'\.[0-9]*/[0-9]*" | head -1' 2>/dev/null || echo none)" \
				"$(in_guest "${n}" 'findmnt -n -o SIZE /mnt/nvme 2>/dev/null || echo unmounted' 2>/dev/null)"
		else
			printf 'unreachable\n'
		fi
	done
}

cmd_nvme() {
	for n in $(seq 1 "${VM_COUNT}"); do
		printf 'vm%s: ' "${n}"
		in_guest "${n}" '
			set -e
			if findmnt -n /mnt/nvme >/dev/null 2>&1; then
				echo "already mounted"; exit 0
			fi
			test -b /dev/nvme0n1 || { echo "no /dev/nvme0n1 (VM_NVME unset?)"; exit 1; }
			sudo -n wipefs -a /dev/nvme0n1 >/dev/null 2>&1 || true
			sudo -n mkfs.ext4 -q -F -L nvme-scratch /dev/nvme0n1
			sudo -n mkdir -p /mnt/nvme
			sudo -n mount /dev/nvme0n1 /mnt/nvme
			sudo -n chown "$(id -u):$(id -g)" /mnt/nvme
			findmnt -n -o SIZE,FSTYPE /mnt/nvme
		' || echo "  FAILED"
	done
}

# Everything a guest needs before an RDMA benchmark will run, made to survive
# a reboot. All three of these were learned by losing a cluster to them.
#
#   memlock   nixlbench registers its whole buffer, so ibv_reg_mr asks for far
#             more locked memory than the 8 MB default. UCX reports it
#             accurately -- "Please set max locked memory (ulimit -l)" -- and
#             then NIXL buries it under "registration failed for the specified
#             or all potential backends", which reads like a backend fault.
#
#   address   the fabric address is not in the guest image and nothing in the
#             compose stack assigns it; it was being set by hand, which lasts
#             exactly until the guest reboots. Written as netplan config so it
#             comes back on its own. `optional: true` matters: without it
#             systemd-networkd-wait-online blocks the boot for as long as the
#             link is down, which is forever if the link cannot come up, and
#             the guest never reaches sshd.
#
#   queues    only as a fallback, and only when the link is down. ionic sizes
#             its interrupt pool from the vCPU count and asks for one vector
#             per queue pair plus an admin vector; on the in-tree driver in the
#             `ionic`-flavour guest four combined queues did not reliably fit,
#             and the link stayed down with `ip link set up` answering "No
#             space left on device" after `qcq alloc of rx0 -28`. The
#             ernic-rocjitsu guest carries ionic-ernic as a DKMS module and
#             brings all four guests up at four queues, so dropping to one is
#             now a throughput cut for no reason -- hence the guard. The
#             unbind/rebind with it is not ceremony: once ionic has failed to
#             allocate its queues it does not release the vectors it did take,
#             and ethtool alone still cannot bring the link up.
cmd_prep() {
	for n in $(seq 1 "${VM_COUNT}"); do
		printf 'vm%s: ' "${n}"
		in_guest "${n}" "
			set -e
			printf '* soft memlock unlimited\n* hard memlock unlimited\n' \
				| sudo -n tee /etc/security/limits.d/90-rdma-memlock.conf >/dev/null
			printf 'network:\n  version: 2\n  ethernets:\n    enp1s0np0:\n      addresses: [${SUBNET}.1${n}/20]\n      optional: true\n' \
				| sudo -n tee /etc/netplan/60-ernic.yaml >/dev/null
			sudo -n chmod 600 /etc/netplan/60-ernic.yaml

			if ! ip link show enp1s0np0 | grep -q 'state UP'; then
				sudo -n sh -c 'echo 0000:01:00.0 > /sys/bus/pci/drivers/ionic/unbind' || true
				sleep 4
				sudo -n sh -c 'echo 0000:01:00.0 > /sys/bus/pci/drivers/ionic/bind' || true
				sleep 6
				sudo -n ethtool -L enp1s0np0 combined 1 >/dev/null 2>&1 || true
				sudo -n ip link set enp1s0np0 up >/dev/null 2>&1 || true
			fi
			sudo -n netplan apply
			sleep 3
			printf '%s  memlock=%s  %s\n' \
				\"\$(ip -br addr show enp1s0np0 | awk '{print \$2, \$3}')\" \
				\"\$(ulimit -l)\" \
				\"\$(ibv_devinfo 2>/dev/null | sed -n 's/.*state:[[:space:]]*//p' | head -1)\"
		" || echo "  FAILED"
	done
}

# Bring the emulated GPU up on every guest. Needed once per boot, and needed
# at all because MORI_IO cannot start without one: mori::io builds its topology
# through ROCm-SMI before any transport, and calls exit(-1) on zero GPUs, so
# even a DRAM-segment run aborts at RSMI_STATUS_INIT_ERROR.
#
# Two halves, and the guest image deliberately ships neither:
#
#   ip_discovery.bin  the IP table amdgpu reads under `discovery=2`. It is
#                     per-rocjitsu-build, so a published guest must not carry
#                     one -- a baked copy would be wrong for every consumer
#                     serving the device from a different build. Generated here
#                     from the rocjitsu image this cluster actually runs.
#                     Written to /lib/firmware/amdgpu, NOT
#                     /lib/firmware/updates/amdgpu: the kernel searches updates/
#                     first, and that is where amdgpu-dkms-firmware 31.60 puts
#                     the real gc_12_1_0 and sdma_7_1_0 blobs. Only
#                     ip_discovery.bin is missing from the set, so this adds
#                     exactly that and shadows nothing.
#
#   amdgpu-probe      the guest blacklists amdgpu and loads it by hand with the
#                     gfx1250 emulation parameters. A bare `modprobe amdgpu`
#                     gets emu_mode=0 and finds no GPU.
#
# /dev/kfd is not the test -- it appears as soon as the module loads, GPU or
# not. A KFD topology node with a non-empty name is, which is what this prints.
cmd_gpu() {
	local rj fw
	rj="$(envget ROCJITSU_IMAGE)"
	[[ -n "${rj}" ]] || { echo "ERROR: ROCJITSU_IMAGE is not set in ${ENV_FILE}" >&2; exit 1; }

	fw="$(mktemp -d)"
	trap 'rm -rf "${fw}"' RETURN
	echo "generating ip_discovery.bin from ${rj##*/}"
	docker run --rm --entrypoint /bin/sh "${rj}" -c \
		'python3 /usr/local/bin/vfio_guest_firmware.py --generation gfx1250 \
		   --set full --output /out >/dev/null 2>&1 && tar -cf - -C /out .' \
		| tar -xf - -C "${fw}"
	[[ -s "${fw}/ip_discovery.bin" ]] || { echo "ERROR: no ip_discovery.bin generated" >&2; exit 1; }

	for n in $(seq 1 "${VM_COUNT}"); do
		printf 'vm%s: ' "${n}"
		copy_to_guest "${n}" "${fw}/ip_discovery.bin" /tmp/ip_discovery.bin
		in_guest "${n}" '
			sudo -n install -m 0644 /tmp/ip_discovery.bin \
				/lib/firmware/amdgpu/ip_discovery.bin
			# modprobe is a no-op on a resident module, so a driver already
			# loaded against a missing blob has to be re-bound at the PCI
			# layer rather than reloaded. amdgpu-probe covers the cold case.
			if [ -d /sys/bus/pci/drivers/amdgpu ]; then
				sudo -n sh -c "echo 0000:02:00.0 > /sys/bus/pci/drivers/amdgpu/bind" 2>/dev/null || true
			else
				sudo -n /usr/local/bin/amdgpu-probe >/dev/null 2>&1 || true
			fi
			sleep 14
			name="$(cat /sys/class/kfd/kfd/topology/nodes/*/name 2>/dev/null | grep -v "^$" | head -1)"
			if [ -n "${name}" ]; then
				printf "kfd node %s, emu_mode=%s\n" "${name}" \
					"$(cat /sys/module/amdgpu/parameters/emu_mode 2>/dev/null)"
			else
				echo "NO KFD NODE -- see dmesg | grep amdgpu"
				exit 1
			fi
		' || echo "  FAILED"
	done
}

cmd_stack() {
	command -v docker >/dev/null || { echo "docker is needed to export the slim image" >&2; exit 1; }
	docker image inspect "${SLIM_TAG}" >/dev/null 2>&1 || {
		echo "ERROR: ${SLIM_TAG} not found. Build it first:" >&2
		echo "       docker buildx bake slim" >&2
		exit 1
	}

	# Deliberately not `local`: the EXIT trap below runs after this function
	# has returned, so a local would be out of scope by then and `set -u`
	# would abort the trap, leaving the temp tree behind.
	tmp=
	tmp="$(mktemp -d)"
	trap 'rm -rf "${tmp}"' EXIT

	echo "exporting /opt from ${SLIM_TAG}"
	local cid
	cid="$(docker create "${SLIM_TAG}")"
	docker export "${cid}" | tar -x -C "${tmp}" opt
	docker rm -f "${cid}" >/dev/null
	tar -czf "${tmp}/nixl-stack.tgz" -C "${tmp}" opt
	echo "  $(du -h "${tmp}/nixl-stack.tgz" | cut -f1)"

	# These two come from apt rather than /opt, so the tarball cannot carry
	# them and the plugins will not resolve without them.
	cat > "${tmp}/install.sh" <<'INSTALL'
set -e
sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
	libgflags2.2 libhwloc15 >/dev/null 2>&1 || true
sudo -n rm -rf /opt/nixl-stack
sudo -n mkdir -p /opt/nixl-stack
sudo -n tar -xzf /tmp/nixl-stack.tgz -C /opt/nixl-stack
S=/opt/nixl-stack/opt
# The image sets this with ENV, which a tarball of /opt cannot carry, so it
# has to be re-established here. Without it libfabric takes its default
# universe of 256, ofi_rxm sizes the MSG completion queue from that, the
# device cannot accommodate the result, and every LIBFABRIC run dies at
# `fi_enable failed for rail 0: Invalid argument` -- before any transfer, and
# with nothing in the message pointing at a universe size.
printf 'FI_UNIVERSE_SIZE=16\n' | sudo -n tee /etc/environment.d/90-nixl-stack.conf >/dev/null 2>&1 || true
printf 'export FI_UNIVERSE_SIZE=16\n' | sudo -n tee /etc/profile.d/nixl-stack.sh >/dev/null
printf '%s\n' $S/rocm/lib $S/rocm/lib/rocm_sysdeps/lib $S/rocnixl-ucx/lib \
	$S/libfabric/lib $S/mori/lib $S/hipfile/lib $S/nixl/lib/x86_64-linux-gnu \
	| sudo -n tee /etc/ld.so.conf.d/nixl-stack.conf >/dev/null
sudo -n ldconfig
miss=0
for so in $S/nixl/lib/x86_64-linux-gnu/plugins/libplugin_*.so; do
	[ -e "$so" ] || continue
	if ldd "$so" 2>/dev/null | grep -q "not found"; then
		echo "  UNRESOLVED $(basename "$so"):"
		ldd "$so" | grep "not found" | sed 's/^/    /'
		miss=1
	fi
done
[ "$miss" -eq 0 ] && echo "  all plugins resolve"
INSTALL

	for n in $(seq 1 "${VM_COUNT}"); do
		printf 'vm%s: ' "${n}"
		copy_to_guest "${n}" "${tmp}/nixl-stack.tgz" /tmp/nixl-stack.tgz
		copy_to_guest "${n}" "${tmp}/install.sh" /tmp/install.sh
		in_guest "${n}" 'bash /tmp/install.sh' || echo "  FAILED"
	done

	cat <<EOF

Installed under /opt/nixl-stack/opt. To run nixlbench there:

  export NIXL_PLUGIN_DIR=/opt/nixl-stack/opt/nixl/lib/x86_64-linux-gnu/plugins
  export FI_UNIVERSE_SIZE=16   # only needed if your shell predates the install
  /opt/nixl-stack/opt/nixlbench/bin/nixlbench --backend UCX \\
      --runtime_type ASIO --asio_address ${SUBNET}.11 --asio_port 9500 \\
      --initiator_seg_type DRAM --target_seg_type DRAM \\
      --total_buffer_size 1073741824

Run the same command on both guests; whichever binds first is the target, and
it is the one that prints the results table.
EOF
}

case "${1-}" in
	status) cmd_status ;;
	prep)   cmd_prep ;;
	gpu)    cmd_gpu ;;
	nvme)   cmd_nvme ;;
	stack)  cmd_stack ;;
	*)      echo "usage: $0 {status|prep|gpu|nvme|stack}" >&2; exit 2 ;;
esac
