#!/bin/bash
# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# Build fio with the hipFile ioengine into FIO_PREFIX.
#
# Why fio is in this image at all: every AIS/AIS_MT number the sweeps produce
# is a NIXL number, and NIXL is a thick layer.  When the AIS plugin plateaued
# at ~5.5 GB/s there was no way to say from inside this tree whether the limit
# was the plugin, NIXL, or hipFile itself -- the closest thing to a ground
# truth available was parallel `dd`, which is queue depth 1 and measures a
# different thing.  fio's libhipfile engine drives the same library through the
# same GPU-direct path with no NIXL in the picture at all.
#
# The ROCm fork rather than upstream fio, for the same reason: its engine adds
# hipfile_mode=sync|stream|batch, which lines up one-for-one with what this
# tree's backends do --
#
#   sync    hipFileRead/hipFileWrite            <-> what AIS_MT drives from a pool
#   stream  hipFileReadAsync/hipFileWriteAsync  <-> what the AIS plugin submits
#   batch   hipFileBatchIOSetUp/Submit/GetStatus <-> the API the AIS plugin was
#           originally planned against, and which is still a stub in hipFile
#           (accepts submissions, does no I/O, GetStatus returns Not
#           Implemented) -- so `batch` is expected to fail, and that failure is
#           itself the check that the stub has not silently started "working".
#
# Upstream fio only has the synchronous engine (--enable-libhipfile landed on
# master, no release tag carries it yet), so it cannot make the comparison this
# image exists to make.  FIO_GIT_URL/FIO_REF can be pointed back at
# axboe/fio if the fork is ever merged.
set -euo pipefail

FIO_PREFIX="${FIO_PREFIX:-/opt/fio}"
FIO_SRC="${FIO_SRC:-/tmp/fio-src}"
ROCM_PATH="${ROCM_PATH:-/opt/rocm}"
HIPFILE_PREFIX="${HIPFILE_PREFIX:-/opt/hipfile}"
# A single prefix that looks like a ROCm install to fio's configure, but whose
# hipFile pieces are the source-built ones.  See the long comment below.
FIO_ROCM_VIEW="${FIO_ROCM_VIEW:-/opt/fio-hipfile}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

if [[ ! -f "${FIO_SRC}/configure" ]]; then
	echo "ERROR: ${FIO_SRC} is not a fio checkout (no configure)" >&2
	exit 1
fi

# ---------------------------------------------------------------------------
# The merged prefix.
#
# fio's configure probes hipFile with exactly one knob, ROCM_PATH, and derives
# everything from it:
#
#   -I${ROCM_PATH}/include
#   -L${ROCM_PATH}/lib -Wl,-rpath,${ROCM_PATH}/lib -lamdhip64 -lhipfile
#
# That is fine when there is one hipFile on the system.  Here there are two:
# /opt/rocm ships hipfile.h plus libhipfile.so.0 -> .0.3.0, and the hipfile
# stage installs its own hipfile.h plus libhipfile.so.0 -> .0.2.0 into
# /opt/hipfile.  Same soname, different libraries -- the packaged 0.3.0 is
# older than the source-built one despite the larger version number, which is
# the whole reason the hipfile stage exists.
#
# Neither ROCM_PATH answer is usable on its own: /opt/rocm gets the wrong
# hipFile, /opt/hipfile has no libamdhip64 and no hip/ headers.  Splitting the
# difference with --extra-cflags/--extra-ldflags does not work either, because
# configure *prepends* its own hipFile flags to CFLAGS and appends its libs
# after LDFLAGS, so which -I and which -L wins depends on where the generated
# Makefile happens to place each variable on the command line.  That is a
# coin-flip decided by fio's build system, and the failure mode is silent: a
# binary that links the wrong hipFile still runs, still reports bandwidth, and
# simply is not measuring the library the AIS plugin uses.
#
# So build one directory that is unambiguous, and point ROCM_PATH at it.
# Symlinks, not copies, so it cannot drift from what the hipfile stage built;
# the runtime stage carries the view along and the links still resolve there.
mkdir -p "${FIO_ROCM_VIEW}/include" "${FIO_ROCM_VIEW}/lib"
ln -sfn "${ROCM_PATH}/include/hip" "${FIO_ROCM_VIEW}/include/hip"
for _l in "${ROCM_PATH}"/lib/libamdhip64.so*; do
	ln -sfn "${_l}" "${FIO_ROCM_VIEW}/lib/$(basename "${_l}")"
