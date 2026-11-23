#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# What a lane of .github/workflows/amd-bench-vm.yml does inside a rocjitsu
# guest before and after the nixlbench runner. The job sends it over ssh:
#
#   ssh -F CONFIG guest bash -s -- SUBCOMMAND [ARG...] < amd_bench_guest.sh
#
# 'preflight' also runs in the job container of amd-bench-container.yml, as
# root and without ssh.
#
#   rocm X.Y            fail unless the guest carries ROCm X.Y
#   gpu                 install the firmware in /tmp/rocjitsu-firmware and
#                       bind amdgpu to the emulated GPU
#   nic MAC ADDRESS     bring up the emulated RDMA NIC whose MAC starts with
#                       MAC and give it ADDRESS
#   reach ADDRESS       fail unless ADDRESS answers a ping or on port 22
#   rdma IFACE          name the RDMA device of IFACE ernic0 and report it
#   pingpong IB ADDRESS an RDMA ping-pong against the NIC's own address
#   disks-local         vm1-local: a file system on the first spare NVMe
#                       drive, the second as the raw device
#   blkdev DEV          hand DEV to this account and seed it for GUSLI
#   blkdev-access DEV   whether DEV is still this account's after it was
#                       opened for writing and closed
#   nvmeof IFACE ADDRESS NQN LLADDR FS-GEOMETRY RAW-GEOMETRY KV-NSID
#                       vm1-fabric: connect to the NVMe-oF target and prepare
#                       its namespaces; a geometry is NSID:LBA-BYTES:BYTES
#   nvmeof-down NQN     unmount and disconnect, as far as that goes
#   install PYTHON UV   a venv with the wheels in ~/amd-bench/wheels
#   preflight VENV X.Y OUT-DIR
#                       require 'nixlbench --version' to run, installing the
#                       libraries the image lacks if it does not
#   facts               print the CPU, the kernel's peer-to-peer DMA state,
#                       the KFD nodes and the ROCm runtime; never fails
#   state [DEV]         print what the diagnostics keep
#
# Results go to stdout as KEY=VALUE lines among the log: NIC_IFACE, NIC_IB,
# RDMA (ok, absent, unusable), IB_MTU, FS_DEV, RAW_DEV, BLKDEV_SEED (ok,
# failed), BLKDEV_ACCESS (ok, lost), KV_NODE, GPU_VRAM_LIMIT, CPU_VENDOR,
# CPU_FAMILY, CPU_MODEL, CPU_MODEL_NAME. The job checks each value before it
# uses it.
#
# A device is written only once the same invocation has found it to be what
# the lane expects: a drive is told from another by its controller's
# transport and NQN and by its namespace id in sysfs, never by its name.

set -euo pipefail

FS_MOUNT=/mnt/amd-bench/fs
KV_LINK=/run/amd-bench/kv
UDEV_RULE=/etc/udev/rules.d/99-amd-bench.rules
ONE_TIB=1099511627776
# nixlbench's GUSLI READ checks 0xaa from its default offset of 1 MiB on.
SEED_MIB=32
RDMA_NAME=ernic0

# soname and the Ubuntu 24.04 and 26.04 package that carries it.
LIBRARIES='libgflags.so.2.2 libgflags2.2
libhwloc.so.15 libhwloc15
libcurl.so.4 libcurl4t64
libssl.so.3 libssl3t64'

root() {
  if [ "$(id -u)" -eq 0 ]; then "$@"; else sudo -n "$@"; fi
}

out() {
  printf '%s=%s\n' "$1" "$2"
}

attr() {
  tr -s ' ' < "$1" | sed 's/ *$//'
}

usage() {
  echo "amd_bench_guest.sh: $1; the subcommands are listed at the top of the script" >&2
  exit 2
}

# --- ROCm and the GPU -------------------------------------------------------

cmd_rocm() {
  local want=$1 ver="" f
  for f in /opt/rocm/.info/version /opt/rocm/core/.info/version \
           /opt/rocm/core-*/.info/version; do
    if [ -s "$f" ]; then ver=$(head -1 "$f"); break; fi
  done
  echo "guest ROCm version: ${ver:-unknown}; ROCM_PATH: ${ROCM_PATH:-unset}"
  case "$ver" in
    "$want".*|"$want") ;;
    *)
      echo "::error::the guest carries ROCm '${ver:-unknown}', not ${want}; the guest image pin and GUEST_ROCM in the workflow disagree"
      exit 1
      ;;
  esac
}

