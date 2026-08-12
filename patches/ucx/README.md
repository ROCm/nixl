# UCX patchset

Applied to `ROCm/ucx` at `UCX_REF` (see the `Makefile`), in lexical filename
order, by the same mechanism as `patches/nixl/` and `patches/mori/`. See
[../README.md](../README.md) for the rules.

**Currently empty** — the pristine tag is built, because the one patch worth
having only applies to openucx `master` and this tree pins ROCm/ucx `v1.19.x`.

Files here:

| File | |
|---|---|
| `01-rocm-ipc-cuda-ipc-capability-parity.patch.master-only` | The real finding. Gets `rocm_ipc` into the `rma_bw` lanes. Verified. Apply with `UCX_REF=master`. |
| `01-rocm-ipc-errhandle-peer-failure.patch.disabled` | Superseded by the above — it is a strict subset. |
| `02-rocm-ipc-rkey-ptr-flag.patch.disabled` | Also superseded; set the component flag but not the MD flag. |

## The diagnosis, in full

`rocm_ipc` is not slow and is not broken. Two UCP APIs, same node, same
transports, same GPU memory:

```
ucx_perftest -t tag_bw     -m rocm  ->  tag(... rocm_ipc/rocm_ipc)  ~511 GB/s
ucx_perftest -t ucp_put_bw -m rocm  ->  rma(sysv/posix)             ~0.4 GB/s
```

Two separate things go wrong on the RMA path, and only the first is a
capability problem:

**1. `rocm_ipc` was not eligible for an `rma_bw` lane.** `ucp_wireup_add_rma_bw_lanes`
adds `UCT_MD_FLAG_INVALIDATE_RMA` to its criteria whenever the endpoint asks
for peer error handling, which NIXL does by default. `cuda_ipc` advertises that
flag; `rocm_ipc` did not. **The parity patch fixes this**, and it is directly
observable:

```
without patch:  no rma_bw lane for rocm_ipc at all
with patch:     ep lane[2]: 7:rocm_ipc/rocm_ipc.0 md[6] -> ... rma_bw#0
```

**2. UCP's RMA protocol selection never uses that lane.** With
`UCX_PROTO_INFO=y`, for a GPU-to-GPU transfer:

```
| remote memory write by ucp_put*(multi) from rocm/GPU1 to rocm/dev[0] |
| 0..inf | software emulation | tcp/eth2                              |
```

Software emulation over TCP for the entire size range, while an `rma_bw` lane
on `rocm_ipc` sits unused. `rma_bw` lanes are consumed by the **rendezvous**
protocols — which is exactly why `tag_bw` flies on that lane and `ucp_put_bw`
does not.

So end-to-end bandwidth is unchanged by the patch: 0.33 GB/s before, 0.33 GB/s
after. The remaining gap is a missing put/get zcopy protocol for GPU memory in
UCP's protocol layer. That is a feature, not a flag, and it needs a decision
from whoever owns UCP protocol selection.

### How to build with it

```bash
mv patches/ucx/01-rocm-ipc-cuda-ipc-capability-parity.patch.master-only \
   patches/ucx/01-rocm-ipc-cuda-ipc-capability-parity.patch
make dist-build-here MAKE_ARGS="UCX_GIT_URL=https://github.com/openucx/ucx.git UCX_REF=master"
```

Add `UCX_DEBUG_LOG=1` to keep `ucs_debug`/`ucs_trace` compiled in — without it
`UCX_LOG_LEVEL=debug` prints nothing and none of the above is visible.

## Superseded experiments



## Why UCX is patchable at all

Because transport selection is the one thing that decides whether NIXL can move
GPU memory between processes on a node, and it lives in UCX, not in NIXL.

## `01-rocm-ipc-errhandle-peer-failure.patch.disabled`

Named `.disabled` so `clone-src.sh` (which globs `*.patch`) does not apply it.
Rename to `.patch` to try it.

It adds `UCT_IFACE_FLAG_ERRHANDLE_PEER_FAILURE` to `rocm_ipc`'s advertised
capabilities. The reasoning is sound: `rocm_ipc` already advertises
`PUT_ZCOPY`/`GET_ZCOPY`, so it can do the RMA that NIXL's UCX backend issues,
but NIXL creates endpoints with `UCP_ERR_HANDLING_MODE_PEER` and UCX drops any
transport that cannot do peer error handling — so `rocm_ipc` is filtered out
before protocol selection ever considers it.

Proposed by @anjoj0 in [ai-dynamo/nixl#2039](https://github.com/ai-dynamo/nixl/issues/2039),
where on 8x Radeon PRO W7900 (gfx1100) it made all four GPU-pair cases select
`rocm_ipc/rocm_ipc` at ~27.4 GB/s READ / ~23.5 GB/s WRITE with NIXL's default
peer error mode.

### It does not work on MI300X — tested

Disabled because we could not reproduce that result here. Tested both:

| UCX | Flag live? | NIXL intra-node VRAM |
|---|---|---|
| ROCm/ucx v1.19.x + patch | — | `rma_am(tcp/veth)`, 0.34 GB/s |
| openucx master 1.23.0 + patch | yes, `error handling: peer failure` | `rma_am(tcp/eth2)`, 0.33 GB/s |
| **NIXL main + master + patch** | yes | `rma_am(tcp/eth2)`, 0.60 GB/s |
| master + patch + `UCT_COMPONENT_FLAG_RKEY_PTR` | yes | `rma_am(tcp/eth2)`, 0.64 GB/s |

The last row is the reporter's exact stack -- NIXL `main` (their "1.4"),
UCX post-#11299, flag applied. It does not reproduce on gfx942, so the
remaining difference is the hardware itself.

The flag is verifiably applied (`ucx_info -d` reports `error handling: peer
failure` for `rocm_ipc`) and `rocm_ipc` still never enters the RMA lane on
gfx942. Note master already carries openucx/ucx#11299, so #11299 is not the
missing piece either.

Remaining differences from the environment where it did work:

- **hardware**: W7900 / gfx1100 (PCIe P2P) against MI300X / gfx942 (XGMI);
- **NIXL version**: they report NIXL 1.4, which is unreleased `main` — no v1.4
  tag exists. This tree pins v1.3.2. NIXL main carries the explicit ROCm VRAM
  memtype hint (#1536); the reporter says that alone did not fix it, but
  #1536 *plus* the flag is a combination we have not tested.

Testing NIXL `main` + this flag on gfx942 is the obvious next experiment.

### Safety note if you do enable it

The patch asserts a capability. Telling UCX that `rocm_ipc` can detect a dead
peer, when it cannot, turns a clean `NIXL_ERR_REMOTE_DISCONNECT` into a hang.
The proposer ran 12 cross-NUMA peer-exit scenarios without a hang but
explicitly excluded stale-registration behaviour from the claim. It is an
experiment, not a hardening.
