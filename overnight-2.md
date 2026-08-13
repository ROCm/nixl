# Overnight work log, session 2

Unattended session, evening of 2026-08-12. Newest entries at the bottom.
Session 1 is in `OVERNIGHT.md`; this file assumes it.

## The ask

1. Wire the UCX intra-node fix into NIXL and generate performance data.
2. More performance testing for AIS_MT and NVMe.

## Result, up front

**The intra-node GPU-to-GPU gap is fixed.** Two one-line UCX changes, in
`patches/ucx/`, take NIXL's UCX backend from 0.33 GB/s to 47.7 GB/s between two
MI300X on one node — 98% of `hipMemcpyPeer` ground truth (48.8), and ahead of
MORI_IO at every block size from 16 KiB up. Nothing needs to be set in the
environment. Neither patch does anything without the other.

**AIS_MT scales and the POSIX backend does not.** Across 16 NVMe drives, AIS_MT
sustains ~56 GB/s read and write while POSIX peaks at 48 and then collapses to
23 as thread count rises past 8 — and POSIX cannot use fewer threads than
drives, so it cannot address a wide array without paying that.

Three things from session 1 are corrected below: the 511 GB/s figure (it was a
same-device copy), the capability-parity patch (five of its six flags do nothing
and the sixth segfaults), and the "hang" (it is a SIGSEGV).

## Where session 1 left off

The intra-node GPU-to-GPU gap (MORI_IO 46.7 GB/s vs NIXL/UCX 0.33-0.69 GB/s) was
diagnosed down to two independent faults in UCX, reproducible without NIXL:

1. `rocm_ipc` was not eligible for an `rma_bw` lane, because
   `ucp_wireup_add_rma_bw_lanes` requires `UCT_MD_FLAG_INVALIDATE_RMA` under
   peer error handling and `rocm_ipc` did not advertise it. A capability-parity
   patch against `cuda_ipc` fixes this, verified by lane dump.
2. UCP's RMA protocol selection picks `software emulation | tcp` anyway. The
   put/get protocol for GPU memory *does* exist, in `src/ucp/rma/rma_rndv.c`,
   but is gated behind `UCX_RMA_PPLN_ENABLE` (default `n`) plus a hardcoded
   `UCS_CPU_MODEL_NVIDIA_VERA` bypass.

Fault 1 patch alone: no hang, no speedup. Fault 2 gate alone: protocol changes
to rndv, still over tcp, no speedup. **Both together: hangs.** That hang is the
blocker and is where this session starts.

## Plan

| # | Step | Why |
|---|---|---|
| 1 | Make the rocm_ipc capability flags runtime-selectable | A 13-minute image rebuild per flag combination makes bisection unaffordable. One build + an env var turns the whole search space into minutes. |
| 2 | Bisect which flag causes the hang | The parity patch changes six things at once. `RKEY_PTR` is the prime suspect: advertising it invites UCP to use a direct-mapped-pointer protocol. |
| 3 | Build the minimal patch series from the result | Ship the smallest set that works, not the whole cuda_ipc parity block. |
| 4 | Wire into NIXL, measure | The point of the exercise. |
| 5 | AIS_MT and NVMe sweeps | Queue depth, block size, thread count, multi-GPU, and a POSIX/AIS_MT comparison worth quoting. |

## Log

### 2026-08-12 23:40 — the "hang" is a segfault, and the parity patch was never the fix

Made the six rocm_ipc capability flags runtime-selectable (`ROCM_IPC_EXP`, in
`patches/ucx/experiments/`), so the whole matrix costs one build. Verified the
gating works before trusting it: `ucx_info -d` reports `error handling: none`
with the var unset and `peer failure` with `e` set.

`ucx_perftest -t ucp_put_bw -m rocm,rocm`, GPU 0 to GPU 1, 8 MiB:

