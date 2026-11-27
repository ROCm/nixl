#!/bin/bash

# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

NIXL_WHEEL_DIR=""
NIXLBENCH_WHEEL_DIR=""
INSTALLED=""
WITH_UCX="false"

while [[ $# -gt 0 ]]; do
    case $1 in
        --nixl-wheel-dir)
            NIXL_WHEEL_DIR=$2
            shift
            shift
            ;;
        --nixlbench-wheel-dir)
            NIXLBENCH_WHEEL_DIR=$2
            shift
            shift
            ;;
        --installed)
            INSTALLED=$2
            shift
            shift
            ;;
        --with-ucx)
            WITH_UCX="true"
            shift
            ;;
        --help)
            echo "Usage: $0 --nixl-wheel-dir <dir> --nixlbench-wheel-dir <dir> [--with-ucx]"
            echo "       $0 --installed <path to nixlbench> [--with-ucx]"
            echo "  --nixl-wheel-dir: Directory of the NIXL wheels (nixl_<variant>-*.whl, the merged nixl_rocm-*.whl included); the nixlbench wheel is installed and run against each of them"
            echo "  --nixlbench-wheel-dir: Directory holding the one nixlbench wheel to test (nixlbench_<variant>-*.whl, the merged nixlbench_rocm-*.whl included)"
            echo "  --installed: Test this installed nixlbench command instead of installing wheels"
            echo "  --with-ucx: Also run a UCX transfer between two local nixlbench processes"
            echo "  --help: Show this help message"
            echo ""
            echo "Runs nixlbench without LD_LIBRARY_PATH and NIXL_PLUGIN_DIR: the wheels"
            echo "and the system (ROCm, OpenSSL 3) must provide every library."
            exit 0
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

if [ -n "$INSTALLED" ]; then
    if [ -n "$NIXL_WHEEL_DIR" ] || [ -n "$NIXLBENCH_WHEEL_DIR" ]; then
        echo "ERROR: --installed cannot be used with --nixl-wheel-dir or --nixlbench-wheel-dir" >&2
        exit 1
    fi
    if [ ! -x "$INSTALLED" ]; then
        echo "ERROR: --installed: $INSTALLED is not an executable" >&2
        exit 1
    fi
elif [ -z "$NIXL_WHEEL_DIR" ] || [ -z "$NIXLBENCH_WHEEL_DIR" ]; then
    echo "ERROR: pass --nixl-wheel-dir and --nixlbench-wheel-dir, or --installed" >&2
    exit 1
fi

set -e

WORK_DIR=$(mktemp -d)
trap 'rm -rf "$WORK_DIR"' EXIT

fail() {
    echo "ERROR: $*" >&2
    exit 1
}

# Nothing but the wheels and the system may provide a library or a plugin.
clean_env() {
    env -u LD_LIBRARY_PATH -u NIXL_PLUGIN_DIR "$@"
}

# The interpreter for a cpXY tag: the manylinux image's, else python3.Y on PATH.
python_for_tag() {
    local tag=$1 minor=${1#cp3}
    if [ -x "/opt/python/$tag-$tag/bin/python" ]; then
        echo "/opt/python/$tag-$tag/bin/python"
    else
        command -v "python3.$minor" || true
    fi
}

# The nixlbench distribution's single requirement must name an installed
# distribution at exactly that version. Not "pip check": NIXL's own
# dependencies are not installed.
check_requirement() {
    "$1" - "$2" <<'EOF'
import importlib.metadata as metadata
import re
import sys

name = sys.argv[1]
requirements = metadata.distribution(name).requires or []
if len(requirements) != 1:
    sys.exit(f"ERROR: {name} must have one requirement, found {requirements}")
match = re.fullmatch(
    r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)\s*==\s*([^\s;,]+)\s*", requirements[0]
)
if match is None:
    sys.exit(f"ERROR: {name} requires {requirements[0]!r}, not an exact version")
try:
    installed = metadata.version(match.group(1))
except metadata.PackageNotFoundError:
    sys.exit(f"ERROR: {name} requires {requirements[0]}, which is not installed")
if installed != match.group(2):
    sys.exit(f"ERROR: {name} requires {requirements[0]}, installed is {installed}")
print(f"{name} requires {requirements[0]}: installed")
EOF
}