# The guest keeps amdgpu blacklisted until the firmware files that must match
# the rocjitsu build serving it are staged.
cmd_gpu() {
  local stage=/tmp/rocjitsu-firmware base=/lib/firmware/amdgpu
  local upd=/lib/firmware/updates/amdgpu probe=/usr/local/bin/amdgpu-probe
  local f n status gpus limit
  root install -d -m 0755 "$base" "$upd"
  for f in "$stage"/*.bin; do
    n=$(basename "$f")
    if [ "$n" = ip_discovery.bin ]; then
      root install -o root -g root -m 0644 "$f" "$base/$n"
    elif [ ! -e "$upd/$n" ] && [ ! -e "$upd/$n.xz" ]; then
      # Never over a packaged blob: a stub is worse than real microcode.
      root install -o root -g root -m 0644 "$f" "$upd/$n"
    fi
  done

  if [ ! -x "$probe" ]; then
    echo "the guest image has no ${probe}"
    exit 1
  fi
  status=$("$probe" --status)
  echo "amdgpu-probe --status: ${status}"
  case "$status" in
    absent)
      echo "no emulated AMD GPU on the guest's PCI bus: the rocjitsu vfio-user device was not attached"
      exit 1
      ;;
    ready|unbound)
      root "$probe"
      ;;
    loaded)
      ;;
    *)
      echo "amdgpu cannot be probed in this guest (amdgpu-probe --status: ${status})"
      exit 1
      ;;
  esac

  # /dev/kfd exists after a probe that bound nothing; a bound GPU is a named
  # node in the KFD topology.
  gpus=0
  for _ in $(seq 40); do
    gpus=$(cat /sys/class/kfd/kfd/topology/nodes/*/name 2>/dev/null | grep -c . || true)
    if [ "${gpus:-0}" -ge 1 ]; then break; fi
    sleep 3
  done
  if [ "${gpus:-0}" -lt 1 ]; then
    echo "amdgpu did not bind the emulated GPU: no GPU node in the KFD topology after 120 s (amdgpu-probe --status: $("$probe" --status))"
    root dmesg | grep -i amdgpu | tail -40 || true
    exit 1
  fi
  echo "${gpus} GPU node(s) in the KFD topology"
  if [ ! -r /dev/kfd ] || [ ! -w /dev/kfd ]; then
    echo "$(id -un) cannot open /dev/kfd (groups: $(id -nG)); HIP would see no device"
    exit 1
  fi
  limit=$(cat /sys/module/amdgpu/parameters/vramlimit 2>/dev/null || true)
  if [[ "$limit" =~ ^[0-9]+$ ]]; then out GPU_VRAM_LIMIT "$limit"; fi
  out GPU ok
}

# --- the emulated RDMA NIC --------------------------------------------------

# There is no modprobe of ionic: the image loads ionic and ionic_rdma at boot,
# and the RDMA device is missing if it did not.
cmd_nic() {
  local mac=$1 want=$2 iface ib="" dev state
  # awk reads every link: were it to exit at the first match, ip could be
  # killed writing the rest and pipefail would end the script.
  iface=$(ip -br link | awk -v mac="$mac" 'index(tolower($3), tolower(mac)) == 1 && !seen++ {print $1}')
  if [ -z "$iface" ]; then
    echo "::error::the guest has no network interface with a ${mac} MAC: its emulated RDMA NIC is missing"
    ip -br link
    exit 1
  fi
  echo "${iface} has a ${mac} MAC"
  root ip link set "$iface" up
  if ! ip -4 -br addr show dev "$iface" | grep -qF "${want}/"; then
    echo "the guest had not given ${iface} the address ${want}; adding it"
    root ip addr add "${want}/24" dev "$iface"
  fi
  ip -4 -br addr show dev "$iface"
  out NIC_IFACE "$iface"

  for _ in $(seq 30); do
    for dev in "/sys/class/net/${iface}/device/infiniband"/*; do
      if [ -e "$dev" ]; then ib=${dev##*/}; break; fi
    done
    if [ -n "$ib" ]; then break; fi
    sleep 2
  done
  if [ -z "$ib" ]; then
    echo "${iface} has no RDMA device, so no RDMA transport is possible"
    return 0
  fi
  state=unknown
  for _ in $(seq 30); do
    state=$(cat "/sys/class/infiniband/${ib}/ports/1/state" 2>/dev/null || echo unknown)
    case "$state" in *ACTIVE*) break ;; esac
    sleep 2
  done
  echo "RDMA device ${ib} on ${iface}, port 1 ${state}"
  out NIC_IB "$ib"
}

cmd_reach() {
  local peer=$1
  for _ in $(seq 10); do
    if ping -c 1 -W 2 "$peer" >/dev/null 2>&1 \
        || timeout 5 bash -c "exec 3<>/dev/tcp/${peer}/22" 2>/dev/null; then
      echo "reached ${peer}"
      return 0
    fi
    sleep 2
  done
  echo "::error::${peer} cannot be reached over the emulated NICs' Ethernet segment"
  exit 1
}

rdma_of() {
  local dev
  for dev in "/sys/class/net/$1/device/infiniband"/*; do
    if [ -e "$dev" ]; then echo "${dev##*/}"; return 0; fi
  done
}

