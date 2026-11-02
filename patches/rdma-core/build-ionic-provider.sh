#!/bin/bash
# Build rdma-core v61.0's ionic provider with the sq_sig_all backport.
#
# v61.0 rather than upstream master because libibverbs loads providers by
# private ABI version: the guests run rdma-core 61.0, whose libibverbs looks
# for libionic-rdmav59.so. An upstream build produces rdmav65, which it will
# not load at all.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends \
	git cmake ninja-build gcc libc6-dev pkg-config python3 \
	libnl-3-dev libnl-route-3-dev libudev-dev libsystemd-dev \
	ca-certificates >/dev/null

git clone -q --depth 1 -b v61.0 https://github.com/linux-rdma/rdma-core.git /src
cd /src
git apply --whitespace=nowarn /patches/01-ionic-honour-sq-sig-all.patch

# Fail here rather than shipping a provider that silently still drops
# completions.
grep -q 'qp->sig_all ||' providers/ionic/ionic_verbs.c

cmake -G Ninja -S /src -B build \
	-DCMAKE_BUILD_TYPE=Release \
	-DENABLE_RESOLVE_NEIGH=0 \
	-DNO_MAN_PAGES=1 >/dev/null
ninja -C build >/dev/null

mkdir -p /out
find build -name 'libionic*.so*' -exec cp -a {} /out/ \;
ls -la /out