NIXLBENCH="$INSTALLED"
if [ -z "$INSTALLED" ]; then
    shopt -s nullglob
    NIXLBENCH_WHEELS=("$NIXLBENCH_WHEEL_DIR"/nixlbench_*.whl)
    NIXL_WHEELS=("$NIXL_WHEEL_DIR"/nixl_*.whl)
    shopt -u nullglob
    if [ ${#NIXLBENCH_WHEELS[@]} -ne 1 ]; then
        fail "expected 1 nixlbench wheel in $NIXLBENCH_WHEEL_DIR, got ${#NIXLBENCH_WHEELS[@]}: ${NIXLBENCH_WHEELS[*]}"
    fi
    NIXLBENCH_WHEEL=${NIXLBENCH_WHEELS[0]}
    NIXLBENCH_DIST=$(basename "$NIXLBENCH_WHEEL")
    NIXLBENCH_DIST=${NIXLBENCH_DIST%%-*}

    # Every NIXL wheel is a separate build of libnixl: bind every symbol
    # nixlbench uses against each of them.
    NEWEST_MINOR=-1
    for NIXL_WHEEL in "${NIXL_WHEELS[@]}"; do
        if ! [[ "$(basename "$NIXL_WHEEL")" =~ -(cp3([0-9]+))-cp3[0-9]+t?- ]]; then
            continue
        fi
        TAG=${BASH_REMATCH[1]}
        MINOR=${BASH_REMATCH[2]}
        PYTHON=$(python_for_tag "$TAG")
        if [ -z "$PYTHON" ]; then
            echo "=== Skipping $(basename "$NIXL_WHEEL"): no Python 3.$MINOR interpreter ==="
            continue
        fi
        echo "=== $(basename "$NIXLBENCH_WHEEL") with $(basename "$NIXL_WHEEL") ==="
        VENV="$WORK_DIR/venv-$TAG"
        "$PYTHON" -m venv "$VENV"
        "$VENV/bin/python" -m pip install --quiet --disable-pip-version-check \
            --no-index --no-deps "$NIXL_WHEEL" "$NIXLBENCH_WHEEL"
        check_requirement "$VENV/bin/python" "$NIXLBENCH_DIST"
        clean_env LD_BIND_NOW=1 "$VENV/bin/nixlbench" --version
        if [ "$MINOR" -gt "$NEWEST_MINOR" ]; then
            NEWEST_MINOR=$MINOR
            NIXLBENCH="$VENV/bin/nixlbench"
        fi
    done
    if [ -z "$NIXLBENCH" ]; then
        fail "no NIXL wheel in $NIXL_WHEEL_DIR has a Python interpreter to test with"
    fi
    VENV=$(dirname "$(dirname "$NIXLBENCH")")

    echo "=== Libraries come from the NIXL wheel and from the ROCm install ==="
    SITE_PACKAGES=$("$VENV/bin/python" -c 'import sysconfig; print(sysconfig.get_path("platlib"))')
    clean_env LD_DEBUG=libs NIXLBENCH_WHEEL_DEBUG=1 "$NIXLBENCH" --version > /dev/null 2> "$WORK_DIR/libs.log"
    LIBNIXL=$(sed -n 's/.*calling init: \(.*\/libnixl\.so\)$/\1/p' "$WORK_DIR/libs.log" | head -n 1)
    if [ -z "$LIBNIXL" ]; then
        fail "nixlbench did not load libnixl.so"
    fi
    case "$(readlink -f "$LIBNIXL")" in
        "$(readlink -f "$SITE_PACKAGES")"/.nixl_*.mesonpy.libs/libnixl.so)
            echo "libnixl.so: $LIBNIXL"
            ;;
        *)
            fail "libnixl.so was loaded from $LIBNIXL, not from the NIXL wheel in $SITE_PACKAGES"
            ;;
    esac
    ROCM_LIB_DIR=$(sed -n 's/^nixlbench: ROCm: .*, libraries //p' "$WORK_DIR/libs.log")
    if [[ "$NIXLBENCH_DIST" == *rocm* ]]; then
        if [ -z "$ROCM_LIB_DIR" ]; then
            fail "the launcher found no ROCm install: $(grep '^nixlbench: ' "$WORK_DIR/libs.log")"
        fi
        LIBAMDHIP64=$(sed -n 's/.*calling init: \(.*\/libamdhip64\.so[^/]*\)$/\1/p' "$WORK_DIR/libs.log" | head -n 1)
        if [ -z "$LIBAMDHIP64" ]; then
            fail "nixlbench did not load libamdhip64.so"
        fi
        if [ "$(readlink -f "$(dirname "$LIBAMDHIP64")")" != "$(readlink -f "$ROCM_LIB_DIR")" ]; then
            fail "libamdhip64 was loaded from $LIBAMDHIP64, not from the ROCm install the launcher selected ($ROCM_LIB_DIR)"
        fi
        echo "libamdhip64: $LIBAMDHIP64"
    fi
