#!/usr/bin/env bash
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# The job-container side of a lane of .github/workflows/amd-bench-vm.yml: it
# boots the lane's rocjitsu stack, prepares the guests through
# amd_bench_guest.sh, and collects what the runner needs and what the
# diagnostics keep. 'render' serves amd-bench-container.yml as well.
#
#   amd_bench_stack.sh SUBCOMMAND
#
#   pins           check the overlay against the job container; note the
#                  digests for the run-meta
#   override       fabric lanes: write the compose override that selects the
#                  NIC backend
#   ernic-image    fabric lanes: check the pinned NIC server image has the
#                  backend and takes the backend string
#   up             boot the stack
#   compose-merge  fabric lanes: check compose replaced what the override names
#   backend-check  fabric lanes: check the NIC server runs the backend
#   ssh            join the stack's network and wait for the guests
#   rocm           check the guests' ROCm version
#   gpu            stage the GPU firmware and load amdgpu; a failure withholds
#                  'gpu' and does not fail
#   facts          record the runner's and the guests' CPU and what
#                  peer-to-peer DMA in the guests rests on; never fails
#   prepare        the lane's own preparation of drives and NICs
#   install        install the wheels in every guest
#   preflight      require 'nixlbench --version' to run in every guest
#   render         write env.json and run-meta.json and validate them
#   diagnostics    collect logs and state; never fails
#   fabric-down    vm1-fabric: unmount and disconnect; never fails
#   down           take the stack down; never fails
#
# The steps boot the stack as rocm-wheels-rocjitsu.yml does and select the NIC
# backend as amd-bench-probe.yml does; change them together.
#
# From the workflow: LANE, ROCM_VERSION, QEMU_MINIMAL_REF, RUN_META, and for
# the VM lanes PYTHON_VERSION, UV_VERSION, GUEST_ROCM, BENCH_DIAG, NIXL_WHEEL,
# NIXLBENCH_WHEEL and the nonroot-user action's CIUSER_UID and CIUSER_HOME.
#
# State between steps is kept under $RUNNER_TEMP/amd-bench: 'withhold' (one
# need per line), 'env-set' and 'meta-set' (NAME=VALUE lines for
# 'amd_bench_ci.py env --set' and 'meta --set'), 'meta-set-int', the guests'
# ssh configuration and the name of the raw device.

set -euo pipefail

: "${LANE:?set LANE to the lane this job runs}"
: "${RUNNER_TEMP:?}" "${GITHUB_WORKSPACE:?}"

work="${RUNNER_TEMP}/amd-bench"
scripts="${GITHUB_WORKSPACE}/.ci/scripts"
SSH_CONFIG="${work}/guest-ssh-config"
BENCH_DIAG=${BENCH_DIAG:-${GITHUB_WORKSPACE}/bench-diagnostics}

# What qemu-tool gives the first NIC server of a fleet. The override restates
# them, and backend-check compares them with the generated file.
ERNIC_SOCK=/run/vfu/ernic-1.sock
ERNIC_STATS=/run/ernic-stats/ernic-1.stats
ERNIC_MAC='72:6f:63:6d:00:01'
# qemu-tool's defaults, which rocm-golden.xml leaves alone: guest N's emulated
# NIC has a MAC with this prefix and the address ERNIC_SUBNET.(10+N).
ERNIC_MAC_PREFIX='72:6f:63:6d'
ERNIC_SUBNET=192.168.100
# Where the nvmeof target answers: any free address of the NIC's subnet.
ERNIC_PEER_IP=192.168.100.1
# The target's default NQN.
NVMEOF_NQN=nvmet-test
# The target answers no ARP, so the guest is given a neighbour entry; any
# link-layer address does.
NVMEOF_NEIGH_MAC='02:00:00:00:c0:01'
# nsid 1 holds the file system, nsid 2 is the raw device and nsid 3 the Key
# Value namespace. The geometries are those of the string: NSID:LBA:BYTES.
NVMEOF_BACKEND="nvmeof:ip=${ERNIC_PEER_IP},size=1024M,bs=4096,ns=2,size=64M,bs=512,ns=3,kv=1,size=64M,keys=4096"
NVMEOF_FS=1:4096:1073741824
NVMEOF_RAW=2:512:67108864
NVMEOF_KV_NSID=3

case "$LANE" in
  bench-container)
    OVERLAY=""
    GUESTS=()
    ERNIC_BACKEND=""
    ;;
  vm1-local)
    OVERLAY=.ci/rocjitsu/fleet-overlay-bench-local.xml
    GUESTS=(guest)
    ERNIC_BACKEND=mesh
    ;;
  vm2-net)
    OVERLAY=.ci/rocjitsu/fleet-overlay-2vm.xml
    GUESTS=(guest1 guest2)
    ERNIC_BACKEND=mesh
    ;;
  vm1-loop)
    OVERLAY=.ci/rocjitsu/fleet-overlay-backends.xml
    GUESTS=(guest)
    ERNIC_BACKEND=loopback
    ;;
  vm1-fabric)
    OVERLAY=.ci/rocjitsu/fleet-overlay-backends.xml
    GUESTS=(guest)
    ERNIC_BACKEND=$NVMEOF_BACKEND
    ;;
  *)
    echo "::error::amd_bench_stack.sh has no lane of this name"
    exit 2
    ;;
