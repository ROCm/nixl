// Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
//
// SPDX-License-Identifier: MIT
//
// Ground truth for GPU-to-GPU copy bandwidth on this node, so a UCX number can
// be checked against something that has no protocol stack in it.
//
// Motivation: ucx_perftest reported 845 GB/s for a GPU0->GPU1 ucp_put once
// UCX_RMA_PPLN_ENABLE=y let it reach rocm_ipc.  That is far above any plausible
// pairwise Infinity Fabric figure, so either the fabric is much faster than
// assumed or the two ranks were not actually on different devices.  This tells
// us which, by doing the same copy with five lines of HIP.
//
// Reports both directions and the same-device case, because the same-device
// number is exactly what a bogus "peer" measurement would look like.
//
//   hipcc -O3 p2p-truth.hip.cpp -o p2p-truth && ./p2p-truth [src] [dst] [MiB]

#include <hip/hip_runtime.h>
#include <cstdio>
#include <cstdlib>
#include <vector>

#define CHECK(x)                                                               \
    do {                                                                       \
        hipError_t _e = (x);                                                   \
        if (_e != hipSuccess) {                                                \
            fprintf(stderr, "%s:%d: %s\n", __FILE__, __LINE__,                 \
                    hipGetErrorString(_e));                                    \
            exit(1);                                                           \
        }                                                                      \
    } while (0)

static double bench(int src, int dst, size_t bytes, int iters)
{
    void *p_src, *p_dst;

    CHECK(hipSetDevice(src));
    CHECK(hipMalloc(&p_src, bytes));
    CHECK(hipMemset(p_src, 0xA5, bytes));
    CHECK(hipSetDevice(dst));
    CHECK(hipMalloc(&p_dst, bytes));

    // Peer access has to be enabled explicitly or hipMemcpyPeer silently
    // stages through host memory, which would understate the fabric instead of
    // overstating it -- the opposite error, but still an error.
    if (src != dst) {
        int can = 0;
        CHECK(hipDeviceCanAccessPeer(&can, dst, src));
        if (!can) {
            printf("  %d -> %d: no peer access\n", src, dst);
            CHECK(hipFree(p_dst));
            CHECK(hipSetDevice(src));
            CHECK(hipFree(p_src));
            return 0.0;
        }
        CHECK(hipSetDevice(dst));
        hipError_t e = hipDeviceEnablePeerAccess(src, 0);
        if (e != hipSuccess && e != hipErrorPeerAccessAlreadyEnabled) {
            CHECK(e);
        }
    }

    hipStream_t stream;
    CHECK(hipStreamCreate(&stream));
    hipEvent_t t0, t1;
    CHECK(hipEventCreate(&t0));
    CHECK(hipEventCreate(&t1));

    for (int i = 0; i < 10; ++i) {
        CHECK(hipMemcpyPeerAsync(p_dst, dst, p_src, src, bytes, stream));
    }
    CHECK(hipStreamSynchronize(stream));

    CHECK(hipEventRecord(t0, stream));
    for (int i = 0; i < iters; ++i) {
        CHECK(hipMemcpyPeerAsync(p_dst, dst, p_src, src, bytes, stream));
    }
    CHECK(hipEventRecord(t1, stream));
    CHECK(hipStreamSynchronize(stream));

    float ms = 0.0f;
    CHECK(hipEventElapsedTime(&ms, t0, t1));

    CHECK(hipFree(p_dst));
    CHECK(hipSetDevice(src));
    CHECK(hipFree(p_src));
    CHECK(hipStreamDestroy(stream));
    CHECK(hipEventDestroy(t0));
    CHECK(hipEventDestroy(t1));

    return (double)bytes * iters / (ms * 1e-3) / 1e9;
}

int main(int argc, char **argv)
{
    int    src   = (argc > 1) ? atoi(argv[1]) : 0;
    int    dst   = (argc > 2) ? atoi(argv[2]) : 1;
    size_t mib   = (argc > 3) ? (size_t)atoll(argv[3]) : 8;
    size_t bytes = mib << 20;
    int    iters = 200;

    int ndev = 0;
    CHECK(hipGetDeviceCount(&ndev));
    printf("%d GPUs visible, %zu MiB, %d iters\n", ndev, mib, iters);
    if (dst >= ndev || src >= ndev) {
        fprintf(stderr, "device out of range\n");
        return 1;
    }

    hipDeviceProp_t prop;
    CHECK(hipGetDeviceProperties(&prop, src));
    printf("device %d: %s\n", src, prop.gcnArchName);

    printf("  %d -> %d (same device) : %8.1f GB/s\n", src, src,
           bench(src, src, bytes, iters));
    printf("  %d -> %d (peer)        : %8.1f GB/s\n", src, dst,
           bench(src, dst, bytes, iters));
    printf("  %d -> %d (peer)        : %8.1f GB/s\n", dst, src,
           bench(dst, src, bytes, iters));
    return 0;
}