# UCX cannot open an RDMA device whose name has a "-" or "_" in it, and the
# env files and their probes name the device, so it gets one fixed name
# whatever it was called. RDMA=ok only once that name is seen to be in place.
cmd_rdma() {
  local iface=$1 ib now state=unknown mtu
  ib=$(rdma_of "$iface")
  if [ -z "$ib" ]; then
    echo "${iface} has no RDMA device"
    out RDMA absent
    return 0
  fi
  if [ "$ib" != "$RDMA_NAME" ]; then
    root rdma dev set "$ib" name "$RDMA_NAME" \
      || echo "rdma dev set ${ib} name ${RDMA_NAME} failed"
  fi
  now=$(rdma_of "$iface")
  if [ "$now" = "$RDMA_NAME" ]; then
    for _ in $(seq 30); do
      state=$(cat "/sys/class/infiniband/${RDMA_NAME}/ports/1/state" 2>/dev/null || echo unknown)
      case "$state" in *ACTIVE*) break ;; esac
      sleep 2
    done
  fi
  echo "RDMA device of ${iface}: was ${ib}, is ${now:-missing}, port 1 ${state}"
  case "${now}/${state}" in
    "${RDMA_NAME}"/*ACTIVE*) ;;
    *)
      out RDMA unusable
      return 0
      ;;
  esac
  out RDMA ok
  # sysfs has no active MTU; ibv_devinfo prints "active_mtu: 1024 (3)".
  mtu=$(ibv_devinfo -d "$RDMA_NAME" 2>/dev/null \
    | awk '/active_mtu:/ && !seen++ {print $2}' || true)
  if [[ "$mtu" =~ ^[0-9]+$ ]]; then
    out IB_MTU "$mtu"
  else
    echo "ibv_devinfo -d ${RDMA_NAME} gave no active_mtu"
  fi
}

# Both ends of the connection are in the one guest, on the NIC's own address.
# It may fail: the log is the answer, and the exit status is always 0.
cmd_pingpong() {
  local ib=$1 addr=$2 port="/sys/class/infiniband/$1/ports/1"
  local a b c d want gid="" path index type server_log client_log server
  local client_rc=0 server_rc=0
  ibv_devinfo -d "$ib" 2>&1 || true

  # The GID of the NIC's IPv4 address, RoCE v2 where both are listed.
  IFS=. read -r a b c d <<< "$addr"
  want=$(printf '0000:0000:0000:0000:0000:ffff:%02x%02x:%02x%02x' "$a" "$b" "$c" "$d")
  for path in "$port"/gids/*; do
    [ "$(cat "$path" 2>/dev/null)" = "$want" ] || continue
    index=${path##*/}
    type=$(cat "$port/gid_attrs/types/${index}" 2>/dev/null || echo unknown)
    echo "GID index ${index} is ${want} (${type})"
    if [ -z "$gid" ]; then gid=$index; fi
    case "$type" in *v2*) gid=$index; break ;; esac
  done
  if [ -z "$gid" ]; then
    echo "PINGPONG=no-gid: ${ib} has no GID for ${addr}"
    return 0
  fi
  if ! command -v ibv_rc_pingpong >/dev/null; then
    echo "PINGPONG=no-tool: the guest has no ibv_rc_pingpong"
    return 0
  fi

  # Ended with SIGTERM, and SIGKILL only if that is not enough: a process
  # killed in the middle of RDMA leaves the guest's RDMA device unusable.
  server_log=$(mktemp)
  client_log=$(mktemp)
  timeout --kill-after=10 60 \
    ibv_rc_pingpong -d "$ib" -i 1 -g "$gid" -s 4096 -n 200 > "$server_log" 2>&1 &
  server=$!
  sleep 2
  timeout --kill-after=10 45 \
    ibv_rc_pingpong -d "$ib" -i 1 -g "$gid" -s 4096 -n 200 "$addr" > "$client_log" 2>&1 || client_rc=$?
  wait "$server" || server_rc=$?
  echo "--- server"
  cat "$server_log"
  echo "--- client"
  cat "$client_log"
  if [ "$client_rc" -eq 0 ] && [ "$server_rc" -eq 0 ]; then
    echo "PINGPONG=pass: 200 exchanges of 4096 bytes over GID index ${gid}"
  else
    echo "PINGPONG=fail: client exit status ${client_rc}, server exit status ${server_rc}"
  fi
}

# --- drives -----------------------------------------------------------------

# The controllers a block device hangs off. A multipath head hangs off the
# subsystem, which lists them.
controllers_of() {
  local sysdev c
  sysdev=$(readlink -f "/sys/block/$1/device" 2>/dev/null) || return 0
  if [ -r "$sysdev/transport" ]; then
    echo "$sysdev"
    return 0
  fi
  for c in "$sysdev"/nvme*; do
    if [ -r "$c/transport" ]; then readlink -f "$c"; fi
  done
}

# Prints the signature blkid finds on a device, nothing when it finds none,
# and fails when blkid could not tell.
signature() {
  local text rc=0
  text=$(root blkid -p -o export "$1" 2>&1) || rc=$?
  case "$rc" in
    0) sed -n -E 's/^(TYPE|PTTYPE)=//p' <<< "$text" | tr '\n' ' ' ;;
    2) ;;
    *)
      echo "blkid:${rc}"
      return 1
      ;;
  esac
}

# Why a block device may not be written, or nothing.
in_use() {
  local blk=$1 dev="/dev/$1" sig
  if [ ! -b "$dev" ]; then
    echo "it has no block device"
  elif compgen -G "/sys/block/${blk}/${blk}p*" >/dev/null; then
    echo "it has partitions"
  elif [ -n "$(lsblk -no MOUNTPOINTS "$dev" | tr -d '[:space:]')" ]; then
    echo "it is mounted"
  elif ! sig=$(signature "$dev"); then
    echo "blkid could not read it (${sig})"
  elif [ -n "$sig" ]; then
    echo "it carries a ${sig% } signature"
  fi
}

format_fs() {
  local dev=$1
  root mkfs.ext4 -q -F -m 0 "$dev"
  root mkdir -p "$FS_MOUNT"
  root mount -o noatime "$dev" "$FS_MOUNT"
  root chmod 1777 "$FS_MOUNT"
  mkdir -p "$FS_MOUNT/posix" "$FS_MOUNT/ais"
  test "$(findmnt -no SOURCE "$FS_MOUNT")" = "$dev"
  df -h "$FS_MOUNT"
}