esac
# Under /var/lib/qemu-tool because the compose bind mounts are resolved by the
# host daemon. The compose project is the stack directory's basename.
STACK_NAME="nixl-bench-${LANE}"
STACK_DIR="/var/lib/qemu-tool/stacks/${STACK_NAME}"
KEY_DIR="/var/lib/qemu-tool/${STACK_NAME}-key"

die() {
  echo "::error title=AMD bench ${LANE}::$1"
  exit 1
}

fabric_lane() {
  [ "$ERNIC_BACKEND" != mesh ] && [ -n "$ERNIC_BACKEND" ]
}

withhold() {
  echo "::warning title=AMD bench ${LANE}::$2; the cases that need '$1' end as skip/env"
  echo "$1" >> "${work}/withhold"
}

env_set() {
  printf '%s=%s\n' "$1" "$2" >> "${work}/env-set"
}

meta_set() {
  printf '%s=%s\n' "$1" "$2" >> "${work}/meta-set"
}

meta_set_int() {
  printf '%s=%s\n' "$1" "$2" >> "${work}/meta-set-int"
}

as_ciuser() {
  setpriv --reuid "$CIUSER_UID" --regid "$CIUSER_UID" --init-groups \
    env HOME="$CIUSER_HOME" "$@"
}

container_of() {
  local found
  found=$(docker ps -aq \
    --filter "label=com.docker.compose.project=${STACK_NAME}" \
    --filter "label=com.docker.compose.service=$1")
  echo "${found%%$'\n'*}"
}

# usage: guest_run GUEST LOG SUBCOMMAND [ARG...]
# Runs a subcommand of amd_bench_guest.sh in a guest, with its output in the
# job log and in LOG under the diagnostics. The arguments reach the guest's
# shell on a command line, so only plain ones are sent.
guest_run() {
  local guest=$1 log=$2 arg rc=0
  shift 2
  for arg in "$@"; do
    if [[ ! "$arg" =~ ^[A-Za-z0-9_.:/,=-]+$ ]]; then
      echo "::error::refusing to send an argument that is not plain to ${guest}"
      return 2
    fi
  done
  ssh -F "$SSH_CONFIG" "$guest" bash -s -- "$@" < "${scripts}/amd_bench_guest.sh" 2>&1 \
    | tee -a "${BENCH_DIAG}/${log}" || rc=${PIPESTATUS[0]}
  return "$rc"
}

# usage: guest_value LOG KEY
# The last KEY=VALUE line a guest script printed, if the value is plain.
guest_value() {
  local value
  value=$(sed -n "s/^$2=//p" "${BENCH_DIAG}/$1" 2>/dev/null | tail -n 1)
  if [[ "$value" =~ ^[A-Za-z0-9_.:/,-]+$ ]]; then echo "$value"; fi
}

overlay_digest() {
  sed -n "s:.*<$1>.*@\(sha256\:[0-9a-f]\{64\}\)</$1>.*:\1:p" "${GITHUB_WORKSPACE}/${OVERLAY}"
}

runner_state() {
  {
    echo "--- $1"
    df -h / /var/lib/qemu-tool 2>&1 || true
    free -m 2>&1 || true
  } | tee -a "${BENCH_DIAG}/runner.txt"
}

# --- before the boot --------------------------------------------------------

# The image is pinned twice, here and in the overlay, and the two are moved by
# hand.
cmd_pins() {
  local fleet="${GITHUB_WORKSPACE}/${OVERLAY}" want have name digest
  test -f "$fleet" || die "${OVERLAY} is missing"
  mkdir -p "$work"
  want=$(sed -n 's:.*<qemu-ref>\(.*\)</qemu-ref>.*:\1:p' "$fleet")
  have=$(docker inspect -f '{{.Config.Image}}' "$(cat /etc/hostname)" 2>/dev/null || true)
  if [ -z "$have" ]; then
    echo "::warning::the job container's image could not be read, so it was not compared with <qemu-ref> of ${OVERLAY}"
  elif [ "$have" != "$want" ]; then
    die "the job container is ${have} but <qemu-ref> of ${OVERLAY} is ${want:-missing}; move the workflow's pins with the overlay's"
  fi
  meta_set runner_image "${have:-$want}"
  meta_set overlay_sha256 "$(sha256sum "$fleet" | cut -d ' ' -f 1)"
  meta_set qemu_minimal_ref "$QEMU_MINIMAL_REF"
  meta_set ernic_backend "$ERNIC_BACKEND"
  for name in guest_image_digest:backing-ref qemu_digest:qemu-ref \
              ernic_digest:ernic-ref rocjitsu_digest:rocjitsu-ref; do
    digest=$(overlay_digest "${name#*:}")
    if [ -n "$digest" ]; then
      meta_set "${name%%:*}" "$digest"
    else
      echo "::warning::${OVERLAY} pins <${name#*:}> by no digest, so the run-meta has no ${name%%:*}"
    fi
  done
  cat "${work}/meta-set"
  runner_state "before the boot"
}

