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

VM_IMAGES_DIR="$(envget VM_IMAGES_DIR /data/qemu-tool-stebates/images)"
VM_PREFIX="$(envget VM_NAME_PREFIX nixl-mori)"
BACKING_IMAGE="$(envget VM_BACKING_IMAGE)"

# Everything fetched from VM_BACKING_IMAGE lands here, in a directory named for
# that image's build stamp. Derived rather than written down: the SSH key below
# comes out of this tree, and a hardcoded path would keep resolving -- to the
# previous image's key -- after a pin bump, so the failure would be "every
# guest refuses the key" rather than "that file is missing".
BACKING_TAG="${BACKING_IMAGE##*:}"
BACKING_STAMP="${BACKING_TAG%%.*}"
ARTIFACTS_DIR="${ARTIFACTS_DIR:-$(dirname "${VM_IMAGES_DIR}")/artifacts/${BACKING_STAMP}}"

# The guests are reached through the host that runs the stack. Local by
# default; set CLUSTER_HOST to drive a remote one.
CLUSTER_HOST="${CLUSTER_HOST:-}"
# The key baked into VM_BACKING_IMAGE, not a key of ours: gen-vm does no
# cloud-init pass over a published backing image, so the guests trust only what
# the image shipped with. `cluster.sh images` extracts it from the conventional
# (non -qcow2) tag of the same artifact, under /output, which the oras pull of
# the -qcow2 tag does not carry.
SSH_KEY="${SSH_KEY:-${ARTIFACTS_DIR}/id_rsa}"
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
# `prep` and `gpu` used to live here. They are now ansible/guest-setup.yml --
# one definition rather than two that drift -- and `make cluster-prep` /
# `make cluster-gpu` run it with the matching tag. What they do, and why each
# step is needed, is documented on the tasks there.
#
# What stays here is what ansible is the wrong tool for: `stack` exports /opt
# from a local docker image, and `status` and `nvme` are quick imperative
# checks that predate the playbook.

