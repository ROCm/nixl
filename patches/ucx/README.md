# UCX patchset

Applied to the UCX tree at `UCX_REF` (see the `Makefile`), in lexical filename
order, by the same mechanism as `patches/nixl/` and `patches/mori/`. See
[../README.md](../README.md) for the rules.

**These two patches are the fix for the intra-node GPU-to-GPU gap.** Together
they take NIXL's UCX backend from 0.33 GB/s to 47.7 GB/s between two MI300X GPUs
on one node — 98% of what `hipMemcpyPeer` gets on the same pair — and put it
ahead of MORI_IO at every block size from 16 KiB up.

| File | |
|---|---|
| `01-ucp-enable-rma-rndv-for-device-memory.patch` | Enable the RMA rendezvous protocol by default. Without this UCP never selects a protocol that can use an IPC transport. |
| `02-rocm-ipc-errhandle-peer-failure.patch` | Advertise `UCT_IFACE_FLAG_ERRHANDLE_PEER_FAILURE` on `rocm_ipc`, matching `cuda_ipc`. Without this UCP filters the transport out before protocol selection, for any application that asks for peer error handling — which NIXL does. |
| `experiments/99-rocm-ipc-runtime-flag-bisect.patch.experiment` | Not applied. Makes six `rocm_ipc` capability flags runtime-selectable via `ROCM_IPC_EXP` so the search space costs one build instead of one build per combination. How the above was found. |

All three apply to openucx **v1.22.0**, which is what `UCX_REF` pins. Check
before building:

```bash
make patch-check COMPONENT=ucx   # the two shipped patches; skips experiments/
make build
```

The patches were originally developed against `master`, where
`uct_rocm_ipc_iface_query` ends its `cap.flags` chain with
`UCT_IFACE_FLAG_DEVICE_EP` and `rocm_ipc_md.c` wraps the component in a
`uct_rocm_ipc_component_t`. Neither exists in v1.22.0, so patches 02 and 99
carry v1.22.0 context and will not apply to `master` unchanged; patch 01 is
context-identical on both.

## The diagnosis

Two independent faults, on the same path, in different layers. Each is
individually invisible: fix either one alone and the bandwidth does not move,
which is why this took two sessions to pin down.

**Fault 1 — UCP has no RMA protocol that can drive an IPC transport, by
default.** `rocm_ipc` and `cuda_ipc` are zcopy-only transports: no bcopy, no
short. The only UCP RMA protocol that emits zcopy over an `rma_bw` lane is the
RMA rendezvous protocol in `src/ucp/rma/rma_rndv.c`, and its probe begins:

```c
if (!context->config.ext.rma_ppln_enable &&
    (ucs_arch_get_cpu_model() != UCS_CPU_MODEL_NVIDIA_VERA)) {
    return 0;
}
```

`UCX_RMA_PPLN_ENABLE` defaults to `n`. So on every CPU on earth except NVIDIA
Vera, the protocol that GPU RMA needs is switched off, and `ucp_put`/`ucp_get`
on device memory falls back to software emulation over TCP. That fallback costs
0.33 GB/s and, being emulation, does not care how fast the hardware underneath
it is — hence the flat line in the results below.

Patch 01 flips the default to `y` and deletes the Vera special case, which the
new default subsumes. Host-to-host is unaffected: the probe still ends with

```c
return !UCP_MEM_IS_HOST(sg_mem_info->type) || !UCP_MEM_IS_HOST(rkey_mem_info->type);
```

so a host-to-host transfer is rejected exactly as before. Measured DRAM-to-DRAM
before and after: 46.827 GB/s both, identical to three digits.

**Fault 2 — `rocm_ipc` is filtered out before protocol selection, for NIXL
specifically.** NIXL creates endpoints with `UCP_ERR_HANDLING_MODE_PEER`. UCP
drops any interface lacking `UCT_IFACE_FLAG_ERRHANDLE_PEER_FAILURE` from lane
selection under that mode, and `rocm_ipc` did not advertise it while `cuda_ipc`
did. So with patch 01 alone, NIXL still gets 0.6 GB/s: the protocol is now
available but the transport it would run on is gone.