# quick-start leaves a file of this name in the stack directory alone, and
# compose reads it because it is run there with no -f. It replaces the
# command of the one NIC server and the health check of the mesh.
cmd_override() {
  local fleet="${GITHUB_WORKSPACE}/${OVERLAY}"
  fabric_lane || die "the lane ${LANE} selects no NIC backend"
  # With a second guest the other NIC server would wait for a mesh manager
  # that is gone; with the TAP the server is started through an entrypoint,
  # which ignores a replaced command.
  if ! grep -qF '<vm-count>1</vm-count>' "$fleet" \
      || ! grep -qF '<ernic-tap>false</ernic-tap>' "$fleet"; then
    die "${OVERLAY} must set <vm-count>1</vm-count> and <ernic-tap>false</ernic-tap>; the override is not valid for any other stack"
  fi
  install -d -o "$CIUSER_UID" -g "$CIUSER_UID" "$STACK_DIR"
  cat > "${STACK_DIR}/docker-compose.override.yml" <<EOF
services:
  ernic-1:
    command: ["rocm-ernic", "-s", "${ERNIC_SOCK}", "-b", "${ERNIC_BACKEND}", "-m", "${ERNIC_MAC}", "-S", "${ERNIC_STATS}"]
    healthcheck:
      test: ["CMD", "sh", "-c", "test -S ${ERNIC_SOCK}"]
EOF
  chown "$CIUSER_UID:$CIUSER_UID" "${STACK_DIR}/docker-compose.override.yml"
  cat "${STACK_DIR}/docker-compose.override.yml"
  cp "${STACK_DIR}/docker-compose.override.yml" "${BENCH_DIAG}/"
}

cmd_ernic_image() {
  local ref missing="" word rc=0
  fabric_lane || die "the lane ${LANE} selects no NIC backend"
  ref=$(sed -n 's:.*<ernic-ref>\(.*\)</ernic-ref>.*:\1:p' "${GITHUB_WORKSPACE}/${OVERLAY}")
  [ -n "$ref" ] || die "${OVERLAY} has no <ernic-ref>"
  # --help is written to stderr.
  docker run --rm "$ref" rocm-ernic --help > "${BENCH_DIAG}/ernic-help.txt" 2>&1 || true
  for word in loopback nvmeof; do
    if ! grep -qw -- "$word" "${BENCH_DIAG}/ernic-help.txt"; then missing="${missing} ${word}"; fi
  done
  [ -z "$missing" ] || die "rocm-ernic --help of the pinned image does not list:${missing}"

  # A malformed backend string is refused before the server starts, so a
  # server that is still running when the timeout ends it took the string.
  docker run --rm "$ref" timeout 5 rocm-ernic -s /tmp/probe.sock -b "$ERNIC_BACKEND" -L info \
    > "${BENCH_DIAG}/ernic-dry-run.txt" 2>&1 || rc=$?
  if [ "$rc" -eq 124 ] && grep -qF -- "Backend: ${ERNIC_BACKEND}" "${BENCH_DIAG}/ernic-dry-run.txt"; then
    echo "the pinned image starts with the backend string ${ERNIC_BACKEND}"
  else
    echo "::warning title=AMD bench ${LANE}::a dry run of the pinned NIC server image with the backend string ended with exit status ${rc}: $(tail -n 1 "${BENCH_DIAG}/ernic-dry-run.txt")"
  fi
}

# --- the boot ---------------------------------------------------------------

# Run from the qemu-minimal checkout so the qemu container installs the
# pinned qemu-tool from it.
cmd_up() {
  cd "${GITHUB_WORKSPACE}/qemu-minimal"
  as_ciuser qemu-tool quick-start up \
    --xml qemu/stacks/rocm-golden.xml \
    --xml "${GITHUB_WORKSPACE}/${OVERLAY}" \
    --stack-dir "$STACK_DIR" \
    --key-dir "$KEY_DIR" \
    --in-network \
    --no-metrics \
    --yes
  runner_state "after the boot"
}

