// SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc.
// SPDX-License-Identifier: Apache-2.0
//
// Build and run it inside a guest that has the slim stack installed and a KV
// namespace attached:
//
//   S=/opt/nixl-stack/opt; L=$S/nixl/lib/x86_64-linux-gnu
//   g++ -O2 -std=c++20 -I$S/nixl/include -I$S/nixl/include/backend \
//       cluster/scripts/kv-vram-roundtrip.cpp -o /tmp/kvvram \
//       $L/libnixl.so $L/libnixl_build.so $S/rocm/lib/libamdhip64.so.7 \
//       -Wl,-rpath,$L -Wl,-rpath,$S/rocm/lib
//   sudo env NIXL_PLUGIN_DIR=$L/plugins \
//       LD_LIBRARY_PATH=$L:$S/rocm/lib:$S/rocm/lib/rocm_sysdeps/lib \
//       /tmp/kvvram /dev/ng3n1
//
// -std=c++20 is required: nixl.h uses std::span. Both libnixl.so and
// libnixl_build.so are needed -- nixlDescList::addDesc lives in the latter.
// The HIP runtime entry points are declared here rather than included,
// because the slim guest stack carries ROCm's libraries without its headers.
//
// Prove the whole VRAM path: a pattern put on the device, Stored through
// NIXL, the device buffer wiped, Retrieved back through NIXL, and read off
// the device again. Nothing here trusts the plugin's own bookkeeping -- the
// comparison is against bytes that only ever existed in device memory.
#include "nixl.h"
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>
#include <chrono>
extern "C" {
typedef int hipError_t;
hipError_t hipMalloc(void **, size_t);
hipError_t hipMemcpy(void *, const void *, size_t, int);
hipError_t hipMemset(void *, int, size_t);
const char *hipGetErrorString(hipError_t);
}
static const int H2D = 1, D2H = 2;
#define HK(x) do { hipError_t e=(x); if(e){printf("HIP FAIL %s: %s\n",#x,hipGetErrorString(e));return 1;} } while(0)
#define NK(x) do { nixl_status_t s=(x); if(s!=NIXL_SUCCESS){printf("NIXL FAIL %s: %d\n",#x,s);return 1;} } while(0)

int main(int argc, char **argv) {
    const char *dev_path = argc > 1 ? argv[1] : "/dev/ng3n1";
    const size_t SIZE = 65536;
    const std::string KEY = "beef0123456789abcdef0123456789ab";  // 16 bytes of hex

    nixlAgentConfig cfg(false);
    nixlAgent agent("kvvram", cfg);
    nixlBackendH *bk = nullptr;
    nixl_b_params_t params;
    params["device_name"] = dev_path;
    NK(agent.createBackend("NVME_KV", params, bk));

    // The pattern is written straight to the device, so a buffer that still
    // holds it at the end can only have come back through the GPU.
    void *dptr = nullptr;
    HK(hipMalloc(&dptr, SIZE));
    std::vector<uint8_t> pattern(SIZE);
    for (size_t i = 0; i < SIZE; i++) pattern[i] = (uint8_t)((i * 31 + 7) & 0xFF);
    HK(hipMemcpy(dptr, pattern.data(), SIZE, H2D));

    nixl_reg_dlist_t vram(VRAM_SEG);
    vram.addDesc(nixlBlobDesc((uintptr_t)dptr, SIZE, 0, ""));
    NK(agent.registerMem(vram));

    nixl_reg_dlist_t objs(OBJ_SEG);
    objs.addDesc(nixlBlobDesc(0, SIZE, 0, KEY));
    NK(agent.registerMem(objs));

    nixl_xfer_dlist_t src(VRAM_SEG), dst(OBJ_SEG);
    src.addDesc(nixlBasicDesc((uintptr_t)dptr, SIZE, 0));
    dst.addDesc(nixlBasicDesc(0, SIZE, 0));

    auto run = [&](nixl_xfer_op_t op, const char *what) -> int {
        nixlXferReqH *req = nullptr;
        NK(agent.createXferReq(op, src, dst, "kvvram", req));
        auto _t0 = std::chrono::steady_clock::now();
        nixl_status_t st = agent.postXferReq(req);
        while (st == NIXL_IN_PROG) st = agent.getXferStatus(req);
        if (st != NIXL_SUCCESS) { printf("%s failed: %d\n", what, st); return 1; }
        agent.releaseXferReq(req);
        auto _t1 = std::chrono::steady_clock::now();
        printf("%s ok (%.0f us)\n", what, std::chrono::duration<double,std::micro>(_t1-_t0).count());
        return 0;
    };

    if (run(NIXL_WRITE, "Store from VRAM")) return 1;

    // Wipe the device buffer, so anything found afterwards came back.
    HK(hipMemset(dptr, 0, SIZE));
    std::vector<uint8_t> check(SIZE);
    HK(hipMemcpy(check.data(), dptr, SIZE, D2H));
    if (memcmp(check.data(), pattern.data(), SIZE) == 0) {
        printf("FAIL: wipe did not take, the test would prove nothing\n");
        return 1;
    }

    if (run(NIXL_READ, "Retrieve into VRAM")) return 1;

    HK(hipMemcpy(check.data(), dptr, SIZE, D2H));
    if (memcmp(check.data(), pattern.data(), SIZE) != 0) {
        size_t i = 0; while (i < SIZE && check[i] == pattern[i]) i++;
        printf("MISMATCH at byte %zu: got 0x%02x want 0x%02x\n", i, check[i], pattern[i]);
        return 1;
    }
    printf("VERIFIED: %zu bytes survived VRAM -> NVMe KV -> VRAM byte-identical\n", SIZE);
    return 0;
}
