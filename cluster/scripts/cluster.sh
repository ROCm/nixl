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
SSH_KEY="${SSH_KEY:-/data/qemu-tool-stebates/artifacts/20261021/id_rsa}"
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
	nvme)   cmd_nvme ;;
	stack)  cmd_stack ;;
	*)      echo "usage: $0 {status|nvme|stack}" >&2; exit 2 ;;
esac