# Whether compose replaces "command" and "healthcheck.test" with the
# override's, rather than appending to them, is what a fabric lane rests on.
cmd_compose_merge() {
  local merged="${BENCH_DIAG}/compose-config.json" verdict status detail
  test -f "${STACK_DIR}/docker-compose.yml" || die "quick-start generated no compose file"
  if ! as_ciuser qemu-tool compose --stack-dir "$STACK_DIR" config --format json \
      > "$merged" 2> "${BENCH_DIAG}/compose-config.err"; then
    die "docker compose config failed: $(tail -n 1 "${BENCH_DIAG}/compose-config.err")"
  fi
  verdict=$(python3 -I -c '
import json
import sys

merged, backend, mac, sock, stats = sys.argv[1:]
try:
    with open(merged, encoding="utf-8") as f:
        service = json.load(f)["services"]["ernic-1"]
except (OSError, ValueError, KeyError) as e:
    print(f"fail\tcould not read the merged configuration: {e!r}")
    sys.exit(0)
want_command = ["rocm-ernic", "-s", sock, "-b", backend, "-m", mac, "-S", stats]
want_test = ["CMD", "sh", "-c", "test -S " + sock]
command = service.get("command")
entrypoint = service.get("entrypoint")
test = (service.get("healthcheck") or {}).get("test")
wrong = []
if command != want_command:
    wrong.append(f"command is {command}")
if entrypoint:
    wrong.append(f"entrypoint is {entrypoint}")
if test != want_test:
    wrong.append(f"healthcheck.test is {test}")
if wrong:
    print("fail\tcompose did not replace the generated values: " + "; ".join(wrong))
else:
    print("pass\tcommand and healthcheck.test of ernic-1 are those of the override alone, with no entrypoint")
' "$merged" "$ERNIC_BACKEND" "$ERNIC_MAC" "$ERNIC_SOCK" "$ERNIC_STATS") \
    || verdict=$'fail\tcould not compare the merged configuration with the override'
  IFS=$'\t' read -r status detail <<< "$verdict"
  [ "$status" = pass ] || die "$detail"
  echo "$detail"
}

# On what the server reports, not on its arguments.
cmd_backend_check() {
  local c entrypoint
  c=$(container_of ernic-1)
  [ -n "$c" ] || die "the compose project ${STACK_NAME} has no ernic-1 container"
  echo "ernic-1 is $(docker inspect -f '{{.Name}} ({{.State.Status}})' "$c")"
  docker logs "$c" > "${BENCH_DIAG}/ernic-1-boot.log" 2>&1 || true
  if ! grep -qF -- "Backend: ${ERNIC_BACKEND}" "${BENCH_DIAG}/ernic-1-boot.log"; then
    die "the NIC server does not report the requested backend; it reports '$(grep -m 1 -F 'Backend:' "${BENCH_DIAG}/ernic-1-boot.log" || echo nothing)'"
  fi
  # A stack with the TAP starts the server from an entrypoint, and the
  # override's command would be unused arguments to it.
  entrypoint=$(docker inspect -f '{{json .Config.Entrypoint}}' "$c")
  [ "$entrypoint" = null ] \
    || die "the NIC server's container has the entrypoint ${entrypoint}, so the override's command is not what runs"
  # The socket path, the MAC and the statistics path the override hardcodes.
  if ! grep -qF -- "- \"${ERNIC_SOCK}\"" "${STACK_DIR}/docker-compose.yml" \
      || ! grep -qF -- "- \"${ERNIC_MAC}\"" "${STACK_DIR}/docker-compose.yml" \
      || ! grep -qF -- "- \"${ERNIC_STATS}\"" "${STACK_DIR}/docker-compose.yml"; then
    die "the generated compose file does not give ernic-1 the socket ${ERNIC_SOCK}, the MAC ${ERNIC_MAC} and the statistics file ${ERNIC_STATS} that the override hardcodes"
  fi
  echo "the NIC server reports Backend: ${ERNIC_BACKEND}"
}

# A job container cannot reach the SSH ports published on the host; on the
# stack's network each guest answers on its qemu container's port 2222.
cmd_ssh() {
  local user n guest qemu host answered
  docker network connect "${STACK_NAME}_fleet" "$(cat /etc/hostname)"
  # The image says which account it was baked with.
  user=$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1])).get("username") or "ubuntu")' \
    "${KEY_DIR}/vm-info.json" 2>/dev/null || echo ubuntu)
  mkdir -p "$work"
  : > "$SSH_CONFIG"
  n=0
  for guest in "${GUESTS[@]}"; do
    n=$((n + 1))
    qemu=$(docker ps -q \
      --filter "label=com.docker.compose.project=${STACK_NAME}" \
      --filter "label=com.docker.compose.service=qemu-${n}")
    [ -n "$qemu" ] || die "the qemu-${n} container is not running"
    host=$(docker inspect \
      -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' "$qemu")
    [ -n "$host" ] || die "${guest}'s container has no address on the stack network"
    echo "${guest}: ${user}@${host}:2222"
    cat >> "$SSH_CONFIG" <<EOF
Host ${guest}
  HostName ${host}
  Port 2222
  User ${user}
  IdentityFile ${KEY_DIR}/id_rsa
  IdentitiesOnly yes
  BatchMode yes
  StrictHostKeyChecking no
  UserKnownHostsFile /dev/null
  LogLevel ERROR
  ConnectTimeout 10
  ServerAliveInterval 15
EOF
    env_set "hosts.${guest}.ssh_config" "$SSH_CONFIG"
  done

  for guest in "${GUESTS[@]}"; do
    answered=false
    for _ in $(seq 1 120); do
      if ssh -F "$SSH_CONFIG" "$guest" true 2>/dev/null; then
        answered=true
        break
      fi
      sleep 5
    done
    if [ "$answered" != true ]; then
      ssh -v -F "$SSH_CONFIG" "$guest" true 2>&1 | tail -20 || true
      die "${guest} never answered SSH"
    fi
    echo "${guest} answered SSH"
  done
}

