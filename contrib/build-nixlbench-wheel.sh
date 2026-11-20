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

NIXL_PATH="/usr/local/nixl"
NIXL_WHEEL_DIR="dist"
ARCH=$(uname -m)
WHL_PLATFORM="manylinux_2_28_$ARCH"
OUTPUT_DIR="dist-nixlbench"
# Set by --rocm-path only: a ROCM_PATH in the environment must not select ROCm.
ROCM_PATH=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --nixl-path)
            NIXL_PATH=$2
            shift
            shift
            ;;
        --nixl-wheel-dir)
            NIXL_WHEEL_DIR=$2
            shift
            shift
            ;;
        --rocm-path)
            ROCM_PATH=$2
            shift
            shift
            ;;
        --platform)
            WHL_PLATFORM=$2
            shift
            shift
            ;;
        --output-dir)
            OUTPUT_DIR=$2
            shift
            shift
            ;;
        --help)
            echo "Usage: $0 --rocm-path <rocm-path> [--nixl-path <nixl-path>] [--nixl-wheel-dir <dir>] [--platform <platform>] [--output-dir <output-dir>]"
            echo "  --rocm-path: ROCm installation prefix, e.g. /opt/rocm; nixlbench is built with HIP (VRAM) support against it"
            echo "  --nixl-path: Prefix of the NIXL install to build against (default: $NIXL_PATH)"
            echo "  --nixl-wheel-dir: Directory of the NIXL backend wheels built from that install; the nixlbench wheel requires their distribution at their version (default: $NIXL_WHEEL_DIR)"
            echo "  --platform: Platform to build the wheel for (default: $WHL_PLATFORM)"
            echo "  --output-dir: Directory to output the wheel to (default: $OUTPUT_DIR)"
            echo "  --help: Show this help message"
            echo ""
            echo "Builds nixlbench against an installed NIXL and packages it as the wheel"
            echo "nixlbench_<variant>, which requires the NIXL backend wheel nixl_<variant>."
            echo "Must be executed from the root of the NIXL repository."
            exit 0
            ;;
        *)
            echo "Unknown argument: $1"
            exit 1
            ;;
    esac
done

if [ -z "$ROCM_PATH" ]; then
    echo "ERROR: only ROCm builds are supported so far; pass --rocm-path" >&2
    exit 1
fi

set -e

# What auditwheel may vendor, as "<file name prefix>|<SPDX expression>|<license
# files>", each license file as "<path in the wheel>=<source>". Anything else
# found in the wheel fails the check at the end.
VENDORED_LIBS=(
    "libgflags|BSD-3-Clause|gflags/COPYING.txt=/usr/share/licenses/gflags/COPYING.txt"
    # The libgomp rpm has no license files; it is built from the gcc sources
    # that libgcc's cover.
    "libgomp|GPL-3.0-or-later WITH GCC-exception-3.1|libgomp/COPYING3=/usr/share/licenses/libgcc/COPYING3 libgomp/COPYING.RUNTIME=/usr/share/licenses/libgcc/COPYING.RUNTIME"
)
# The header-only libraries compiled into nixlbench, as "<wrap>|<SPDX
# expression>|<license files>", each license file a name in the directory meson
# unpacks the wrap into.
HEADER_ONLY_LIBS=(
    "asio|BSL-1.0|COPYING LICENSE_1_0.txt"
    "tomlplusplus|MIT|LICENSE"
)
LICENSE_EXPRESSION="Apache-2.0"
LICENSE_ARGS=(--license-file LICENSE=LICENSE)
for ENTRY in "${HEADER_ONLY_LIBS[@]}"; do
    IFS='|' read -r WRAP SPDX LICENSE_FILES <<< "$ENTRY"
    WRAP_DIR=$(sed -n 's/^directory *= *//p' "benchmark/nixlbench/subprojects/$WRAP.wrap")
    LICENSE_EXPRESSION+=" AND $SPDX"
    for LICENSE_FILE in $LICENSE_FILES; do
        LICENSE_ARGS+=(--license-file "$WRAP/$LICENSE_FILE=benchmark/nixlbench/subprojects/$WRAP_DIR/$LICENSE_FILE")
    done
done
ALLOW_VENDORED_ARGS=()
for ENTRY in "${VENDORED_LIBS[@]}"; do
    IFS='|' read -r LIB SPDX LICENSE_FILES <<< "$ENTRY"
    LICENSE_EXPRESSION+=" AND $SPDX"
    ALLOW_VENDORED_ARGS+=(--allow-vendored "$LIB")
    for LICENSE_FILE in $LICENSE_FILES; do
        LICENSE_ARGS+=(--license-file "$LICENSE_FILE")
    done
done