fi

echo "=== An empty environment ==="
# As after sudo or "env -i". ROCM_PATH is kept: where set, it is what names the
# ROCm install.
env -i ${ROCM_PATH:+ROCM_PATH="$ROCM_PATH"} "$NIXLBENCH" --version

echo "=== nixlbench --help ==="
# gflags exits 1 after printing the help.
clean_env "$NIXLBENCH" --help > "$WORK_DIR/help.log" 2>&1 || true
if ! grep -q "NIXL Benchmark" "$WORK_DIR/help.log"; then
    cat "$WORK_DIR/help.log" >&2
    fail "nixlbench --help did not print its usage"
fi

RUN_ARGS=(
    --check_consistency
    --total_buffer_size 80000000
    --start_block_size 16384 --max_block_size 16384
    --start_batch_size 4 --max_batch_size 4
    --num_iter 100 --warmup_iter 10
)

# A directory each: READ takes an existing file to hold what nixlbench itself
# would have put there, which is not what WRITE leaves.
for OP in WRITE READ; do
    echo "=== POSIX $OP ==="
    mkdir "$WORK_DIR/posix-$OP"
    clean_env "$NIXLBENCH" --backend POSIX --filepath "$WORK_DIR/posix-$OP" --op_type "$OP" "${RUN_ARGS[@]}"
done

if [ "$WITH_UCX" = "true" ]; then
    echo "=== UCX between two local processes ==="
    UCX_ARGS=(
        --backend UCX --runtime_type ASIO --asio_address 127.0.0.1
        --initiator_seg_type DRAM --target_seg_type DRAM
        "${RUN_ARGS[@]}"
    )
    clean_env timeout 120 "$NIXLBENCH" "${UCX_ARGS[@]}" > "$WORK_DIR/ucx-first.log" 2>&1 &
    FIRST_PID=$!
    # The first process to bind the ASIO port waits for the second to connect.
    sleep 3
    SECOND_RC=0
    clean_env timeout 120 "$NIXLBENCH" "${UCX_ARGS[@]}" > "$WORK_DIR/ucx-second.log" 2>&1 || SECOND_RC=$?
    FIRST_RC=0
    wait "$FIRST_PID" || FIRST_RC=$?
    cat "$WORK_DIR/ucx-first.log"
    if [ "$FIRST_RC" -ne 0 ] || [ "$SECOND_RC" -ne 0 ]; then
        cat "$WORK_DIR/ucx-second.log" >&2
        fail "the UCX run failed (first process: $FIRST_RC, second process: $SECOND_RC)"
    fi
fi

echo "=== nixlbench wheel test passed ==="