# --- the guests -------------------------------------------------------------

cmd_rocm() {
  local guest
  for guest in "${GUESTS[@]}"; do
    guest_run "$guest" "${guest}-rocm.txt" rocm "${GUEST_ROCM:?}"
  done
}

# All the guests' GPUs are served from one image, so the firmware files are
# generated once.
gpu_firmware() {
  local image="" served n fw="${RUNNER_TEMP}/rocjitsu-firmware"
  for n in $(seq "${#GUESTS[@]}"); do
    served=$(docker ps \
      --filter "label=com.docker.compose.project=${STACK_NAME}" \
      --filter "label=com.docker.compose.service=rocjitsu-${n}" \
      --format '{{.Image}}') || return 1
    if [ -z "$served" ]; then
      echo "the rocjitsu-${n} container is not running, so guest ${n} has no emulated GPU being served"
      return 1
    fi
    if [ -n "$image" ] && [ "$served" != "$image" ]; then
      echo "the guests' GPUs are served from different images (${image} and ${served})"
      return 1
    fi
    image=$served
  done
  echo "generating firmware with ${image}"
  mkdir -p "$fw" || return 1
  # Streamed as a tar: a bind mount would be resolved by the host daemon.
  docker run --rm --entrypoint /bin/sh "$image" -c \
    'python3 /usr/local/bin/vfio_guest_firmware.py --generation gfx1250 --set gap --output /out && tar -cf - -C /out .' \
    | tar -xf - -C "$fw" || return 1
  ls -l "$fw"
  test -s "$fw/ip_discovery.bin" && test -s "$fw/gc_12_1_0_imu.bin"
}