# Not a chown: udev puts root:disk 0660 back on an NVMe block node whenever a
# descriptor opened for writing is closed, which the seed below and nixlbench's
# GUSLI READ both do. Fails on the first thing that goes wrong; the caller
# decides what that means.
blkdev_seed() {
  local dev=$1 name=${1#/dev/} user pattern
  if [[ ! "$name" =~ ^nvme[0-9]+n[0-9]+$ ]]; then
    echo "${dev} is not an NVMe namespace's block device"
    return 1
  fi
  user=$(id -un) || return 1
  if [[ ! "$user" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo "the account name is not one a udev rule can carry"
    return 1
  fi
  printf 'SUBSYSTEM=="block", KERNEL=="%s", OWNER="%s", MODE="0660"\n' "$name" "$user" \
    | root tee "$UDEV_RULE" >/dev/null || return 1
  cat "$UDEV_RULE"
  root udevadm control --reload || return 1
  root udevadm trigger --action=change --subsystem-match=block --sysname-match="$name" || return 1
  root udevadm settle --timeout=30 || return 1
  ls -l "$dev" || return 1

  mkdir -p "$HOME/amd-bench" || return 1
  pattern="$HOME/amd-bench/aa.bin"
  head -c "$((SEED_MIB << 20))" /dev/zero | tr '\0' '\252' > "$pattern" || return 1
  root dd if="$pattern" of="$dev" bs=1M seek=1 count="$SEED_MIB" \
    oflag=direct conv=fsync,notrunc status=none || return 1
  root udevadm settle --timeout=30 || return 1
  ls -l "$dev" || return 1
  # Without sudo: this is the access nixlbench will have.
  if ! dd if="$dev" bs=1M skip=1 count="$SEED_MIB" iflag=direct status=none \
      | cmp - "$pattern"; then
    echo "${dev} does not read back as it was seeded, or $(id -un) may not read it"
    return 1
  fi
  echo "${dev}: ${SEED_MIB} MiB of 0xaa from 1 MiB on, read back by $(id -un)"
}

seed_and_report() {
  if blkdev_seed "$1"; then out BLKDEV_SEED ok; else out BLKDEV_SEED failed; fi
}

cmd_blkdev() {
  local dev=$1 blk=${1#/dev/} why root_src root_disk real
  [[ "$blk" =~ ^nvme[0-9]+n[0-9]+$ ]] || usage "blkdev wants /dev/nvmeXnY"
  why=$(in_use "$blk")
  root_src=$(findmnt -no SOURCE /)
  root_disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null | awk 'NR == 1' || true)
  for real in $(controllers_of "$blk"); do
    if controllers_of "${root_disk:-${root_src#/dev/}}" | grep -qxF "$real"; then
      why="it is on the controller of the root file system"
    fi
  done
  if [ -n "$why" ]; then
    echo "::error::refusing to write to ${dev}: ${why}"
    exit 1
  fi
  seed_and_report "$dev"
}

# In an ssh session of its own: what matters is whether the device is still
# this account's once an earlier session's descriptors are gone and this one
# has opened it for writing and closed it again.
cmd_blkdev_access() {
  local dev=$1 access=lost
  if (exec 3<>"$dev") 2>/dev/null; then
    echo "${dev} opened for reading and writing, and closed"
  else
    echo "${dev} could not be opened for reading and writing"
  fi
  root udevadm settle --timeout=30 || true
  ls -l "$dev" || true
  if [ -r "$dev" ] && [ -w "$dev" ]; then access=ok; fi
  out BLKDEV_ACCESS "$access"
}

# The spare drives are the 1 TiB namespaces of the pcie controllers that are
# not the root file system's, empty and unused. Sorted by controller serial,
# the first takes the file system and the second is the raw device.
cmd_disks_local() {
  local root_src root_disk root_ctrls c real serial ns blk why list="" spares
  local fs_blk raw_blk bytes
  lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS
  root_src=$(findmnt -no SOURCE /)
  root_disk=$(lsblk -no PKNAME "$root_src" 2>/dev/null | awk 'NR == 1' || true)
  root_ctrls=$(controllers_of "${root_disk:-${root_src#/dev/}}")
  echo "root file system: ${root_src} on ${root_disk:-unknown}"

  for c in /sys/class/nvme/nvme*; do
    [ -r "$c/transport" ] || continue
    [ "$(cat "$c/transport")" = pcie ] || continue
    real=$(readlink -f "$c")
    if grep -qxF "$real" <<< "$root_ctrls"; then continue; fi
    serial=$(attr "$c/serial")
    for ns in "$c"/nvme*n*; do
      [ -e "$ns/nsid" ] || continue
      # With native multipath the controller holds nvmeXcYnZ and the block
      # device is nvmeXnZ; without it they are the same name.
      blk=$(sed -E 's/c[0-9]+n/n/' <<< "${ns##*/}")
      bytes=$(($(cat "/sys/block/${blk}/size" 2>/dev/null || echo 0) * 512))
      why=$(in_use "$blk")
      if [ -z "$why" ] && [ "$bytes" -ne "$ONE_TIB" ]; then
        why="it has ${bytes} bytes, not the ${ONE_TIB} of a spare drive"
      fi
      if [ -n "$why" ]; then
        echo "/dev/${blk} (${c##*/}, serial ${serial}) is not a spare drive: ${why}"
        continue
      fi
      echo "/dev/${blk} (${c##*/}, serial ${serial}) is a spare drive"
      list+="${serial}"$'\t'"${blk}"$'\n'
    done
  done
  spares=$(printf '%s' "$list" | LC_ALL=C sort | cut -f 2)
  fs_blk=$(awk 'NR == 1' <<< "$spares")
  raw_blk=$(awk 'NR == 2' <<< "$spares")
  if [ -z "$fs_blk" ]; then
    echo "::error::the guest has no spare NVMe drive: no empty 1 TiB namespace on a pcie controller"
    exit 1
  fi
  format_fs "/dev/${fs_blk}"
  out FS_DEV "/dev/${fs_blk}"
  if [ -z "$raw_blk" ]; then
    echo "the guest has one spare NVMe drive only, so there is no raw device"
    return 0
  fi
  out RAW_DEV "/dev/${raw_blk}"
  seed_and_report "/dev/${raw_blk}"
}

# --- the NVMe-oF target -----------------------------------------------------

# Whether a namespace may be written: it must be behind the fabric controller
# alone, be the namespace id asked for, be unused, and have the geometry the
# backend string gave it. Prints why not.
fabric_guard() {
  local blk=$1 nsid=$2 lba=$3 bytes=$4 nqn=$5 dev="/dev/$1"
  local sysdev transports subnqn have why
  if [ ! -b "$dev" ]; then
    echo "nsid ${nsid} has no block device ${dev}"
    return 1
  fi
  sysdev=$(readlink -f "/sys/block/${blk}/device")
  if [ -r "$sysdev/transport" ]; then
    transports=$(cat "$sysdev/transport")
  else
    transports=$(cat "$sysdev"/nvme*/transport 2>/dev/null | sort -u | tr '\n' ' ' || true)
    transports=${transports% }
  fi
  subnqn=$(cat "$sysdev/subsysnqn" 2>/dev/null || true)
  if [ "$transports" != rdma ] || [ "$subnqn" != "$nqn" ]; then
    echo "${dev}: its transport is '${transports}' and its NQN '${subnqn}', not rdma and ${nqn}"
    return 1
  fi
  have=$(cat "/sys/block/${blk}/nsid" 2>/dev/null || echo "$nsid")
  if [ "$have" != "$nsid" ]; then
    echo "${dev} is namespace ${have}, not ${nsid}"
    return 1
  fi
  why=$(in_use "$blk")
  if [ -n "$why" ]; then
    echo "${dev}: ${why}"
    return 1
  fi
  have=$(cat "/sys/block/${blk}/queue/logical_block_size" 2>/dev/null || echo unknown)
  if [ "$have" != "$lba" ]; then
    echo "${dev} has ${have}-byte logical blocks, not the ${lba} of the backend string"
    return 1
  fi
  have=$(($(cat "/sys/block/${blk}/size" 2>/dev/null || echo 0) * 512))
  if [ "$have" -ne "$bytes" ]; then
    echo "${dev} has ${have} bytes, not the ${bytes} of the backend string"
    return 1
  fi
  echo "${dev}: nsid ${nsid}, ${lba}-byte blocks, ${bytes} bytes, behind the rdma controller of ${nqn} alone"
}

cmd_nvmeof() {
  local iface=$1 ip=$2 nqn=$3 lladdr=$4 fs_geom=$5 raw_geom=$6 kv_nsid=$7
  local fs_nsid fs_lba fs_bytes raw_nsid raw_lba raw_bytes
  local c ctrl address count d nsid generic why
  local -a ctrls=()
  local -A blk_of=()
  IFS=: read -r fs_nsid fs_lba fs_bytes <<< "$fs_geom"
  IFS=: read -r raw_nsid raw_lba raw_bytes <<< "$raw_geom"
  for d in "$fs_nsid" "$fs_lba" "$fs_bytes" "$raw_nsid" "$raw_lba" "$raw_bytes" "$kv_nsid"; do
    [[ "$d" =~ ^[0-9]+$ ]] || usage "a geometry is NSID:LBA-BYTES:BYTES"
  done

  # -a, or the second name is taken as a parameter of the first.
  root modprobe -a nvme-fabrics nvme-rdma
  # Nothing answers ARP for the target, so rdma_resolve_addr() needs the
  # neighbour given to it.
  root ip neigh replace "$ip" lladdr "$lladdr" dev "$iface" nud permanent
  ip neigh show "$ip" dev "$iface"
  if ! root timeout 120 nvme connect -t rdma -a "$ip" -s 4420 -n "$nqn"; then
    echo "::error::nvme connect -t rdma -a ${ip} -s 4420 -n ${nqn} failed"
    root dmesg | tail -n 30 || true
    exit 1
  fi
  root udevadm settle --timeout=30 || true

  for c in /sys/class/nvme/nvme*; do
    [ -r "$c/transport" ] || continue
    [ "$(cat "$c/transport")" = rdma ] || continue
    [ "$(cat "$c/subsysnqn")" = "$nqn" ] || continue
    ctrls+=("$c")
  done
  if [ "${#ctrls[@]}" -ne 1 ]; then
    echo "::error::${#ctrls[@]} controllers have the transport rdma and the NQN ${nqn}; the lane needs exactly one"
    exit 1
  fi
  ctrl=${ctrls[0]}
  address=$(cat "$ctrl/address")
  echo "${ctrl##*/}: transport rdma, address ${address}, model $(attr "$ctrl/model"), serial $(attr "$ctrl/serial")"
  case "$address" in
    *"traddr=${ip}"*) ;;
    *)
      echo "::error::the address of ${ctrl##*/} does not name the target ${ip}"
      exit 1
      ;;
  esac

  count=0
  for _ in $(seq 15); do
    count=0
    for d in "$ctrl"/nvme*n*; do
      if [ -e "$d/nsid" ]; then count=$((count + 1)); fi
    done
    if [ "$count" -ge 3 ]; then break; fi
    sleep 2
  done
  if [ "$count" -ne 3 ]; then
    echo "::error::${ctrl##*/} has ${count} namespaces, not the 3 the backend string defines"
    exit 1
  fi
  # Names do not follow namespace ids, so each is looked up.
  for d in "$ctrl"/nvme*n*; do
    [ -e "$d/nsid" ] || continue
    nsid=$(cat "$d/nsid")
    blk_of[$nsid]=$(sed -E 's/c[0-9]+n/n/' <<< "${d##*/}")
    echo "nsid ${nsid} is ${blk_of[$nsid]}"
  done
  for nsid in "$fs_nsid" "$raw_nsid" "$kv_nsid"; do
    if [ -z "${blk_of[$nsid]:-}" ]; then
      echo "::error::${ctrl##*/} has no namespace ${nsid}"
      exit 1
    fi
  done

  if ! why=$(fabric_guard "${blk_of[$fs_nsid]}" "$fs_nsid" "$fs_lba" "$fs_bytes" "$nqn"); then
    echo "::error::refusing to make a file system: ${why}"
    exit 1
  fi
  echo "$why"
  format_fs "/dev/${blk_of[$fs_nsid]}"
  out FS_DEV "/dev/${blk_of[$fs_nsid]}"

  if ! why=$(fabric_guard "${blk_of[$raw_nsid]}" "$raw_nsid" "$raw_lba" "$raw_bytes" "$nqn"); then
    echo "::error::refusing to seed the raw device: ${why}"
    exit 1
  fi
  echo "$why"
  out RAW_DEV "/dev/${blk_of[$raw_nsid]}"
  seed_and_report "/dev/${blk_of[$raw_nsid]}"

  # Linux attaches a Key Value namespace with no usable block device and
  # keeps its generic character device. Nothing is written to it here.
  generic="/dev/ng${blk_of[$kv_nsid]#nvme}"
  if [ -c "$generic" ]; then
    root mkdir -p "${KV_LINK%/*}"
    root ln -sfn "$generic" "$KV_LINK"
    out KV_NODE "$generic"
  else
    echo "nsid ${kv_nsid} has no generic node ${generic}"
  fi
  root nvme list || true
}

cmd_nvmeof_down() {
  local nqn=$1
  root umount "$FS_MOUNT" || true
  root timeout 60 nvme disconnect -n "$nqn" || true
}

# --- the wheels -------------------------------------------------------------

cmd_install() {
  local py=$1 uv_version=$2 work="$HOME/amd-bench" venv uv
  venv="$work/venv"
  ls -l "$work/wheels"
  # uv supplies the requested interpreter when the guest's own python3 is
  # another version.
  python3 -m venv "$work/uv-bootstrap"
  "$work/uv-bootstrap/bin/pip" install --quiet "uv==${uv_version}"
  uv="$work/uv-bootstrap/bin/uv"
  "$uv" venv --python "$py" "$venv"
  # numpy is rocm-nixl's only unconditional dependency and the only package
  # taken from an index. The wheels are resolved with no index, so they can
  # pull nothing else in.
  "$uv" pip install --python "$venv/bin/python" numpy
  "$uv" pip install --python "$venv/bin/python" --no-index \
    --find-links "$work/wheels" rocm-nixl rocm-nixlbench
  "$uv" pip list --python "$venv/bin/python" | tee "$work/pip-list.txt"
  test -x "$venv/bin/nixlbench"
}

# Every symbol bound at start, against the libnixl of the NIXL wheel and the
# ROCm install on disk, and with what the launcher selected in the capture.
gate() {
  local rc=0
  LD_BIND_NOW=1 NIXLBENCH_WHEEL_DEBUG=1 "$1" --version > "$2" 2>&1 || rc=$?
  cat "$2"
  return "$rc"
}

# Prints ldd of the nixlbench binary, and of the NIXL plugins when asked for,
# with the loader path the launcher sets.
libraries() {
  local venv=$1 xy=$2 capture=$3 plugins=$4 binary path dir f
  binary=$(sed -n 's/^nixlbench: binary realpath: //p' "$capture" | awk 'NR == 1')
  if [ ! -x "$binary" ]; then
    for f in "$venv"/lib/python*/site-packages/"nixlbench_rocm${xy/./_}"/bin/nixlbench; do
      if [ -x "$f" ]; then binary=$f; break; fi
    done
  fi
  path=$(sed -n 's/^nixlbench: LD_LIBRARY_PATH: //p' "$capture" | awk 'NR == 1')
  path=${path:-/opt/rocm/core-${xy}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}}
  echo "--- ${binary:-no nixlbench binary found} (LD_LIBRARY_PATH=${path})"
  if [ -x "$binary" ]; then LD_LIBRARY_PATH="$path" ldd "$binary" 2>&1 || true; fi
  if [ "$plugins" != plugins ]; then return 0; fi
  dir=$(sed -n 's/^nixlbench: NIXL plugins: //p' "$capture" | awk 'NR == 1')
  for f in "${dir:-/nonexistent}"/*.so; do
    [ -e "$f" ] || continue
    echo "--- ${f}"
    LD_LIBRARY_PATH="$path" ldd "$f" 2>&1 || true
  done
}

cmd_preflight() {
  local venv=$1 xy=$2 dir=$3 bench="$1/bin/nixlbench" soname package
  local -a packages=()
  mkdir -p "$dir"
  if [ ! -x "$bench" ]; then
    echo "::error::${bench} is not installed"
    exit 1
  fi
  if gate "$bench" "$dir/nixlbench-version.txt"; then
    echo "nixlbench --version ran"
  else
    echo "::warning::nixlbench --version failed; looking for the libraries it lacks"
    cp "$dir/nixlbench-version.txt" "$dir/nixlbench-version-first.txt"
    libraries "$venv" "$xy" "$dir/nixlbench-version.txt" binary | tee "$dir/ldd-first.txt"
    while read -r soname package; do
      if grep -qE "^[[:space:]]*${soname//./\\.} => not found" "$dir/ldd-first.txt"; then
        packages+=("$package")
      fi
    done <<< "$LIBRARIES"
    if [ "${#packages[@]}" -gt 0 ]; then
      echo "installing ${packages[*]}"
      root env DEBIAN_FRONTEND=noninteractive apt-get update
      root env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y --no-install-recommends "${packages[@]}"
    else
      echo "none of the libraries this script knows a package for is missing"
    fi
    if ! gate "$bench" "$dir/nixlbench-version.txt"; then
      echo "::error::nixlbench --version does not run, so no case can; see the output above and ldd-first.txt"
      exit 1
    fi
    echo "nixlbench --version ran once ${packages[*]:-nothing} was installed"
  fi

  # For the diagnostics only. gflags exits 1 after printing the help, so the
  # exit status says nothing.
  "$bench" --help > "$dir/nixlbench-help.txt" 2>&1 || true
  if ! grep -q "NIXL Benchmark" "$dir/nixlbench-help.txt"; then
    echo "::warning::nixlbench --help did not print its usage; see nixlbench-help.txt"
  fi
  libraries "$venv" "$xy" "$dir/nixlbench-version.txt" plugins > "$dir/ldd.txt" 2>&1 || true
  if grep -q "not found" "$dir/ldd.txt"; then
    echo "libraries the loader does not find (a plugin that lacks one cannot be loaded):"
    grep "not found" "$dir/ldd.txt" | sort -u || true
  fi
}

# --- diagnostics ------------------------------------------------------------

# usage: cpu_field KEY; KEY of the first processor in /proc/cpuinfo.
cpu_field() {
  awk -F ': *' -v key="$1" '$1 ~ "^" key "[ \t]*$" && !seen++ {print $2}' /proc/cpuinfo
}

# What a transfer between GPU memory and another device rests on. The kernel
# allows peer-to-peer DMA across a host bridge only on an AMD CPU of family
# 23 (Zen) or later, and a guest under KVM may show its host's vendor
# whatever CPU model it was started with.
cmd_facts() {
  set +e
  local vendor family model name pci node drv f real seen=""
  vendor=$(cpu_field vendor_id)
  family=$(cpu_field 'cpu family')
  model=$(cpu_field model)
  name=$(cpu_field 'model name')
  echo "--- CPU"
  echo "vendor_id: ${vendor:-unknown}; cpu family: ${family:-unknown}; model: ${model:-unknown}; model name: ${name:-unknown}"
  lscpu 2>&1
  # Plain values only: the job puts them in a notice.
  out CPU_VENDOR "$(printf '%s' "${vendor:-unknown}" | tr -cs 'A-Za-z0-9.-' '_')"
  out CPU_FAMILY "$(printf '%s' "${family:-unknown}" | tr -cs 'A-Za-z0-9.-' '_')"
  out CPU_MODEL "$(printf '%s' "${model:-unknown}" | tr -cs 'A-Za-z0-9.-' '_')"
  out CPU_MODEL_NAME "$(printf '%s' "${name:-unknown}" | tr -cs 'A-Za-z0-9.-' '_')"

  echo "--- kernel log lines that mention p2p"
  root dmesg 2>&1 | grep -i p2p || echo "none"
  echo "--- pci_p2pdma of the AMD PCI functions"
  for pci in /sys/bus/pci/devices/*; do
    [ "$(cat "$pci/vendor" 2>/dev/null)" = 0x1002 ] || continue
    drv=none
    if [ -e "$pci/driver" ]; then drv=$(basename "$(readlink -f "$pci/driver")"); fi
    echo "${pci##*/} device=$(cat "$pci/device") class=$(cat "$pci/class") driver=${drv}"
    if [ -d "$pci/p2pmem" ]; then
      grep -H . "$pci"/p2pmem/* 2>&1
    else
      echo "${pci##*/} has no p2pmem directory: it publishes no peer-to-peer memory of its own"
    fi
  done
  echo "--- every PCI function with a p2pmem directory"
  ls -d /sys/bus/pci/devices/*/p2pmem 2>/dev/null || echo "none"
  echo "--- PCI topology"
  lspci -tvnn 2>&1

  echo "--- KFD nodes"
  for node in /sys/class/kfd/kfd/topology/nodes/*; do
    [ -d "$node" ] || continue
    echo "node ${node##*/}: name '$(cat "$node/name" 2>/dev/null)', gpu_id $(cat "$node/gpu_id" 2>/dev/null)"
    cat "$node/properties" 2>/dev/null || root cat "$node/properties" 2>&1
    for f in "$node"/mem_banks/*/properties; do
      [ -e "$f" ] || continue
      echo "${f#"$node"/}:"
      cat "$f" 2>/dev/null || root cat "$f" 2>&1
    done
  done

  echo "--- ROCm and HIP"
  echo "ROCM_PATH: ${ROCM_PATH:-unset}"
  for f in /opt/rocm/.info/version /opt/rocm/core/.info/version \
           /opt/rocm/core-*/.info/version; do
    if [ -s "$f" ]; then echo "${f}: $(head -1 "$f")"; fi
  done
  # The file a soname resolves to carries the runtime's own version. One
  # install is often reachable under several of these names.
  for f in /opt/rocm/lib /opt/rocm/core/lib /opt/rocm/core-*/lib; do
    [ -d "$f" ] || continue
    real=$(readlink -f "$f")
    case " ${seen} " in *" ${real} "*) continue ;; esac
    seen="${seen} ${real}"
    ls -l "$real"/libamdhip64.so* "$real"/libhsa-runtime64.so* "$real"/libhipfile.so* 2>/dev/null
  done
  for f in /opt/rocm/bin/hipconfig /opt/rocm/core/bin/hipconfig \
           /opt/rocm/core-*/bin/hipconfig; do
    [ -x "$f" ] || continue
    echo "${f} --version: $("$f" --version 2>&1 | head -1)"
    break
  done
  echo "kernel $(uname -r); amdgpu module version: $(cat /sys/module/amdgpu/version 2>/dev/null || echo 'none, the module is the kernel'"'"'s')"
  return 0
}