# The backend wheels only: the nixl meta wheel has no libnixl.
shopt -s nullglob
NIXL_WHEELS=()
NIXL_PACKAGE=""
NIXL_VERSION=""
for WHEEL in "$NIXL_WHEEL_DIR"/nixl_*.whl; do
    if ! [[ "$(basename "$WHEEL")" =~ ^(nixl_(rocm[0-9]+_[0-9]+|cu[0-9]+))-([^-]+)- ]]; then
        continue
    fi
    if [ -n "$NIXL_PACKAGE" ] && [ "${BASH_REMATCH[1]}-${BASH_REMATCH[3]}" != "$NIXL_PACKAGE-$NIXL_VERSION" ]; then
        echo "ERROR: $NIXL_WHEEL_DIR holds NIXL wheels of more than one package or version: $NIXL_PACKAGE $NIXL_VERSION and ${BASH_REMATCH[1]} ${BASH_REMATCH[3]}" >&2
        exit 1
    fi
    NIXL_PACKAGE=${BASH_REMATCH[1]}
    NIXL_VERSION=${BASH_REMATCH[3]}
    NIXL_WHEELS+=("$WHEEL")
done
shopt -u nullglob
if [ ${#NIXL_WHEELS[@]} -eq 0 ]; then
    echo "ERROR: no NIXL backend wheel (nixl_rocm<X>_<Y>-*.whl or nixl_cu<N>-*.whl) in $NIXL_WHEEL_DIR" >&2
    exit 1
fi
echo "=== Building nixlbench for ${NIXL_PACKAGE} ${NIXL_VERSION} (${#NIXL_WHEELS[@]} NIXL wheels) against ${NIXL_PATH} and ROCm at ${ROCM_PATH} ==="

TMP_DIR=$(mktemp -d)
trap 'rm -rf "$TMP_DIR"' EXIT

# The NIXL build has already downloaded the asio and tomlplusplus wraps.
if [ -d subprojects/packagecache ]; then
    export MESON_PACKAGE_CACHE_DIR="$PWD/subprojects/packagecache"
fi
# No etcd: the wheel would have to vendor the etcd client with gRPC and
# protobuf. The ASIO runtime needs no service.
meson setup "$TMP_DIR/build" benchmark/nixlbench \
    --buildtype=release \
    -Dnixl_path="$NIXL_PATH" \
    -Dnixlbench_gpu=rocm \
    -Drocm_path="$ROCM_PATH" \
    -Detcd=disabled
ninja -C "$TMP_DIR/build" -j"${NPROC:-$(nproc)}" nixlbench

# DT_RPATH, not DT_RUNPATH: it is searched before LD_LIBRARY_PATH, so no other
# NIXL on the loader path can replace the libnixl of the wheel this binary
# requires. It replaces the link-time ROCm entry, which would likewise override
# the ROCm install the launcher selects.
patchelf --force-rpath --set-rpath "\$ORIGIN/../../.${NIXL_PACKAGE}.mesonpy.libs" "$TMP_DIR/build/nixlbench"

./contrib/wheel_nixlbench.py build \
    --binary "$TMP_DIR/build/nixlbench" \
    --nixl-wheel "${NIXL_WHEELS[0]}" \
    --platform "linux_$ARCH" \
    --output-dir "$TMP_DIR/raw" \
    --license-expression "$LICENSE_EXPRESSION" \
    "${LICENSE_ARGS[@]}"

# NIXL comes from the required NIXL wheel and ROCm from the host; excluding
# them also keeps everything only they link out of the wheel.
auditwheel repair --exclude 'libnixl*' --exclude 'libserdes*' --exclude 'libamdhip64*' \
    --plat "$WHL_PLATFORM" --wheel-dir "$TMP_DIR/dist" "$TMP_DIR"/raw/*.whl

NIXL_LIBDIR=""
for LIBDIR in lib64 "lib/$ARCH-linux-gnu" lib; do
    if [ -e "$NIXL_PATH/$LIBDIR/libnixl.so" ]; then
        NIXL_LIBDIR="$NIXL_PATH/$LIBDIR"
        break
    fi
done
if [ -z "$NIXL_LIBDIR" ]; then
    echo "ERROR: no libnixl.so under $NIXL_PATH" >&2
    exit 1
fi
./contrib/wheel_nixlbench.py check \
    "${ALLOW_VENDORED_ARGS[@]}" \
    --nixl-libdir "$NIXL_LIBDIR" \
    --nixl-wheel-dir "$NIXL_WHEEL_DIR" \
    "$TMP_DIR"/dist/nixlbench_*.whl

mkdir -p "$OUTPUT_DIR"
cp "$TMP_DIR"/dist/nixlbench_*.whl "$OUTPUT_DIR"
