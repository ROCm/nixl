#!/bin/bash
# Build the AWS SDK for C++, s3 and s3-crt only.
#
# WHY
#
# NIXL's OBJ plugin -- every object-storage engine it has, including the
# S3-over-RDMA one -- is skipped wholesale without this:
#
#   src/utils/object/meson.build
#     aws_s3     = dependency('aws-cpp-sdk-s3',     required: false)
#     aws_s3_crt = dependency('aws-cpp-sdk-s3-crt', required: false)
#     aws_core   = dependency('aws-cpp-sdk-core',   required: false)
#     if all three found -> build obj_utils
#   src/plugins/obj/meson.build
#     if not is_variable('obj_utils_interface') -> subdir_done()
#
# So libplugin_OBJ.so simply never appears, with one warning in the meson log
# and no error. That is why this image has had no object backend at all.
#
# BUILD_ONLY is doing real work here. The full SDK is ~400 service clients and
# takes the better part of an hour; s3 and s3-crt are the two NIXL asks for by
# pkg-config name, and core comes along as their dependency. Adding a service
# later means adding it to this list, not rebuilding the world.
set -euo pipefail

AWS_SDK_SRC="${AWS_SDK_SRC:-/tmp/aws-sdk-src}"
AWS_SDK_PREFIX="${AWS_SDK_PREFIX:-/opt/aws-sdk}"
BUILD_JOBS="${BUILD_JOBS:-$(nproc)}"

# -DBUILD_SHARED_LIBS=ON so the NIXL plugin links .so and the runtime image
# can carry just the libraries. The static build would pull the whole CRT into
# every consumer.
#
# -DENABLE_TESTING=OFF and -DAUTORUN_UNIT_TESTS=OFF: the test suite wants
# network access and credentials, neither of which a build layer has.
#
# -DCMAKE_INSTALL_LIBDIR=lib, not lib64: meson's pkg-config lookup follows
# PKG_CONFIG_PATH, and keeping every prefix in this image on lib/ means one
# path rather than two spellings to get wrong.
cmake -S "${AWS_SDK_SRC}" -B "${AWS_SDK_SRC}/build" \
	-DCMAKE_BUILD_TYPE=Release \
	-DCMAKE_INSTALL_PREFIX="${AWS_SDK_PREFIX}" \
	-DCMAKE_INSTALL_LIBDIR=lib \
	-DBUILD_ONLY="s3;s3-crt" \
	-DBUILD_SHARED_LIBS=ON \
	-DENABLE_TESTING=OFF \
	-DAUTORUN_UNIT_TESTS=OFF \
	-DCPP_STANDARD=17

cmake --build "${AWS_SDK_SRC}/build" -j"${BUILD_JOBS}"
cmake --install "${AWS_SDK_SRC}/build"

# Repair the generated .pc files. The SDK writes CMAKE_INSTALL_LIBDIR and
# CMAKE_INSTALL_INCLUDEDIR into them verbatim, with no prefix= line at all:
#
#   includedir=include
#   libdir=lib
#   Cflags: -I${includedir} ...
#   Libs: -L${libdir} -laws-cpp-sdk-s3
#
# Those are RELATIVE paths, so they resolve against whatever directory the
# consumer happens to build in. Meson dutifully passes them on and ld goes
# looking in <nixl-build-dir>/lib, which is how `cannot find -laws-cpp-sdk-s3`
# appears for libraries that are plainly installed and that pkg-config has
# already said it found. Nothing downstream can correct this; the file has to
# be right. Upstream only escapes it by installing to a prefix where the bare
# relative path happens to be unnecessary.
for pc in "${AWS_SDK_PREFIX}"/lib/pkgconfig/*.pc; do
	[[ -e "${pc}" ]] || continue
	sed -i \
		-e "1i prefix=${AWS_SDK_PREFIX}" \
		-e 's|^includedir=.*|includedir=${prefix}/include|' \
		-e 's|^libdir=.*|libdir=${prefix}/lib|' \
		"${pc}"
done

# Assert the three pkg-config names NIXL looks up, by the names it uses.
# Finding the libraries but not the .pc files would leave the OBJ plugin
# skipped exactly as before, and the only symptom would be a missing plugin.
export PKG_CONFIG_PATH="${AWS_SDK_PREFIX}/lib/pkgconfig:${PKG_CONFIG_PATH:-}"
missing=0
for pc in aws-cpp-sdk-s3 aws-cpp-sdk-s3-crt aws-cpp-sdk-core; do
	if pkg-config --exists "${pc}"; then
		# Existence is not enough -- the relative-path bug above leaves
		# pkg-config happy and the linker lost. Demand the absolute -L.
		if ! pkg-config --libs "${pc}" | grep -q -- "-L${AWS_SDK_PREFIX}/lib"; then
			echo "ERROR: ${pc} has no -L${AWS_SDK_PREFIX}/lib: $(pkg-config --libs "${pc}")" >&2
			missing=1
			continue
		fi
		echo "[aws-sdk] ${pc} $(pkg-config --modversion "${pc}")"
	else
		echo "ERROR: pkg-config cannot find ${pc}" >&2
		missing=1
	fi
done
[[ "${missing}" -eq 0 ]] || exit 1