done
# hipFile last, so it wins any name it shares with the ROCm links above.
if [[ -e "${HIPFILE_PREFIX}/lib/libhipfile.so" ]]; then
	for _f in "${HIPFILE_PREFIX}"/include/*; do
		ln -sfn "${_f}" "${FIO_ROCM_VIEW}/include/$(basename "${_f}")"
	done
	for _f in "${HIPFILE_PREFIX}"/lib/libhipfile.so*; do
		ln -sfn "${_f}" "${FIO_ROCM_VIEW}/lib/$(basename "${_f}")"
	done
	echo "[fio] hipFile for the engine: ${HIPFILE_PREFIX} (via ${FIO_ROCM_VIEW})"
else
	# HIPFILE_REF was unset.  Fall back to the packaged copy rather than
	# failing: the escape hatch is supposed to produce a working image, just
	# one measuring a different hipFile.
	for _f in "${ROCM_PATH}"/include/hipfile*.h; do
		ln -sfn "${_f}" "${FIO_ROCM_VIEW}/include/$(basename "${_f}")"
	done
	for _f in "${ROCM_PATH}"/lib/libhipfile.so*; do
		ln -sfn "${_f}" "${FIO_ROCM_VIEW}/lib/$(basename "${_f}")"
	done
	echo "[fio] HIPFILE_REF unset -- engine links the ROCm-packaged libhipfile"
fi

cd "${FIO_SRC}"

# --enable-libhipfile makes the probe hard-fail instead of quietly dropping the
# engine, which is the only way a missing engine surfaces at build time rather
# than as a puzzling "engine not found" hours later on a GPU node.
#
# No --enable-cuda/--enable-libcufile: this image has no CUDA toolkit, and
# unlike the CI image it never needs to talk to an NVIDIA GPU.
ROCM_PATH="${FIO_ROCM_VIEW}" ./configure \
	--prefix="${FIO_PREFIX}" \
	--enable-libhipfile

make -j"${BUILD_JOBS}"
make install prefix="${FIO_PREFIX}"

_fio="${FIO_PREFIX}/bin/fio"
[[ -x "${_fio}" ]] || { echo "ERROR: ${_fio} not installed" >&2; exit 1; }

# Assert the engine is compiled in.  configure --enable-* already fails on a
# missing dependency, but it does not prove the engine reached the binary.
if ! "${_fio}" --enghelp 2>&1 | grep -qE '^[[:space:]]*libhipfile[[:space:]]*$'; then
	echo "ERROR: fio built without the libhipfile ioengine" >&2
	"${_fio}" --enghelp >&2
	exit 1
fi

# Assert it linked the hipFile we meant.  This is the check the merged prefix
# exists for; without it the whole arrangement is unverified.  ldd reports the
# view path, so resolve the symlink to see which library it actually is.
_resolved="$(ldd "${_fio}" 2>/dev/null | awk '/libhipfile/ {print $3}')"
if [[ -z "${_resolved}" ]]; then
	echo "ERROR: fio does not link libhipfile at all" >&2
	exit 1
fi
_real="$(readlink -f "${_resolved}")"
echo "[fio] libhipfile: ${_resolved} -> ${_real}"
if [[ -e "${HIPFILE_PREFIX}/lib/libhipfile.so" ]] \
	&& [[ "${_real}" != "${HIPFILE_PREFIX}"/* ]]; then
	echo "ERROR: fio linked ${_real}, not the source-built ${HIPFILE_PREFIX}" >&2
	exit 1
fi

# Record what this build can do, so neither the sweep scripts nor a human has
# to re-derive it from engine help on a GPU node.  hipfile_mode only exists on
# the ROCm fork; on upstream fio the file says `no` and the async comparisons
# are simply unavailable.
install -d "${FIO_PREFIX}/share"
"${_fio}" --enghelp=libhipfile > "${FIO_PREFIX}/share/fio-libhipfile-help.txt" 2>&1 || true
if grep -q 'hipfile_mode' "${FIO_PREFIX}/share/fio-libhipfile-help.txt"; then
	echo yes > "${FIO_PREFIX}/share/fio-hipfile-async.txt"
	# --enghelp=<engine>,<opt> is the only form that lists the mode names.
	"${_fio}" --enghelp=libhipfile,hipfile_mode \
		>> "${FIO_PREFIX}/share/fio-libhipfile-help.txt" 2>&1 || true
else
	echo no > "${FIO_PREFIX}/share/fio-hipfile-async.txt"
fi
git -C "${FIO_SRC}" rev-parse HEAD > "${FIO_PREFIX}/share/fio-commit.txt" 2>/dev/null || true

# The fork's example jobs are driven by FIO_DIR/GPU_DEV_IDS and are the
# quickest way to check one mode by hand.
install -d "${FIO_PREFIX}/share/examples"
for _e in "${FIO_SRC}"/examples/libhipfile*.fio; do
	[[ -e "${_e}" ]] && install -m 0644 "${_e}" "${FIO_PREFIX}/share/examples/"
done

echo "[fio] $("${_fio}" --version), async=$(cat "${FIO_PREFIX}/share/fio-hipfile-async.txt")"
echo "fio build complete prefix=${FIO_PREFIX}"