| ROCM_IPC_EXP | RMA_PPLN | BW (GB/s) | protocol chosen |
|---|---|---|---|
| (none) | n | 0.8 | software emulation, sysv/memory |
| any subset | n | 0.8 | software emulation, sysv/memory |
| **(none)** | **y** | **845.3** | rndv zero-copy read from remote, **rocm_ipc** |
| e / ir / ire / ira | y | 565-591 | rndv zero-copy read, rocm_ipc |
| irk / irake | y | 820-825 | rndv zero-copy read, rocm_ipc |
| **irkc / irakec** | **y** | **SEGFAULT** | - |

Three things fall out, and two of them contradict session 1.

**1. The capability-parity patch is not what fixes this.** With no flags at all,
just `UCX_RMA_PPLN_ENABLE=y`, UCP picks `rocm_ipc` and the transfer runs at
845 GB/s. Session 1 concluded the parity patch was a necessary half-fix. For
`ucx_perftest` it is not necessary at all. The reason session 1 could not see
this is that it never ran the two changes independently -- it went straight from
"parity patch alone does nothing" to "parity patch plus gate hangs".

Note the parity flags should still matter *for NIXL specifically*, because the
lane-eligibility filter they clear (`UCT_MD_FLAG_INVALIDATE_RMA` in
`ucp_wireup_add_rma_bw_lanes`) only applies under `UCP_ERR_HANDLING_MODE_PEER`,
which `ucx_perftest` does not request and NIXL does by default. That is a
prediction, not a result, and it is the next thing to test.

**2. It was never a hang.** It is a SIGSEGV in `uct_rocm_ipc_ep_zcopy`, and UCX's
default error handler freezes the process for debugging, which is what session 1
saw as a 30-minute stall:

```
Caught signal 11 (address not mapped to object at address 0x1f)
  4  libuct_rocm.so(uct_rocm_ipc_ep_zcopy+0x4e)
  5  libuct_rocm.so(uct_rocm_ipc_ep_get_zcopy+0x45)
  7  libucp.so(ucp_proto_rndv_receive_start+0x2a1)
  8  libucp.so(ucp_rma_rndv_process_rts+0x130)
```

**3. The culprit is one flag: `c`, `UCT_COMPONENT_FLAG_RKEY_PTR`.** `irk` is fine
and `irkc` crashes; the only difference is the component flag. Advertising it
changes how UCP unpacks the rkey, and the rkey that then reaches
`uct_rocm_ipc_ep_zcopy` is not the `uct_rocm_ipc_key_t` it casts to. So the fix
is to drop that one flag, which session 1 had bundled in with the other five on
the strength of openucx/ucx#11069 being the cuda_ipc equivalent.

Caveat before any of this is quotable: 845 GB/s for GPU-to-GPU needs a ground
truth check against `rocm-bandwidth-test`, and the run needs data validation.
Both next.

### 2026-08-13 00:05 — the 845 GB/s was a same-device copy, and so were session 1's

845 GB/s GPU-to-GPU did not survive contact with ground truth. Wrote a five-line
HIP peer-copy benchmark (`docker/scripts/p2p-truth.hip.cpp`) to get a number with
no protocol stack in it:

```
8 GPUs visible, 8 MiB, 200 iters, gfx942
  0 -> 0 (same device) :   2050.4 GB/s
  0 -> 1 (peer)        :     45.7 GB/s
  1 -> 0 (peer)        :     46.1 GB/s
```

Pairwise Infinity Fabric on this node is **45.7 GB/s**. Anything above that is
not crossing between two GPUs.

The cause is a two-character mistake that has been in every UCX measurement this
project has taken:

| device isolation | ucp_put_bw, RMA_PPLN=y |
|---|---|
| `HIP_VISIBLE_DEVICES=0` / `=1` | 586 GB/s |
| `ROCR_VISIBLE_DEVICES=0` / `=1` | **43.8 GB/s** |