`ucx_perftest` does not request peer error handling, which is why patch 01 alone
takes *it* from 0.8 to 43.7 GB/s and made this look fixed from the UCX side
while NIXL saw nothing. Patch 02 alone is what
[ai-dynamo/nixl#2039](https://github.com/ai-dynamo/nixl/issues/2039) proposed;
tested alone it does nothing on gfx942, for the mirror-image reason — the
transport is eligible but no protocol will use it.

### The evidence for "exactly these two, nothing else"

The runtime-gated build makes each capability flag independently selectable, so
the necessary set can be measured rather than argued. nixlbench, UCX backend,
VRAM, GPU 0 to GPU 1, all rows with `UCX_RMA_PPLN_ENABLE=y`:

| `ROCM_IPC_EXP` | flags added | peak GB/s |
|---|---|---|
| (none) | — | 0.6 |
| `ir` | `MD_FLAG_INVALIDATE`, `INVALIDATE_RMA` | 0.4 |
| `k` | `MD_FLAG_RKEY_PTR` | 0.6 |
| **`e`** | **`IFACE_FLAG_ERRHANDLE_PEER_FAILURE`** | **47.7** |
| `ie`, `re`, `ire`, `irae`, `irke`, `irake` | supersets of `e` | 47.1 – 47.9 |

`e` is necessary and sufficient. Every other flag contributes nothing, and one
of them is actively dangerous: adding `UCT_COMPONENT_FLAG_RKEY_PTR` (`c`)
segfaults in `uct_rocm_ipc_ep_zcopy` — the rkey that reaches it is not the
`uct_rocm_ipc_key_t` it casts to. UCX's default handler then freezes the process
rather than dying, which reads as a hang.

This retires the earlier `01-rocm-ipc-cuda-ipc-capability-parity.patch.master-only`,
which set all six flags at once: five do nothing and the sixth crashes.

## Results

`ctr-smc-mi300x-cx68-25`, two MI300X (gfx942), GPU 0 to GPU 1, one initiator and
one target through nixlbench, `UCX_TLS` pinned away from IB (see the caveat
below). Ground truth for this pair, `hipMemcpyPeer` at 64 MiB: **48.8 GB/s**.

| block | UCX stock | UCX patched | MORI_IO |
|---|---|---|---|
| 64 KiB | 0.38 | 1.84 | 1.55 |
| 256 KiB | 0.42 | 6.81 | 5.60 |
| 1 MiB | 0.33 | 19.00 | 16.30 |
| 4 MiB | 0.33 | 34.91 | 27.16 |
| 16 MiB | 0.33 | 43.71 | 37.52 |
| 64 MiB | 0.33 | **47.67** | 46.62 |

Nothing is set in the environment for the patched column — the patches are the
whole configuration.

### Re-verified on v1.22.0

The numbers above were taken on the v1.19-era tree the patches were first
written against. Rebuilt on openucx v1.22.0 with the reworked patches, same
node, same pair, same pinned `UCX_TLS`:

| block | stock-equivalent | patched | MORI_IO |
|---|---|---|---|
| 64 KiB | 0.39 | 1.87 | 1.67 |
| 1 MiB | 0.44 | 19.34 | 17.08 |
| 16 MiB | 0.44 | 43.92 | 42.35 |
| 64 MiB | 0.44 | **47.99** | 47.02 |

"Stock-equivalent" is the patched image with `UCX_RMA_PPLN_ENABLE=n`, which
switches patch 01 back off at runtime and reproduces the stock curve without a
second 13-minute build. Patch 02's flag is still compiled in for that column,
which is exactly the "patch 02 alone does nothing" row of the evidence table
above — and it reads 0.44 GB/s flat, as it should.

The DRAM control is unchanged to three digits either way, 9.738 vs 9.746 GB/s
at 64 MiB, so the host path is still untouched on this tree. (The absolute
number is lower than the 46.8 above because these runs pin `UCX_TLS` away from
IB; only the before/after comparison is meaningful.)

### Caveat: this node cannot run UCX VRAM with the default `UCX_TLS`

Unrelated to these patches and present on stock UCX too, `mlx5_1` advertises
`rocm` memory support, NIXL registers a VRAM buffer against it, and
`ibv_reg_mr` returns `EFAULT`. NIXL treats one failed MD as total failure and
aborts. Every measurement here therefore pins
`UCX_TLS=rocm_ipc,rocm_copy,tcp,sysv,posix,cma`. Separate bug, recorded in
`overnight-2.md`.

## On the honesty of patch 02

`rocm_ipc` cannot actually detect a dead peer. Neither can `cuda_ipc`, which has
advertised this flag for years. The flag means "this transport participates in
peer error handling", and in practice an IPC transport discovers peer death when
the mapped memory goes away, not through any active mechanism.

So patch 02 makes `rocm_ipc` exactly as honest as `cuda_ipc` and no more. If
that is the wrong bar, the fix belongs upstream in both transports at once —
but shipping ROCm at a stricter standard than CUDA costs 100x of the fabric and
buys nothing, because the CUDA path has the same gap and nobody has been bitten
by it.

## Why UCX is patchable at all

Because transport selection is the one thing that decides whether NIXL can move
GPU memory between processes on a node, and it lives in UCX, not in NIXL.
