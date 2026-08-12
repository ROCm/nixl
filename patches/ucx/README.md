# UCX patchset

Applied to `ROCm/ucx` at `UCX_REF` (see the `Makefile`), in lexical filename
order, by the same mechanism as `patches/nixl/` and `patches/mori/`. See
[../README.md](../README.md) for the rules.

**Currently empty** — the pristine tag is built. One patch is parked here
disabled; read on before enabling it.

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