gpu_load() {
  local guest=$1
  ssh -F "$SSH_CONFIG" "$guest" 'rm -rf /tmp/rocjitsu-firmware && mkdir -p /tmp/rocjitsu-firmware' || return 1
  scp -F "$SSH_CONFIG" "${RUNNER_TEMP}/rocjitsu-firmware"/*.bin "${guest}:/tmp/rocjitsu-firmware/" || return 1
  guest_run "$guest" "${guest}-gpu.txt" gpu
}

cmd_gpu() {
  local problem="" guest limit
  if ! gpu_firmware; then
    problem="the GPU firmware could not be generated"
  else
    for guest in "${GUESTS[@]}"; do
      echo "::group::${guest}"
      if ! gpu_load "$guest"; then
        problem="${problem:+${problem}; }amdgpu did not bind the emulated GPU in ${guest}"
      fi
      echo "::endgroup::"
    done
  fi
  if [ -n "$problem" ]; then
    withhold gpu "$problem"
    return 0
  fi
  limit=$(guest_value "${GUESTS[0]}-gpu.txt" GPU_VRAM_LIMIT)
  if [[ "$limit" =~ ^[0-9]+$ ]]; then meta_set_int gpu_vram_limit "$limit"; fi
}

# The guest's kernel allows peer-to-peer DMA across a host bridge only on an
# AMD CPU of family 23 (Zen) or later, and a transfer between GPU memory and
# the NIC or an NVMe drive is one. This records what each guest shows, and
# 'amd_bench_ci.py guest-cpu' states it and warns. No case is skipped and
# nothing is withheld for it.
cmd_facts() {
  set +e
  local guest
  mkdir -p "$BENCH_DIAG"
  {
    # The first processor: in the job container that is the runner's.
    sed '/^$/q' /proc/cpuinfo
    echo "processors: $(grep -c '^processor' /proc/cpuinfo)"
    lscpu 2>&1
  } > "${BENCH_DIAG}/runner-cpu.txt"
  for guest in "${GUESTS[@]}"; do
    echo "::group::${guest}"
    # Before any case: a guest that logs every refused request has lost its
    # boot messages by the end of the lane.
    ssh -F "$SSH_CONFIG" "$guest" 'sudo -n dmesg' > "${BENCH_DIAG}/${guest}-dmesg-start.txt" 2>&1
    guest_run "$guest" "${guest}-facts.txt" facts
    echo "::endgroup::"
    python3 "${scripts}/amd_bench_ci.py" guest-cpu --lane "$LANE" --guest "$guest" \
      --facts "${BENCH_DIAG}/${guest}-facts.txt" --notes "${BENCH_DIAG}/notes.txt"
  done
  return 0
}

# The device is the guest's only once a second ssh session has opened it for
# writing, closed it, and still owns it. Anything less withholds 'blkdev'.
raw_device() {
  local log=$1 raw seed access=""
  raw=$(guest_value "$log" RAW_DEV)
  seed=$(guest_value "$log" BLKDEV_SEED)
  if [[ "$raw" =~ ^/dev/nvme[0-9]+n[0-9]+$ ]]; then
    echo "$raw" > "${work}/raw-dev"
    if [ "$seed" = ok ]; then
      guest_run guest blkdev-access.txt blkdev-access "$raw" || true
      access=$(guest_value blkdev-access.txt BLKDEV_ACCESS)
    fi
  fi
  if [ "$access" = ok ]; then
    env_set vars.gusli_blkdev "$raw"
    echo "the raw device is ${raw}"
  else
    withhold blkdev "the raw block device is not usable by the guest account (device '${raw:-none}', seed '${seed:-not run}', access '${access:-not checked}')"
    env_set vars.gusli_blkdev /nonexistent/amd-bench
  fi
}

# usage: bring_nic GUEST MAC ADDRESS; leaves the interface name in IFACE
bring_nic() {
  local guest=$1
  guest_run "$guest" "${guest}-nic.txt" nic "$2" "$3" \
    || die "${guest} has no usable emulated RDMA NIC, so the lane cannot run"
  IFACE=$(guest_value "${guest}-nic.txt" NIC_IFACE)
  [[ "$IFACE" =~ ^[A-Za-z0-9_.-]+$ ]] \
    || die "${guest} did not name its emulated RDMA NIC, so the lane cannot run"
}

# usage: name_rdma GUEST IFACE; succeeds when the device is ernic0 and ACTIVE
name_rdma() {
  guest_run "$1" "$1-rdma.txt" rdma "$2" || true
  [ "$(guest_value "$1-rdma.txt" RDMA)" = ok ]
}

record_mtu() {
  local mtu
  mtu=$(guest_value "$1-rdma.txt" IB_MTU)
  if [[ "$mtu" =~ ^[0-9]+$ ]]; then
    meta_set_int ib_mtu "$mtu"
  else
    echo "no active MTU was read from the RDMA device, so the run-meta has no ib_mtu" \
      | tee -a "${BENCH_DIAG}/notes.txt"
  fi
}

prepare_local() {
  guest_run guest disks-local.txt disks-local \
    || die "the guest's spare NVMe drives could not be prepared"
  raw_device disks-local.txt
}

# The guests reach each other only through the Ethernet side of their
# emulated RDMA NICs.
prepare_net() {
  local iface1 iface2 rdma=true
  bring_nic guest1 "$ERNIC_MAC_PREFIX" "${ERNIC_SUBNET}.11"
  iface1=$IFACE
  bring_nic guest2 "$ERNIC_MAC_PREFIX" "${ERNIC_SUBNET}.12"
  iface2=$IFACE
  guest_run guest1 guest1-reach.txt reach "${ERNIC_SUBNET}.12" \
    || die "guest1 cannot reach guest2; nothing can be transferred between the guests"
  guest_run guest2 guest2-reach.txt reach "${ERNIC_SUBNET}.11" \
    || die "guest2 cannot reach guest1; nothing can be transferred between the guests"
  # Before any case: a rename under a process that holds the device is not
  # tried.
  name_rdma guest1 "$iface1" || rdma=false
  name_rdma guest2 "$iface2" || rdma=false
  if [ "$rdma" = true ]; then
    # The netdev carries tcp and the verbs port carries rc and ud.
    env_set hosts.guest1.vars.ucx_net_devices "${iface1},ernic0:1"
    env_set hosts.guest2.vars.ucx_net_devices "${iface2},ernic0:1"
    record_mtu guest1
  else
    withhold rdma "the RDMA device is not ernic0 and ACTIVE in both guests"
    env_set hosts.guest1.vars.ucx_net_devices "$iface1"
    env_set hosts.guest2.vars.ucx_net_devices "$iface2"
  fi
}

prepare_loop() {
  bring_nic guest "$ERNIC_MAC" "${ERNIC_SUBNET}.11"
  if name_rdma guest "$IFACE"; then
    record_mtu guest
    # For the diagnostics only: it says whether the backend carries anything.
    guest_run guest pingpong.txt pingpong ernic0 "${ERNIC_SUBNET}.11" || true
  else
    withhold rdma "the guest's RDMA device is not ernic0 and ACTIVE"
  fi
}

prepare_fabric() {
  bring_nic guest "$ERNIC_MAC" "${ERNIC_SUBNET}.11"
  guest_run guest nvmeof.txt nvmeof "$IFACE" "$ERNIC_PEER_IP" "$NVMEOF_NQN" \
    "$NVMEOF_NEIGH_MAC" "$NVMEOF_FS" "$NVMEOF_RAW" "$NVMEOF_KV_NSID" \
    || die "the NVMe-oF namespaces could not be prepared"
  raw_device nvmeof.txt
}

cmd_prepare() {
  case "$LANE" in
    vm1-local) prepare_local ;;
    vm2-net) prepare_net ;;
    vm1-loop) prepare_loop ;;
    vm1-fabric) prepare_fabric ;;
    *) die "the lane ${LANE} has no guest to prepare" ;;
  esac
}

cmd_install() {
  local guest
  [[ "${PYTHON_VERSION:?}" =~ ^3\.[0-9]+$ ]] || die "python-version must be 3.Y"
  [[ "${UV_VERSION:?}" =~ ^[0-9]+(\.[0-9]+)*$ ]] || die "UV_VERSION is not a version"
  for guest in "${GUESTS[@]}"; do
    echo "::group::${guest}"
    ssh -F "$SSH_CONFIG" "$guest" 'mkdir -p amd-bench/wheels'
    scp -F "$SSH_CONFIG" "${NIXL_WHEEL:?}" "${NIXLBENCH_WHEEL:?}" "${guest}:amd-bench/wheels/"
    guest_run "$guest" "${guest}-install.txt" install "$PYTHON_VERSION" "$UV_VERSION"
    echo "::endgroup::"
  done
}

cmd_preflight() {
  local guest rc
  for guest in "${GUESTS[@]}"; do
    echo "::group::${guest}"
    rc=0
    guest_run "$guest" "${guest}-preflight.txt" preflight amd-bench/venv "${GUEST_ROCM:?}" amd-bench/preflight || rc=$?
    scp -q -r -F "$SSH_CONFIG" "${guest}:amd-bench/preflight" "${BENCH_DIAG}/${guest}-preflight" || true
    echo "::endgroup::"
    [ "$rc" -eq 0 ] || die "nixlbench does not run in ${guest}"
  done
}

# --- the runner's files -----------------------------------------------------

cmd_render() {
  local line
  local -a env_args=() meta_args=()
  mkdir -p "$work"
  touch "${work}/env-set" "${work}/withhold" "${work}/meta-set" "${work}/meta-set-int"
  while IFS= read -r line; do env_args+=(--set "$line"); done < "${work}/env-set"
  while IFS= read -r line; do env_args+=(--withhold "$line"); done < <(sort -u "${work}/withhold")
  while IFS= read -r line; do meta_args+=(--set "$line"); done < "${work}/meta-set"
  while IFS= read -r line; do meta_args+=(--set-int "$line"); done < "${work}/meta-set-int"

  python3 "${scripts}/amd_bench_ci.py" env --matrix "${GITHUB_WORKSPACE}/.ci/bench" \
    --lane "$LANE" --out "${work}/env.json" "${env_args[@]}"
  python3 "${scripts}/amd_bench_ci.py" meta --base "${RUN_META:?}" \
    --out "${work}/run-meta.json" \
    --set "bench_run_id=${GITHUB_RUN_ID:?}" \
    --set-int "run_attempt=${GITHUB_RUN_ATTEMPT:?}" \
    --set "rocm_version=${ROCM_VERSION:?}" \
    "${meta_args[@]}"
  mkdir -p "$BENCH_DIAG"
  cp "${work}/env.json" "${work}/run-meta.json" "${BENCH_DIAG}/"
  python3 "${scripts}/amd_bench_run.py" validate --matrix "${GITHUB_WORKSPACE}/.ci/bench" \
    --lane "$LANE" --env "${work}/env.json"
}

# --- after the runner -------------------------------------------------------

cmd_diagnostics() {
  set +e
  local out=$BENCH_DIAG guest n c p2p raw=""
  mkdir -p "$out"
  if [ -s "${work}/raw-dev" ]; then raw=$(cat "${work}/raw-dev"); fi
  if [ -s "$SSH_CONFIG" ]; then
    for guest in "${GUESTS[@]}"; do
      ssh -F "$SSH_CONFIG" "$guest" 'sudo -n dmesg' > "${out}/${guest}-dmesg.txt" 2>&1
      # Counted, without the timestamps: a refusal may be logged for every
      # request, and by then the log holds nothing else.
      p2p=$(grep -ci p2p "${out}/${guest}-dmesg.txt")
      {
        echo "${p2p:-0} line(s) of ${guest}-dmesg.txt mention p2p; the most frequent:"
        grep -i p2p "${out}/${guest}-dmesg.txt" | sed 's/^\[[^]]*\] *//' \
          | sort | uniq -c | sort -rn | head -n 40
      } > "${out}/${guest}-dmesg-p2p.txt"
      if [ "${p2p:-0}" -gt 0 ]; then
        echo "::notice title=AMD bench ${LANE}::the kernel log of ${guest} has ${p2p} line(s) about peer-to-peer DMA at the end of the lane; see ${guest}-dmesg-p2p.txt in the diagnostics"
      fi
      if [[ "$raw" =~ ^/dev/nvme[0-9]+n[0-9]+$ ]]; then
        ssh -F "$SSH_CONFIG" "$guest" bash -s -- state "$raw" \
          < "${scripts}/amd_bench_guest.sh" > "${out}/${guest}-state.txt" 2>&1
      else
        ssh -F "$SSH_CONFIG" "$guest" bash -s -- state \
          < "${scripts}/amd_bench_guest.sh" > "${out}/${guest}-state.txt" 2>&1
      fi
      scp -q -F "$SSH_CONFIG" "${guest}:amd-bench/pip-list.txt" "${out}/${guest}-pip-list.txt"
    done
  fi
  n=0
  for guest in "${GUESTS[@]}"; do
    n=$((n + 1))
    c=$(container_of "ernic-${n}")
    if [ -n "$c" ]; then
      docker exec "$c" cat "/run/ernic-stats/ernic-${n}.stats" > "${out}/ernic-${n}-stats.txt" 2>&1
      docker logs "$c" > "${out}/ernic-${n}.log" 2>&1
      docker stats --no-stream --format 'memory {{.MemUsage}}, CPU {{.CPUPerc}}' "$c" \
        > "${out}/ernic-${n}-usage.txt" 2>&1
    fi
  done
  if [ -f "${STACK_DIR}/docker-compose.yml" ]; then
    cp "${STACK_DIR}/docker-compose.yml" "$out/"
    cp "${STACK_DIR}/docker-compose.override.yml" "$out/" 2>/dev/null
    if [ ! -s "${out}/compose-config.json" ]; then
      qemu-tool compose --stack-dir "$STACK_DIR" config --format json \
        > "${out}/compose-config.json" 2> "${out}/compose-config.err"
    fi
    qemu-tool compose --stack-dir "$STACK_DIR" logs --no-color --tail 2000 \
      > "${out}/compose-logs.txt" 2>&1
    # Two guests of four vCPUs oversubscribe a hosted runner, and the NICs'
    # mesh manager drops a node that misses its heartbeat.
    if grep -qi 'failed health check' "${out}/compose-logs.txt"; then
      echo "::warning title=AMD bench ${LANE}::the emulated NICs' mesh manager evicted a node during the run; an RDMA failure in this run may be that and not the wheel"
    fi
  fi
  cp "${work}/env.json" "${work}/run-meta.json" "${work}/withhold" "$out/" 2>/dev/null
  runner_state "at the end"
  ls -l "$out"
  return 0
}