# Fetch VM_BACKING_IMAGE and build the guest disks from it. Everything this
# does was done by hand the first time, which is the whole reason it is here:
# the pins in cluster/env/nixl-mori describe the cluster exactly, and without
# this they still could not be turned back into a running one.
#
# Two artifacts from two tags of the same build, because they carry different
# things:
#
#   <tag>-qcow2  an OCI artifact (artifactType application/vnd.batesste.vm-image.v1),
#                one zstd layer, pulled with oras. This is the disk.
#   <tag>        a conventional image ~2 GiB larger carrying the same qcow2
#                plus /output/id_rsa and /output/vm-info.json. The key is only
#                here, and the guests trust no other -- a published backing
#                image gets no cloud-init pass, so nothing of ours is injected.
#
# Idempotent by .artifact-tag: a fetched tree records what it came from, and a
# re-run against the same pin does nothing. Bumping VM_BACKING_IMAGE therefore
# re-fetches, and FORCE=1 re-fetches regardless.
cmd_images() {
	[[ -n "${BACKING_IMAGE}" ]] || {
		echo "ERROR: VM_BACKING_IMAGE is not set in ${ENV_FILE}" >&2; exit 1; }
	for tool in oras zstd qemu-img docker; do
		command -v "${tool}" >/dev/null || {
			echo "ERROR: ${tool} is needed and not on PATH" >&2; exit 1; }
	done

	local plain_tag="${BACKING_IMAGE%-qcow2}"
	local backing="${VM_IMAGES_DIR}/${VM_PREFIX}-backing.qcow2"
	local stamp_file="${ARTIFACTS_DIR}/.artifact-tag"

	mkdir -p "${ARTIFACTS_DIR}" "${VM_IMAGES_DIR}"

	if [[ "${FORCE:-0}" != "1" ]] && [[ -f "${stamp_file}" ]] &&
	   [[ "$(cat "${stamp_file}")" == "${BACKING_TAG}" ]] &&
	   [[ -f "${ARTIFACTS_DIR}/id_rsa" ]]; then
		echo "artifacts already at ${BACKING_TAG} (FORCE=1 to refetch)"
	else
		echo "pulling ${BACKING_IMAGE}"
		( cd "${ARTIFACTS_DIR}" && oras pull "${BACKING_IMAGE}" ) >/dev/null

		# The key and vm-info come out of the conventional tag; `docker cp`
		# rather than `docker export | tar` so this does not stream ~3 GiB of
		# qcow2 through a pipe to reach two small files.
		echo "extracting the fleet key from ${plain_tag##*:}"
		local cid
		cid="$(docker create "${plain_tag}")"
		for f in id_rsa id_rsa.pub vm-info.json; do
			docker cp "${cid}:/output/${f}" "${ARTIFACTS_DIR}/${f}" >/dev/null 2>&1 || true
		done
		docker rm -f "${cid}" >/dev/null
		chmod 600 "${ARTIFACTS_DIR}/id_rsa" 2>/dev/null || true
		[[ -s "${ARTIFACTS_DIR}/id_rsa" ]] || {
			echo "ERROR: no /output/id_rsa in ${plain_tag}" >&2; exit 1; }

		printf '%s\n' "${BACKING_TAG}" > "${stamp_file}"
	fi

	if [[ "${FORCE:-0}" == "1" || ! -f "${backing}" ]]; then
		local zst
		zst="$(ls "${ARTIFACTS_DIR}"/*.qcow2.zst 2>/dev/null | head -1)"
		[[ -n "${zst}" ]] || { echo "ERROR: no qcow2.zst in ${ARTIFACTS_DIR}" >&2; exit 1; }
		echo "decompressing $(basename "${zst}") -> $(basename "${backing}")"
		# -o straight to the final name: no second full-size copy.
		zstd -d -f "${zst}" -o "${backing}"
	else
		echo "backing image present: ${backing}"
	fi

	# One overlay per guest. run-vm does NOT create these -- --backing-shared
	# only turns off file locking so the guests can share one backing file --
	# so a missing overlay is a guest that will not boot.
	for n in $(seq 1 "${VM_COUNT}"); do
		local overlay="${VM_IMAGES_DIR}/${VM_PREFIX}-${n}.qcow2"
		if [[ "${FORCE:-0}" == "1" || ! -f "${overlay}" ]]; then
			rm -f "${overlay}"
			qemu-img create -F qcow2 -b "${backing}" -f qcow2 "${overlay}" >/dev/null
			echo "  created $(basename "${overlay}")"
		else
			echo "  $(basename "${overlay}") exists"
		fi
	done

	# Scratch NVMe owned by root is left over from when the containers ran as
	# root. QEMU opens every drive read-write and refuses to start rather than
	# merely warning, so this is a boot failure with a confusing message.
	local stale
	stale="$(find "${VM_IMAGES_DIR}" -maxdepth 1 -name "${VM_PREFIX}-*-nvme*.qcow2" \
	         ! -user "$(id -u)" 2>/dev/null || true)"
	if [[ -n "${stale}" ]]; then
		echo "removing NVMe scratch images owned by another user (recreated on boot):"
		printf '  %s\n' ${stale}
		rm -f ${stale}
	fi

	echo
	echo "artifacts: ${ARTIFACTS_DIR}"
	echo "ssh key:   ${SSH_KEY}"
	grep -oE '"kernel_release": "[^"]+"|"flavour": "[^"]+"' \
		"${ARTIFACTS_DIR}/vm-info.json" 2>/dev/null | sed 's/^/  /' || true
}

# Start the stack in dependency order, ignoring compose's healthcheck gate.
#
# Needed because ERNIC_IMAGE is our own build (see docker/rocm-ernic/
# Dockerfile) and that server binds its mesh-manager port on tcp6 only -- a
# dual-stack socket, which Linux reports in /proc/net/tcp6 and not
# /proc/net/tcp. gen-compose's manager healthcheck greps /proc/net/tcp for a
# v4 LISTEN, so it never passes and `compose up` refuses to start anything
# that depends on it. The mesh is fine; v4 clients connect through the
# dual-stack socket. Only the check is wrong, and it is generated, so it
# cannot be fixed from our side.
#
# compose still CREATES every container before it gives up, so this only has
# to start them. The order is the one the traps in this file describe:
#
#   ernic     serves the vfio-user socket; waits for tap0 before it execs
#   tapsetup  joins ernic's netns, creates tap0, exits. Restarting ernic
#             gives it a fresh namespace with no tap0, so this follows it
#             every time, not just the first
#   rocjitsu  stops serving when its client disconnects and cannot rebind a
#             stale socket, so it starts after a clean socket directory
#   qemu      last, once every vfio-user socket it opens exists
cmd_boot() {
	local names
	for kind in ernic tapsetup rocjitsu qemu; do
		names="$(on_host "docker ps -a --filter name=nixl-mori-${kind}- --format '{{.Names}}' | sort")"
		[[ -n "${names}" ]] || continue
		printf '%-9s' "${kind}:"
		for c in ${names}; do
			on_host "docker start ${c}" >/dev/null 2>&1 && printf ' %s' "${c##*-${kind}-}" || printf ' %s!' "${c##*-${kind}-}"
		done
		echo
		# ernic has to be serving before tapsetup joins its namespace, and
		# every socket has to exist before qemu opens them.
		case "${kind}" in ernic) sleep 4 ;; tapsetup|rocjitsu) sleep 8 ;; esac
	done
	echo
	echo "guests will take a few minutes to boot; 'make cluster-status' to watch"
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
# libcurl4t64 is the AWS SDK's HTTP client and so libplugin_OBJ.so's: the
# S3-over-RDMA path still speaks ordinary HTTP for the request that carries
# the x-amz-rdma-token, and only the payload leaves the wire.
sudo -n DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
	libgflags2.2 libhwloc15 libcurl4t64 >/dev/null 2>&1 || true
