#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# In-container smoke test: both stacks load, NIXL has its plugins (including
# MORI_IO), nixlbench runs, and the GPUs/NICs are visible.
#
# Run by `make test` (needs /dev/kfd + /dev/dri) or `make test-nogpu`
# (SMOKE_GPU=0: everything that does not need a device).
set -uo pipefail

rc=0
_check() {
	local label="$1"
	shift
	if "$@" > /tmp/smoke.out 2>&1; then
		echo "PASS: ${label}"
		sed 's/^/      /' /tmp/smoke.out
	else
		echo "FAIL: ${label}"
		sed 's/^/      /' /tmp/smoke.out
		rc=1
	fi
}

echo "=== environment ==="
echo "  ROCM_PATH=${ROCM_PATH:-unset}"
echo "  NIXL_PLUGIN_DIR=${NIXL_PLUGIN_DIR:-unset}"
echo "  MORI_PREFIX=${MORI_PREFIX:-unset}"
hipconfig --version 2>/dev/null | sed 's/^/  hip: /' || true
echo

echo "=== NIXL ==="
# The plugin .so files are the ground truth for what backends this image can
# offer; the Python agent below can only be asked when torch is installed.
_check "nixl plugins present" bash -c '
found=$(ls "${NIXL_PLUGIN_DIR}"/libplugin_*.so 2>/dev/null | xargs -r -n1 basename | sed "s/^libplugin_//;s/\.so$//" | sort | tr "\n" " ")
echo "plugins: ${found}"
[ -n "${found}" ] || { echo "no plugins in ${NIXL_PLUGIN_DIR}"; exit 1; }
case " ${found} " in *" UCX "*) ;; *) echo "UCX plugin missing"; exit 1;; esac
case " ${found} " in
    *" MORI_IO "*) echo "MORI_IO backend present" ;;
    *) echo "note: MORI_IO backend not built (no patches/nixl/ MORI plugin?)" ;;
esac
'
# gflags exits 1 from --help by convention, so assert on the output instead of
# the status -- and on MORI_IO being listed, which proves the nixlbench patch
# is in and `--backend MORI_IO` will be accepted rather than rejected.
_check "nixlbench runs and lists MORI_IO" bash -c '
out=$(nixlbench --help 2>&1 || true)
[ -n "${out}" ] || { echo "nixlbench --help produced nothing"; exit 1; }
grep -q -- "-backend" <<< "${out}" || { echo "no -backend flag in help"; exit 1; }
if grep -q "MORI_IO" <<< "${out}"; then
    echo "nixlbench accepts --backend MORI_IO"
else
    echo "nixlbench help does not list MORI_IO (03-nixlbench patch missing?)"
    exit 1
fi
'

if python3 -c "import torch" 2> /dev/null; then
	_check "nixl python agent + plugin list" python3 -c '
from nixl._api import nixl_agent
a = nixl_agent("smoke")
plugins = a.get_plugin_list()
print("agent sees:", ", ".join(sorted(plugins)))
assert plugins, "no NIXL plugins loaded (check NIXL_PLUGIN_DIR / LD_LIBRARY_PATH)"
'
else
	echo "SKIP: nixl python agent (torch not installed; built with INSTALL_TORCH=0)"
fi
echo

echo "=== MORI ==="
_check "import mori" python3 -c \
	'import mori; print("mori:", mori.__version__, mori.__file__)'
_check "mori C++ SDK staged" bash -c '
test -e "${MORI_PREFIX:-/opt/mori}/lib/libmori_io.so" || { echo "libmori_io.so missing"; exit 1; }
echo "libmori_io.so present"
'

if [[ "${SMOKE_GPU:-1}" == "1" ]]; then
	echo
	echo "=== devices ==="
	_check "rocm devices visible" bash -c \
		'rocm_agent_enumerator | grep -E "^gfx" | sort -u'
	# `mori check` inspects GPUs, NICs, firmware and IBGDA readiness.  Advisory
	# here: NIC support varies by host, so its exit code is not the verdict.
	echo "--- mori check (advisory) ---"
	mori check 2>&1 | sed 's/^/      /' || true
fi

echo
if [[ "${rc}" -eq 0 ]]; then
	echo "smoke-test: PASS"
else
	echo "smoke-test: FAIL"
fi
exit "${rc}"
