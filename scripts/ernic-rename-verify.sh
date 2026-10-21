#!/bin/sh
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Prove that the ERNIC RDMA device has a name UCX and libfabric can bind to,
# and that both of them actually bind to it.
#
# Both failure modes this checks for are SILENT -- UCX falls back to TCP and
# libfabric exports an unbound 0.0.0.0 endpoint, and in each case the
# benchmark still runs and still prints numbers.  So "the benchmark worked"
# proves nothing here; these are the checks that do.  See
# udev/99-zz-nixl-ernic-name.rules for the two upstream bugs in full.
#
# Run in the guest, or in the benchmark container (which is where it matters,
# since that is where nixlbench runs):
#   sh scripts/ernic-rename-verify.sh
#
# Exit 0 = both transports can bind.  Exit 1 = at least one cannot, and the
# output says which and why.  Exit 2 = no ERNIC device found at all.
#
# POSIX sh and no bashisms on purpose: it has to run on a minimal guest image.

ERNIC_VENDOR=0x1dd8
ERNIC_DEVICE=0x1002
ERNIC_SUBSYS_VENDOR=0x1dd8
ERNIC_SUBSYS_DEVICE=0x5400
SYSFS_ROOT=${ERNIC_SYSFS_ROOT:-/sys}

# UCT_MD_NAME_MAX is 16 in src/uct/api/uct_def.h and that INCLUDES the NUL.
UCX_NAME_MAX=15

rc=0
note()  { printf '  %s\n' "$*"; }
pass()  { printf 'PASS  %s\n' "$*"; }
fail()  { printf 'FAIL  %s\n' "$*"; rc=1; }
warn()  { printf 'WARN  %s\n' "$*"; }

# --- 1. Find the device by PCI tuple, never by name ------------------------
# The name is the thing under test, so matching on it would be circular (and
# rocm-ernic's own find-rdma-device.sh warns that name patterns go stale).
dev=
for vendor_attr in "$SYSFS_ROOT"/class/infiniband/*/device/vendor; do
    [ -r "$vendor_attr" ] || continue
    device_dir=${vendor_attr%/vendor}
    read -r v < "$vendor_attr" || continue
    [ "$v" = "$ERNIC_VENDOR" ] || continue
    read -r d < "$device_dir/device" 2>/dev/null || continue
    [ "$d" = "$ERNIC_DEVICE" ] || continue
    read -r sv < "$device_dir/subsystem_vendor" 2>/dev/null || continue
    [ "$sv" = "$ERNIC_SUBSYS_VENDOR" ] || continue
    read -r sd < "$device_dir/subsystem_device" 2>/dev/null || continue
    [ "$sd" = "$ERNIC_SUBSYS_DEVICE" ] || continue
    ibdev_dir=${device_dir%/device}
    dev=${ibdev_dir##*/}
    break
done

if [ -z "$dev" ]; then
    printf 'No emulated ERNIC RDMA device (%s:%s subsystem %s:%s) in %s/class/infiniband.\n' \
        "$ERNIC_VENDOR" "$ERNIC_DEVICE" "$ERNIC_SUBSYS_VENDOR" "$ERNIC_SUBSYS_DEVICE" \
        "$SYSFS_ROOT"
    note "In a container this usually means the rdma-core in THIS image has no"
    note "providers/ionic -- it needs rdma-core >= 61 (Ubuntu 26.04 ships 61.0;"
    note "24.04 ships 50.0 and cannot see this device at all)."
    exit 2
fi
printf 'ERNIC RDMA device: %s\n\n' "$dev"

# --- 2. The name itself ----------------------------------------------------
len=${#dev}
case $dev in
    *-*) fail "name '$dev' contains a hyphen"
         note "libfabric vrb_cmp_domain_and_dev_name() cuts the domain name at the"
         note "LAST hyphen, so the verbs domain will not match its own device."
         note "src_addr ends up NULL and the endpoint binds 0.0.0.0 -- the run"
         note "silently goes over whatever NIC the routing table picks." ;;
    *)   pass "name has no hyphen (libfabric domain/device compare is safe)" ;;
esac

if [ "$len" -gt "$UCX_NAME_MAX" ]; then
    fail "name '$dev' is $len chars; UCX truncates at $UCX_NAME_MAX"
    note "ucs_snprintf_zero() stores only the first $UCX_NAME_MAX chars, the exact"
    note "strcmp in uct_ib_get_device_by_name() then fails, and UCX drops the"
    note "memory domain and falls back to TCP without an error."