sudo -n rm -rf /opt/nixl-stack
sudo -n mkdir -p /opt/nixl-stack
sudo -n tar -xzf /tmp/nixl-stack.tgz -C /opt/nixl-stack
S=/opt/nixl-stack/opt
# The image sets both of these with ENV, which a tarball of /opt cannot carry,
# so they have to be re-established here.
#
# Without FI_UNIVERSE_SIZE libfabric takes its default universe of 256, ofi_rxm
# sizes the MSG completion queue from that, the device cannot accommodate the
# result, and every LIBFABRIC run dies at `fi_enable failed for rail 0: Invalid
# argument` -- before any transfer, and with nothing in the message pointing at
# a universe size.
#
# Without UCX_ROCM_COPY_DMABUF, UCX never exports a dma-buf fd for ROCm memory
# and hands a device pointer to plain ibv_reg_mr instead, so every VRAM segment
# fails registration with EFAULT -- which reads like a NIC that cannot take GPU
# memory. `try` rather than `y` so a guest without dmabuf falls back instead of
# refusing to start. See cluster/README.md.
printf 'FI_UNIVERSE_SIZE=16\nUCX_ROCM_COPY_DMABUF=try\n' | sudo -n tee /etc/environment.d/90-nixl-stack.conf >/dev/null 2>&1 || true
printf 'export FI_UNIVERSE_SIZE=16\nexport UCX_ROCM_COPY_DMABUF=try\n' | sudo -n tee /etc/profile.d/nixl-stack.sh >/dev/null
# /etc/environment is the one that actually reaches a benchmark. profile.d is
# only sourced by a login shell and environment.d only by a systemd user
# session, so neither applies to `ssh guest 'nixlbench ...'` -- which is how
# every run here is driven. pam_env reads this file on any PAM session,
# interactive or not. Written key-by-key so an existing file is updated rather
# than replaced.
for kv in 'FI_UNIVERSE_SIZE=16' 'UCX_ROCM_COPY_DMABUF=try'; do
	k="${kv%%=*}"
	sudo -n sed -i "/^${k}=/d" /etc/environment
	printf '%s\n' "${kv}" | sudo -n tee -a /etc/environment >/dev/null
done
printf '%s\n' $S/rocm/lib $S/rocm/lib/rocm_sysdeps/lib $S/rocnixl-ucx/lib \
	$S/libfabric/lib $S/mori/lib $S/hipfile/lib $S/nixl/lib/x86_64-linux-gnu \
	$S/aws-sdk/lib $S/hipobject/lib \
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
		# nixlbench seeds an object with `aws` before an OBJ READ run, and
		# aws-cli v2's chunk-signed PutObject is more HTTP than rocm-ernic's
		# S3 server implements. See the script's header.
		copy_to_guest "${n}" "${REPO_ROOT}/cluster/scripts/aws-curl-shim.sh" /tmp/aws-shim
		in_guest "${n}" 'mkdir -p ~/bin && mv /tmp/aws-shim ~/bin/aws && chmod +x ~/bin/aws'
		in_guest "${n}" 'bash /tmp/install.sh' || echo "  FAILED"
	done

	cat <<EOF

Installed under /opt/nixl-stack/opt. To run nixlbench there:

  export NIXL_PLUGIN_DIR=/opt/nixl-stack/opt/nixl/lib/x86_64-linux-gnu/plugins
  export FI_UNIVERSE_SIZE=16 UCX_ROCM_COPY_DMABUF=try   # if your shell predates the install
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
	images) cmd_images ;;
	boot)   cmd_boot ;;
	nvme)   cmd_nvme ;;
	stack)  cmd_stack ;;
	*)      echo "usage: $0 {status|images|boot|nvme|stack}  (prep/gpu are now ansible/guest-setup.yml)" >&2; exit 2 ;;
esac