`HIP_VISIBLE_DEVICES` masks devices for the HIP runtime. UCX does not use the
HIP runtime -- it talks to ROCr/HSA directly, and the HSA agent list is not
filtered by that variable. So both UCX ranks allocated on physical GPU 0 and the
"GPU 0 to GPU 1" measurement was GPU 0 to GPU 0.

Consequences, in order of how much they matter:

- **Session 1's 511 GB/s `tag_bw` figure is void.** So is the reasoning built on
  it ("rocm_ipc is neither broken nor slow -- the rendezvous path uses it at full
  speed"). The conclusion happens to be right, but 511 GB/s was a same-device
  copy and should never have been quoted.
- **The real result is better, not worse.** `UCX_RMA_PPLN_ENABLE=y` takes
  GPU-to-GPU RMA put from 0.8 GB/s to 43.8 GB/s. That is 55x, and it is 96% of
  the 45.7 GB/s hardware ceiling. A number that lands just under ground truth is
  worth far more than one that sails past it.
- **MORI's 46.7 GB/s was right all along.** It is at the peer ceiling, which is
  exactly what a direct `hipMemcpyAsync` over IPC should give. MORI was never
  beating the fabric; NIXL was failing to reach it.
- `run-nixlbench.sh` had the same bug. It now sets both variables, with the
  reason written down next to them.

Redoing the flag bisect with correct isolation before drawing any conclusion
from it.

### 2026-08-13 01:20 — the fix, and it is two lines

With correct device isolation, `ucx_perftest -t ucp_put_bw -m rocm,rocm`,
GPU 0 to GPU 1, 8 MiB:

| ROCM_IPC_EXP | RMA_PPLN=n | RMA_PPLN=y |
|---|---|---|
| (none) | 0.8 | **43.7** |
| e / ir / ire / ira / irk / irake | 0.8 | 43.5-44.1 |
| irkc / irakec | 0.8 | **SEGFAULT** |

Every subset performs identically once the gate is on, including the empty one.
So for `ucx_perftest` the capability flags do nothing and `UCX_RMA_PPLN_ENABLE=y`
is the whole fix.

For NIXL it is the other way round, which is the part that took the longest to
see. `nixlbench --backend UCX`, VRAM, all with `RMA_PPLN=y`:

| ROCM_IPC_EXP | peak GB/s |
|---|---|
| (none) | 0.6 |
| `ir` (MD invalidate flags) | 0.4 |
| `k` (MD RKEY_PTR) | 0.6 |
| **`e` (iface ERRHANDLE_PEER_FAILURE)** | **47.7** |
| `ie` / `re` / `ire` / `irae` / `irke` / `irake` | 47.1-47.9 |

`e` is necessary and sufficient. Nothing else contributes. NIXL needs it because
it creates endpoints with `UCP_ERR_HANDLING_MODE_PEER`, and UCP drops any iface
without that flag before protocol selection runs; `ucx_perftest` does not request
peer error handling, so it never hits the filter.

**So the patch series is two changes, each about one line:**

1. `patches/ucx/01-ucp-enable-rma-rndv-for-device-memory.patch` — flip
   `UCX_RMA_PPLN_ENABLE` to default `y` and drop the `UCS_CPU_MODEL_NVIDIA_VERA`
   special case it subsumes. The RMA rendezvous protocol is the only RMA protocol
   that can drive a zcopy-only transport like rocm_ipc or cuda_ipc, and it was
   disabled on every CPU except one NVIDIA model.
2. `patches/ucx/02-rocm-ipc-errhandle-peer-failure.patch` — advertise
   `UCT_IFACE_FLAG_ERRHANDLE_PEER_FAILURE` on rocm_ipc, matching cuda_ipc.

Neither works without the other, which is exactly why this took two sessions.
Patch 02 alone is what [ai-dynamo/nixl#2039](https://github.com/ai-dynamo/nixl/issues/2039)
proposed, and session 1 tested it alone and reported it did not help. That was a
correct measurement of an incomplete fix.

The old `01-rocm-ipc-cuda-ipc-capability-parity.patch.master-only` is retired: of
its six flags, five measurably do nothing and the sixth
(`UCT_COMPONENT_FLAG_RKEY_PTR`) segfaults.

### 2026-08-13 01:25 — device isolation, third time

Getting the two ranks onto two GPUs took three attempts, and the two failures
are worth writing down because both produced plausible numbers rather than
errors.

| isolation | UCX | MORI_IO | verdict |
|---|---|---|---|
| `HIP_VISIBLE_DEVICES` only | 586 GB/s | 46.7 | UCX both ranks on GPU 0 |
| `ROCR_VISIBLE_DEVICES` only | 43.8 | 16.9 | MORI's peer is hidden from HSA |
| `ROCR=0,1` both ranks + `HIP=0` / `HIP=1` | 47.3 | 46.8 | both correct |

The middle row is the subtle one: masking HSA down to a single agent also hides
the *peer*, so MORI's IPC path has nothing to map and falls back. Showing both
GPUs to HSA and letting HIP choose which one each rank allocates on satisfies
both stacks. Ground truth at 64 MiB is 48.8 GB/s, and both backends now land
just under it, which is the shape a correct measurement should have.

### 2026-08-13 04:15 — the shipped patches, verified with nothing in the environment

Rebuilt against openucx master with both patches applied for real (no
`ROCM_IPC_EXP`, no `UCX_RMA_PPLN_ENABLE`, release-configured UCX) and tagged it
`nixl-mori-test:fixed`. The control is `nixl-mori-test:bisect`, the runtime-gated
build, which with no variables set behaves exactly like stock upstream.

`make bench BACKEND=UCX SEG_TYPE=VRAM`, GPU 0 to GPU 1, no UCX variables set:

| block | stock | patched |
|---|---|---|
| 4 KiB | 0.12 | 0.15 |
| 1 MiB | 0.44 | 24.1 |
| 64 MiB | 0.45 | **47.7** |

MORI_IO on the same node and run: 46.98 GB/s at 64 MiB. Ground truth
(`hipMemcpyPeer`, 64 MiB) is 48.8. So NIXL goes from **1% of the hardware to 98%
of it**, and from 100x behind MORI to level with it, with two one-line changes
and nothing set in the environment.

Both stock rows above needed `UCX_TLS` restricted to get a number at all, for a
reason that has nothing to do with these patches — see the next entry.

### 2026-08-13 04:20 — with default UCX_TLS, VRAM does not run on this node at all

Independent of the patches, and present on the stock image too:

```
ib_md.c:316 UCX ERROR ibv_reg_mr(address=0x74bf80000000, length=2147483648,
                                 access=0x10000f) failed: Bad address
ucp_mm.c:84 UCX ERROR failed to register address ... (rocm) length 8589934592
                      on md[4]=mlx5_1: Input/output error (md supports: host|rocm)
nixl_agent.cpp:468 registerMem: registration failed for all potential backends
```

`mlx5_1` claims to support `rocm` memory, NIXL takes it at its word and asks it
to register a VRAM buffer, `ibv_reg_mr` returns `EFAULT`, and NIXL treats a
single MD failure as total failure and aborts the run. So on this node **any
UCX VRAM transfer fails unless `UCX_TLS` excludes the IB transports**, whether or
not the intra-node fix is in.

This is why every measurement in this session pins `UCX_TLS`. It is a real
second bug and it is not the one being fixed here, so it is recorded and left:
the MD advertises a capability the device cannot deliver (no peer-memory /
dmabuf path for ROCm on this HCA), and NIXL's all-or-nothing registration turns
a should-be-skippable MD into a fatal error.

### 2026-08-13 04:45 — the performance table

`make bench`, one initiator and one target on `ctr-smc-mi300x-cx68-25`, GPU 0 to
GPU 1, batch 1, 1008 iterations, `UCX_TLS` pinned away from IB for the reason in
the previous entry. Raw output in `logs/perf/`.

**VRAM to VRAM, GB/s:**

| block | UCX stock | UCX patched | MORI_IO | patched vs stock |
|---|---|---|---|---|
| 4 KiB | 0.11 | 0.12 | 0.11 | 1.1x |
| 16 KiB | 0.26 | 0.48 | 0.41 | 1.8x |
| 64 KiB | 0.38 | 1.84 | 1.55 | 4.8x |
| 256 KiB | 0.42 | 6.81 | 5.60 | 16x |
| 1 MiB | 0.33 | 19.00 | 16.30 | 57x |
| 4 MiB | 0.33 | 34.91 | 27.16 | 105x |
| 16 MiB | 0.33 | 43.71 | 37.52 | 132x |
| 64 MiB | 0.33 | **47.67** | 46.62 | **143x** |

Ground truth for this pair is 48.8 GB/s. Patched UCX reaches 97.7% of it.

Three things in that table are worth saying out loud:

- Stock UCX **flattens at 0.33 GB/s** from 512 KiB up. It is not slow in
  proportion to anything; it has fallen off the transport onto a software
  emulation path whose cost is independent of how fast the hardware is.
- Patched UCX is **ahead of MORI_IO at every size from 16 KiB up**, by 20-30% in
  the middle of the range. NIXL/UCX is not merely catching up to the specialised
  backend here, it is beating it, and the gap is largest exactly where a
  disaggregated-inference KV transfer lives (256 KiB to 4 MiB).
- MORI_IO's 2 MiB point (6.85 GB/s) is a reproducible dip, not noise -- it shows
  up in every MORI run and its P99 post time is 11.5 ms against a 35 us average.
  Something in MORI's path stalls at exactly that size. Not chased; noted.

**DRAM to DRAM, GB/s** (the control -- this path must not change):

| block | UCX stock | UCX patched |
|---|---|---|
| 1 MiB | 40.82 | 40.94 |
| 16 MiB | 46.38 | 46.35 |
| 64 MiB | 46.827 | 46.827 |

Identical to three digits at the top end. That is the intended result: patch 01's
`probe_check` still ends in `return !UCP_MEM_IS_HOST(sg_mem_info) || !UCP_MEM_IS_HOST(...)`,
so enabling the gate cannot pull a host-to-host transfer onto the rendezvous
path. The host case was never broken and is not touched.

**MORI_IO on DRAM does not run on this node:**

```
ibv_create_qp failed: errno=25 (Inappropriate ioctl for device); dev=mlx5_1
MORI_IO: batch write failed
```

MORI's host-memory path goes to RDMA rather than IPC, and QP creation fails in
this container. So the DRAM column has no MORI comparison. Unrelated to this
work, recorded for whoever needs a DRAM number from MORI.

## AIS_MT and NVMe

### 2026-08-13 05:10 — what each storage backend will and will not accept

Before any matrix, the axes had to be trimmed, because two of them are not free
variables:

| backend | VRAM | DRAM |
|---|---|---|
| AIS_MT | works | `hipFileBufRegister failed (err=5013); set HIPFILE_ALLOW_COMPAT_MODE=true to allow fallback` |
| POSIX (AIO, URING, POSIXAIO) | `registerMem: registration failed` | works |

So AIS_MT is VRAM-only and POSIX is DRAM-only, and "AIS_MT vs POSIX" is
unavoidably "GPU-direct out of VRAM vs host I/O out of DRAM". **The POSIX column
is the optimistic one**: a real KV-cache offload has its data in VRAM, and to use
POSIX it would first have to copy it to host memory. That copy is not in any
POSIX number below.

Two more nixlbench facts that shape the matrix:

- Storage backends run as **one** process, not a pair -- "Using null runtime for
  storage backend without ETCD ... expecting 1 total". The file is the far end.
  Driving them the way `make bench` drives UCX puts two processes on the same
  file and the loser dies in `registerMem`.
- `--num_threads` must be **>= `--num_files`**: nixlbench allocates one buffer
  per thread and refuses to start otherwise.

### 2026-08-13 05:30 — one drive tells you nothing

Session 1's single measurement (one drive, one thread, WRITE) is reproduced
exactly, and it is a measurement of the drive:

| threads | AIS_MT | POSIX/AIO | POSIX/URING |
|---|---|---|---|
| 1 | 7.19 | 7.26 | 7.26 |
| 2 | 7.23 | 7.26 | 7.26 |
| 4 | 7.24 | 7.26 | 7.26 |
| 8 | 7.21 | 7.25 | 7.27 |
| 16 | 7.24 | 7.25 | 7.26 |

Every path saturates a single KIOXIA at ~7.26 GB/s with **one** thread, and 16x
the threads buys 0%. Both backends' submit paths are far from being the
bottleneck at this scale, which is why session 1's "the difference is submit
cost" could not be seen: there was nothing to see.

READ on one drive is roughly 2x WRITE, and again backend-independent:

| op | AIS_MT | POSIX/AIO | POSIX/URING | POSIX/POSIXAIO |
|---|---|---|---|---|
| WRITE | 7.21 | 7.26 | 7.25 | 7.26 |
| READ | 13.68 | 13.79 | 13.52 | 13.63 |

### 2026-08-13 06:05 — sixteen drives, and the two backends separate completely

This is the result worth having. One file per distinct block device across
`/mnt/nixl-nvme-0..15` (all KIOXIA, `nvme0n1`..`nvme16n1` less the Micron):

**WRITE, GB/s:**

| drives | AIS_MT (VRAM) | POSIX/AIO (DRAM) | POSIX/URING (DRAM) |
|---|---|---|---|
| 1 | 7.25 | 7.26 | 7.24 |
| 2 | 14.54 | 14.38 | 14.41 |
| 4 | 29.01 | 28.00 | 27.95 |
| 8 | 55.26 | 48.11 | 47.30 |
| 16 | **56.51** | 23.05 | 24.59 |

**READ, GB/s:**

| drives | AIS_MT (VRAM) | POSIX/AIO (DRAM) |
|---|---|---|
| 1 | 14.07 | 14.28 |
| 4 | 55.36 | 32.91 |
| 8 | 55.31 | 35.71 |
| 16 | **56.40** | 25.21 |

Two separate things are happening in those tables.

**AIS_MT plateaus at ~56 GB/s and stays there.** It reaches the plateau at 4
drives on READ and 8 on WRITE, and 16 drives adds nothing. 56 GB/s is about 88%
of a PCIe Gen5 x16 link, which is what one MI300X has, so the most likely reading
is that AIS_MT is limited by the GPU's own link rather than by the drives, the
CPU, or NIXL. Confirming that needs a two-GPU run and is the obvious next test.

**POSIX does not plateau, it regresses.** 48 GB/s at 8 drives, 23 GB/s at 16.
That is not a ceiling, it is a collapse, and the `wide` set isolates the cause to
thread count rather than to the second eight drives:

| config | POSIX/AIO | AIS_MT |
|---|---|---|
| 8 drives, 8 threads | 48.11 | 55.26 |
| 8 drives, 16 threads | **25.16** | 55.65 |
| 16 drives, 16 threads | 23.05 | 56.51 |
| 16 drives, 32 threads | 22.37 | 56.38 |

Same eight drives, same data, 8 threads to 16 threads: POSIX halves. AIS_MT does
not move at all across a 4x range of thread counts. So POSIX's degradation is in
its own submit path -- it is oversubscribing something past 8 threads -- and
since `--num_threads` must be at least `--num_files`, **POSIX cannot address more
than 8 drives without paying that penalty.** That is a structural limit on the
POSIX backend for wide NVMe arrays, not a tuning problem.

Net: at the scale this node actually has, AIS_MT delivers **2.4x the write
bandwidth and 2.2x the read bandwidth** of the POSIX backend, and it does it
directly out of VRAM while POSIX's numbers still owe a device-to-host copy.
Session 1's "both saturate the drive, the difference is submit cost" was right
about the mechanism and wrong about the magnitude, because one drive is not
enough load to expose it.

### 2026-08-13 06:15 — O_DIRECT does not appear to do anything

| direct | AIS_MT | POSIX/AIO |
|---|---|---|
| on | 29.23 | 27.86 |
| off | 28.97 | 27.86 |

4 drives, 8 threads, WRITE. POSIX/AIO is identical to two decimals with buffered
I/O and with `O_DIRECT`, which is not what buffered I/O normally looks like --
page cache usually shows up as a large apparent speedup at small sizes. Either
`--storage_enable_direct` is not reaching `open()`, or these writes bypass the
cache for another reason. Flagged rather than concluded; it needs an `strace`
on the `open()` flags to settle, and it does not affect any comparison above
because every row in them has direct on.

### 2026-08-13 06:40 — the 56 GB/s ceiling is the GPU, and two GPUs double it

The plateau hypothesis was testable in one run: two nixlbench processes, one per
GPU, eight distinct drives each, at the same time.

| | GPU 0 | GPU 1 | aggregate |
|---|---|---|---|
| one GPU, 8 drives | 53.84 | — | 53.8 |
| two GPUs, 8 drives each | 53.61 | 52.53 | **106.1** |

Adding a second GPU costs the first 0.4% and doubles the total. So the ~56 GB/s
is **per-GPU**, consistent with a PCIe Gen5 x16 link, and neither the drives nor
NIXL nor the AIS_MT submit path is the limit at this scale. Sixteen drives at
7.26 GB/s each is ~116 GB/s of raw array bandwidth and two GPUs get 91% of it.

This also settles the earlier POSIX comparison in AIS_MT's favour more strongly
than the single-GPU table did: POSIX's ceiling is a software one that gets worse
with scale, AIS_MT's is a link that you can add more of.

## Where this leaves things

Done:

- `patches/ucx/01-ucp-enable-rma-rndv-for-device-memory.patch` and
  `02-rocm-ipc-errhandle-peer-failure.patch`, both applying cleanly to openucx
  master (`make patch-check COMPONENT=ucx`), both verified in a clean build with
  no environment gating.
- `patches/ucx/README.md` rewritten around the real diagnosis.
- `docker/scripts/storage-sweep.sh` plus `make storage-sweep SWEEP_SET=...`
  (`quick`, `rw`, `threads`, `drives`, `wide`, `readscale`, `direct`, `full`),
  CSV per set in `logs/`.
- `docker/scripts/p2p-truth.hip.cpp`, a hardware ground-truth check, because two
  of this project's headline numbers were wrong for want of one.
- `make bench BENCH_ENV="VAR=value ..."`, since UCX reads its configuration from
  the benchmark process's environment and setting it outside `docker run` does
  nothing.

Worth doing next, roughly in order of value:

1. **Upstream patch 01.** It is not ROCm-specific: `cuda_ipc` is zcopy-only too,
   so every CUDA user outside NVIDIA Vera has the same gap on `ucp_put`/`ucp_get`
   for device memory. This is the finding with reach beyond this node.
2. **Two-node RDMA.** Everything here is intra-node. The inter-node path is
   untouched by these patches and unmeasured this session.
3. **The `ibv_reg_mr` EFAULT on ROCm memory**, which makes UCX VRAM unusable on
   this node with the default `UCX_TLS`. Independent of this work and arguably
   more disruptive, since it fails the run outright rather than making it slow.
4. **POSIX's thread-count collapse past 8**, if the POSIX backend matters to
   anyone; AIS_MT makes it mostly moot for GPU workloads.
5. **`--storage_enable_direct` may be a no-op** — buffered and direct give
   identical numbers.