else
    pass "name is $len chars (<= $UCX_NAME_MAX, UCX will not truncate)"
fi

# --- 3. Does libfabric actually bind it? -----------------------------------
# The decisive check is src_addr. A verbs MSG domain whose name failed to
# match its device still appears in fi_info output -- it just has no source
# address, which is exactly the silent failure.
printf '\n--- libfabric ---\n'
if command -v fi_info >/dev/null 2>&1; then
    # -t/--ep_type, NOT -e: in libfabric 2.x -e is --env and prints the
    # environment-variable help instead of any endpoint info, so `-e msg`
    # silently reports no src_addr for a perfectly healthy device.
    out=$(fi_info -p verbs -d "$dev" -t FI_EP_MSG -v 2>&1)
    if [ -z "$out" ] || printf '%s' "$out" | grep -qi "fi_getinfo:.*-61\|No data available"; then
        fail "fi_info -p verbs -d $dev -t FI_EP_MSG returned nothing"
        note "If the name is clean, suspect that rocm-ernic0 has no IP: without"
        note "one, vrb_device_has_ipoib_addr() drops the device from FI_EP_MSG"
        note "entirely and rxm/RDM has nothing to layer on."
    elif printf '%s' "$out" | grep -q "src_addr"; then
        pass "verbs MSG domain has src_addr (device matched its domain)"
        printf '%s' "$out" | grep -m2 "src_addr\|domain:" | sed 's/^/      /'
    else
        fail "verbs MSG domain for $dev has NO src_addr"
        note "This is the hyphen bug firing: the endpoint will be exported as"
        note "0.0.0.0 and the peer will choose a source device by routing table."
    fi

    # NIXL asks for FI_EP_RDM, which only exists over verbs via ofi_rxm, and it
    # keys its provider map on the FULL prov_name string.
    if fi_info -p "verbs;ofi_rxm" -d "$dev" 2>/dev/null | grep -q .; then
        pass "verbs;ofi_rxm offers FI_EP_RDM on $dev (what NIXL asks for)"
    else
        warn "no verbs;ofi_rxm RDM endpoint on $dev -- NIXL's LIBFABRIC backend needs this"
    fi
else
    warn "fi_info not installed; skipping libfabric checks (apt install libfabric-bin)"
fi

# --- 4. Does UCX actually open it? -----------------------------------------
# Note: ucx_info -d prints the device name inside its OWN failure message
# ("failed to open memory domain <truncated-name> for component ib"), so
# grepping for the name alone is a false pass. Check for the failure first.
printf '\n--- UCX ---\n'
if command -v ucx_info >/dev/null 2>&1; then
    uout=$(ucx_info -d 2>&1)
    if printf '%s' "$uout" | grep -q "failed to open memory domain"; then
        fail "UCX could not open a memory domain"
        printf '%s' "$uout" | grep -m2 "failed to open memory domain" | sed 's/^/      /'
        note "A name one character short of the real one is the truncation fingerprint."
    elif printf '%s' "$uout" | grep -q "Memory domain: *$dev\$\|Transport devices.*$dev"; then
        pass "UCX opened memory domain $dev"
        printf '%s' "$uout" | grep -m3 "$dev" | sed 's/^/      /'
    elif printf '%s' "$uout" | grep -q "$dev"; then
        pass "UCX sees $dev"
        printf '%s' "$uout" | grep -m3 "$dev" | sed 's/^/      /'
    else
        fail "UCX does not list $dev at all"
        note "Check that an IB transport is actually enabled -- this repo's UCX_TLS"
        note "settings in Makefile / nixl-ucx-matrix.sh contain no IB transport, and"
        note "with those set UCX reports no error, just no device."
    fi
else
    warn "ucx_info not installed; skipping UCX checks"
fi

printf '\n'
if [ "$rc" -eq 0 ]; then
    printf 'OK: %s is bindable by both transports.\n' "$dev"
else
    printf 'PROBLEM: see FAIL lines above. Install udev/99-zz-nixl-ernic-name.rules\n'
    printf 'in the guest, or rename now with:\n'
    printf '    rdma dev set %s name rocm_ernic0\n' "$dev"
    printf 'Note the rename reverts on the next udev add event (rocm-ernic re-applies\n'
    printf 'its own name), so the rules file is the durable form.\n'
fi
exit "$rc"