cmd_state() {
  local dev=${1:-}
  echo "amdgpu-probe --status: $(/usr/local/bin/amdgpu-probe --status 2>&1 || true)"
  echo "--- KFD topology node names"
  cat /sys/class/kfd/kfd/topology/nodes/*/name 2>&1 || true
  echo "--- AMD PCI functions and their drivers"
  local pci drv
  for pci in /sys/bus/pci/devices/*; do
    [ "$(cat "$pci/vendor" 2>/dev/null)" = 0x1002 ] || continue
    drv=none
    if [ -e "$pci/driver" ]; then drv=$(basename "$(readlink -f "$pci/driver")"); fi
    echo "$(basename "$pci") device=$(cat "$pci/device") class=$(cat "$pci/class") driver=${drv}"
  done
  echo "--- device nodes"
  ls -l /dev/kfd /dev/dri 2>&1 || true
  echo "--- amdgpu module parameters"
  grep -H . /sys/module/amdgpu/parameters/{emu_mode,fw_load_type,discovery,ip_block_mask,vm_update_mode,gpu_recovery,vramlimit} 2>&1 || true
  echo "--- network"
  ip -br link 2>&1 || true
  ip -br addr 2>&1 || true
  ip route 2>&1 || true
  ip neigh 2>&1 || true
  echo "--- RDMA"
  rdma link 2>&1 || true
  ibv_devinfo -v 2>&1 || true
  lsmod 2>&1 | grep -E '^(ionic|ib_|rdma_|nvme)' || true
  echo "--- block devices"
  lsblk -o NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS 2>&1 || true
  echo "--- nvme list"
  root nvme list 2>&1 || true
  echo "--- NVMe controllers"
  grep -H . /sys/class/nvme/nvme*/{transport,subsysnqn,address,model,serial,state} 2>&1 || true
  grep -H . /sys/class/nvme/nvme*/nvme*n*/nsid 2>&1 || true
  grep -H . /sys/block/nvme*/{size,queue/logical_block_size,hidden} 2>&1 || true
  ls -l /dev/ng* "$KV_LINK" 2>&1 || true
  if [ -n "$dev" ]; then
    echo "--- the raw device ${dev}"
    ls -l "$dev" 2>&1 || true
    udevadm info "$dev" 2>&1 || true
    cat "$UDEV_RULE" 2>&1 || true
  fi
  echo "--- mounts"
  findmnt 2>&1 || true
  df -h 2>&1 || true
  echo "--- memory"
  free -m 2>&1 || true
  echo "--- ROCm"
  cat /opt/rocm/core-*/.info/version /etc/rocjitsu-guest.json 2>&1 || true
}

# --- command line -----------------------------------------------------------

main() {
  local command=${1:-}
  if [ "$#" -gt 0 ]; then shift; fi
  case "${command}:$#" in
    rocm:1) cmd_rocm "$@" ;;
    gpu:0) cmd_gpu ;;
    nic:2) cmd_nic "$@" ;;
    reach:1) cmd_reach "$@" ;;
    rdma:1) cmd_rdma "$@" ;;
    pingpong:2) cmd_pingpong "$@" ;;
    disks-local:0) cmd_disks_local ;;
    blkdev:1) cmd_blkdev "$@" ;;
    blkdev-access:1) cmd_blkdev_access "$@" ;;
    nvmeof:7) cmd_nvmeof "$@" ;;
    nvmeof-down:1) cmd_nvmeof_down "$@" ;;
    install:2) cmd_install "$@" ;;
    preflight:3) cmd_preflight "$@" ;;
    facts:0) cmd_facts ;;
    state:0|state:1) cmd_state "$@" ;;
    *) usage "unknown subcommand, or the wrong number of arguments: '${command}' with $#" ;;
  esac
}

# Read whole before anything runs, and nothing it starts reads the script.
main "$@" < /dev/null