cmd_fabric_down() {
  mkdir -p "$BENCH_DIAG"
  if [ -s "$SSH_CONFIG" ]; then
    guest_run guest nvmeof-down.txt nvmeof-down "$NVMEOF_NQN" || true
  fi
}

cmd_down() {
  local left
  cd "${GITHUB_WORKSPACE}/qemu-minimal" || return 0
  qemu-tool quick-start down \
    --xml qemu/stacks/rocm-golden.xml \
    --xml "${GITHUB_WORKSPACE}/${OVERLAY}" \
    --stack-dir "$STACK_DIR" \
    --yes || true
  left=$(docker ps -aq --filter "label=com.docker.compose.project=${STACK_NAME}" || true)
  if [ -n "$left" ]; then
    echo "::warning::removing containers the teardown left behind"
    # shellcheck disable=SC2086
    docker rm -f $left || true
  fi
}

case "${1:-}" in
  pins) cmd_pins ;;
  override) cmd_override ;;
  ernic-image) cmd_ernic_image ;;
  up) cmd_up ;;
  compose-merge) cmd_compose_merge ;;
  backend-check) cmd_backend_check ;;
  ssh) cmd_ssh ;;
  rocm) cmd_rocm ;;
  gpu) cmd_gpu ;;
  facts) cmd_facts ;;
  prepare) cmd_prepare ;;
  install) cmd_install ;;
  preflight) cmd_preflight ;;
  render) cmd_render ;;
  diagnostics) cmd_diagnostics ;;
  fabric-down) cmd_fabric_down ;;
  down) cmd_down ;;
  *)
    echo "amd_bench_stack.sh: unknown subcommand; they are listed at the top of the script" >&2
    exit 2
    ;;
esac
