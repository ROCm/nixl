# Overnight work log

Unattended session, evening of 2026-08-11. Newest entries at the bottom.
Read "Where things stand" first, then "Open items" for what to pick up.

## Goal

1. ~~Docker + Makefile scaffolding to build NIXL and MORI from tagged releases
   plus patches, rocm-aic-style layout.~~ **done**
2. ~~Move the builds off the login node onto a CPU-only Slurm node.~~ **done**
3. ~~Get **nixlbench** driving **both NIXL and MORI**.~~ **done** — both run
   through the same harness, one `--backend` flag apart. The numbers taken so
   far are only a plumbing check; see the caveat below.

## Where things stand

| Item | Status |
|---|---|
| Tree layout (Makefile / docker/ / patches/ / .slurm/) | done |
| `make patch-check` — dry-run patchsets vs pinned tags | done, both pass and fail paths verified |
| UCX (ROCm/ucx v1.19.x) built with HIP support | done |
| MORI v1.2.2 wheel + C++ SDK (`/opt/mori`) | done |
| NIXL v1.3.2 (ROCm variant, `nixl_rocm` bindings) | done |
| nixlbench built for ROCm (`-Duse_rocm=true`) | done |
| **nixlbench driving NIXL/UCX** | **done — full result table, exit 0** |
| NIXL `MORI_IO` backend plugin (new, in `patches/nixl/`) | done — builds, loads, transfers |
| nixlbench accepting `--backend MORI_IO` / `AIS_MT` | done (patch 04) |
| NIXL AIS_MT (GPU-direct NVMe, hipFile) | done — saturates the drive |
| hsa-snoop in the image (AIS + GPU counters) | done — needs `--privileged` |
| **nixlbench driving MORI end to end** | **done — full result table, exit 0** |
| Slurm CPU-only build + tarball distribution | done — 34 GB tarball on /scratch |
| `make dist-build-here` (build on the bench node) | done — ~13 min, no tarball |
| `make dist-bench` (GPU-node benchmark) | done on the storage partition |
| `make dist-bench-2node` | **never executed** — storage partition is one node |

### Results on real hardware (storage partition, 1 node, 8x MI300X + CX7)

All from `ctr-smc-mi300x-cx68-25` via the `storage` partition, which unlike
`defq` is idle and schedulable immediately. Build there too — see "Where to
build".

| Path | Backend | Result |
|---|---|---|
| GPU 0 -> GPU 1, VRAM | MORI_IO | **46.7 GB/s** @ 64 MiB |
| GPU 0 -> GPU 1, VRAM | NIXL/UCX | 0.69 GB/s — see "UCX has no RMA over rocm_ipc" |
| host DRAM | NIXL/UCX | **46.0 GB/s** @ 16 MiB |
| host DRAM | MORI_IO | fails — `ibv_create_qp: errno=25` |
| NVMe, host-staged | NIXL/POSIX (AIO, O_DIRECT) | **7.26 GB/s**, 212 us submit |
| NVMe, GPU-direct | NIXL/AIS_MT (VRAM) | **7.14 GB/s**, **4.8 us submit** |
| NVMe, GPU-direct | MORI | not available (UMBP/SPDK not built) |

AIS_MT and POSIX both saturate the drive; the difference is submit cost --
4.8 us against 212 us at 16 MiB, ~44x less CPU-side work for the same
bandwidth, which is the point of the GPU-direct path.

> An earlier version of this file reported AIS_MT at 1.1-1.7 GB/s. That was
> wrong: `run-nixlbench.sh` did not have AIS_MT in its storage-backend list, so
> `--filepath` was dropped and nixlbench wrote its test file to the container
> working directory instead of the NVMe mount. Watch for the
> `storage backend <NAME>: filepath=...` line, which now prints for every
> storage run precisely so this is visible.

### UCX will not use rocm_ipc for RMA — why NIXL loses intra-node

Intra-node GPU-to-GPU needs no NIC: `rocm_ipc` uses HIP IPC handles. peermem
and dmabuf concern the NIC path and are irrelevant here. The cause shows up in
bare `ucx_perftest`, no NIXL, with `rocm_ipc` explicitly in `UCX_TLS`:

```
perftest intra-node cfg#1  tag(sysv/memory cma/memory rocm_ipc/rocm_ipc)  rma(sysv/memory posix/memory)
```

UCX offers `rocm_ipc` on the **tag** (rendezvous) lane -- ~511 GB/s GPU-to-GPU
there -- but never on the **rma** lane, despite `rocm_ipc` advertising
`put_zcopy`/`get_zcopy`. NIXL's UCX backend is RMA-based, so it cannot reach it
and stages GPU -> host shared memory -> GPU at ~0.69 GB/s.

Two filters were identified, and neither fully explains it:

1. **NIXL's error-handling mode.** NIXL defaults to
   `UCP_ERR_HANDLING_MODE_PEER`, and UCX drops transports that cannot do peer
   error handling -- `rocm_ipc` reports "error handling: none", as do the
   shared-memory transports. Patch 04 adds `--ucx_error_handling_mode` to
   nixlbench; setting it to `none` visibly brings `sysv/posix` back into the
   RMA lane, so the filter is real. It does not bring back `rocm_ipc`.

2. **The proposed UCX capability flag
   ([ai-dynamo/nixl#2039](https://github.com/ai-dynamo/nixl/issues/2039)).**
   Adding `UCT_IFACE_FLAG_ERRHANDLE_PEER_FAILURE` to `rocm_ipc` fixed this on
   Radeon PRO W7900 (gfx1100) for the reporter. **It does not fix it here.**
   Tested on ROCm/ucx v1.19.x and on openucx master 1.23.0 (which already
   carries openucx/ucx#11299), with the flag verified live via `ucx_info -d`
   showing `error handling: peer failure` -- NIXL still gets
   `rma_am(tcp)` at ~0.33 GB/s. The patch is parked, disabled and documented,
   in `patches/ucx/`.

The untested variable is NIXL itself: the reporter runs NIXL 1.4, which is
unreleased `main` (no v1.4 tag exists) and carries the explicit ROCm VRAM
memtype hint (#1536). This tree pins v1.3.2. NIXL `main` + the flag on gfx942
is the next experiment.

MORI is unaffected: it does its own XGMI/IPC and never asks UCX. That is the
whole reason MORI_IO gets 46.7 GB/s here and NIXL/UCX gets 0.69.

### The NIC path: peermem is NOT the only option, but neither works here

Worth recording since it came up. For GPU memory over the NIC there are two
mechanisms: the legacy peer-memory API, and DMA-BUF, which is the modern
in-kernel one and needs no peermem. On this node:

- UCX is built for both (`HAVE_DECL_IBV_REG_DMABUF_MR`,
  `HAVE_HSA_AMD_PORTABLE_EXPORT_DMABUF`) and the mlx5 domain advertises
  `register: unlimited, dmabuf`.
- The legacy path fails: no peermem module, so `ibv_reg_mr(...)` returns
  `Invalid argument` for ROCm memory (any size; 256 MiB fails as readily as
  8 GiB). Host memory registers fine.
- The dmabuf path fails too: `UCX_ROCM_COPY_DMABUF=y` gives
  `rocm_copy_md.c ERROR ROCm dmabuf support requested but not found` -- the
  ROCm stack does not offer the export. The host amdgpu driver is **7.1.3**
  while the container ships ROCm **7.14.0** userspace, which is the obvious
  suspect.

This matters for cross-node RDMA to GPU memory. It does not explain the
intra-node result above.

### hsa-snoop

Built into the image (`/opt/hsa-snoop/bin/hsa-snoop`, on PATH) with the
Prometheus exporter, so a run can report what the GPU and the AIS path did,
not just the end-to-end number:

```bash
make bench BACKEND=AIS_MT SEG_TYPE=VRAM FILEPATH=/mnt/nixl-nvme-0/x HSA_SNOOP=1
```

It needs `--privileged` and `--pid=host` -- CAP_SYS_ADMIN plus a tracefs mount
is not enough, and the failure is quiet: it reports "failed to install kprobe"
and then serves an empty `/metrics`, which reads as "no activity" rather than
as an error. The make target passes the right flags.

It also cross-checks the backends: during the AIS_MT run it shows active
queues, barrier packets and kernel durations per GPU; during the POSIX run it
shows only `hsa_snoop_up`, because POSIX never touches the GPU.

## Open items (in the order I would do them)

1. **Get GPU peer memory working on the node**, or find a node that has it.
   Until then NIXL has no usable GPU path here and neither UCX-vs-MORI on
   VRAM nor AIS_MT-vs-POSIX on NVMe is a fair comparison. This is the single
   highest-value thing outstanding.
2. **Raise the MORI `ibv_create_qp` failure** with the MORI team — two-line
   repro, independent of NIXL, and it is what blocks MORI on host memory.
3. **Test NIXL `main` + the rocm_ipc peer-failure flag on gfx942** — see
   `patches/ucx/README.md`. It is the one untried combination that might close
   the intra-node gap, and it would be a useful second data point for
   ai-dynamo/nixl#2039, which so far only has gfx1100 evidence.
4. **`make dist-bench-2node` has still never run** — the storage partition is
   a single node, so the cross-node RDMA path remains untested.
5. Consider whether `mori_backends=auto` should prefer RDMA over XGMI when
   both exist. On a node with both, a benchmark meaning to measure RDMA
   should pass `mori_backends=rdma` explicitly — that makes it required, so
   it fails loudly instead of quietly measuring XGMI.

## Environment bugs found and fixed along the way

None of these are in NIXL or MORI source; all are fixed in-tree.

1. **ROCm 7.14.0 ships a broken `hsakmtTargets.cmake`.** Its
   `INTERFACE_LINK_LIBRARIES` still contains the packager's own build-host
   paths, including `/usr/lib64/libc.so` (an RHEL path that does not exist on
   Ubuntu). Because that is an explicit file rather than a `-l` flag, CMake
   turns it into a build-graph dependency, so every `find_package(hsakmt)`
   consumer dies with `ninja: error: '/usr/lib64/libc.so' ... missing and no
   known rule to make it`. MORI hits it via `src/application`.
   → `docker/scripts/fix-rocm-cmake.sh`, run in the base stage. Idempotent,
   no-op on a fixed ROCm. Also added `libdrm-dev` so the `-ldrm` in the same
   (otherwise bogus) link line resolves from the system.

2. **ROCm 7.14.0 registers nothing with ldconfig.** `ldconfig -p | grep
   amdhip64` is empty in `rocm/dev-ubuntu-24.04:7.14.0-full`, so anything
   linked against `libamdhip64` — nixlbench built with `-Duse_rocm=true`, the
   MORI libs — fails to load unless the caller sets `LD_LIBRARY_PATH`.
   → base stage writes `/etc/ld.so.conf.d/rocm.conf` and asserts the cache
   picked it up.

3. **MORI's wheel needs setuptools in a narrow window.** `>= 77` because its
   `pyproject.toml` uses the PEP 639 string form (`license = "MIT"`), which
   older setuptools rejects outright; `< 80` because setuptools 80 added
   `assert isinstance(self.compiler, CCompiler)` to distutils' build_extension
   and Cython's `build_ext` (which builds `mori.cco.cco`) still passes the
   legacy compiler string, so the build dies on a bare `AssertionError`.
   MORI's pyproject only asks for `setuptools>=61`, so pip's build isolation
   installs the latest and it breaks.
   → `build-mori.sh` installs a pinned build env and uses
   `--no-build-isolation`. Override with `MORI_BUILD_DEPS`.

4. **gRPC/abseil for MORI breaks the NIXL build.** Installing `libgrpc-dev`
   (per MORI's own dev Dockerfile) drags in Ubuntu's `libabsl`; NIXL then
   finds `absl_base`, sees no `absl_log`, and refuses to fall back to its own
   Abseil subproject — a hard error. MORI only needs gRPC for UMBP, which is
   off by default.
   → gRPC moved out of the shared base into the mori stage, installed only
   when `MORI_BUILD_UMBP=ON`.

5. **MORI's public headers include submodule headers it does not install.**
   `mori/io/common.hpp` → `<msgpack.hpp>`, `mori/utils/mori_log.hpp` →
   `<spdlog/...>`. So nothing outside MORI's own build tree can compile
   against the installed SDK.
   → `build-mori.sh` stages `3rdparty/{msgpack-c,spdlog}/include` into
   `/opt/mori/include`. The consumer also has to match MORI's compile flags:
   `MSGPACK_NO_BOOST` (else msgpack reaches for `<boost/predef/...>`) and
   `SPDLOG_COMPILED_LIB` + linking MORI's own `libspdlog.a` (MORI builds
   spdlog compiled with hidden visibility precisely to avoid two copies).

6. **HIP and toml++ collide in nixlbench.** ROCm's `host_defines.h` defines
   `__noinline__` as an object-like macro; toml++ 3.4.0 evaluates
   `#if TOML_GCC || TOML_CLANG || TOML_HAS_ATTR(__noinline__)` and the
   preprocessor expands the whole expression before it can short-circuit, so
   the ROCm nixlbench build fails with `macro "__has_attribute" requires an
   identifier`.
   → `patches/nixl/02-nixlbench-rocm-tomlplusplus-noinline.patch`
   (push/undef/pop around the include). Upstream-worthy as-is.

7. **`nixlbench --backend` is not a free-form string.** I initially assumed a
   NIXL plugin named MORI_IO would be selectable with no nixlbench change.
   Wrong: `nixl_worker.cpp` gates the flag on a hardcoded list and exits with
   "Unsupported NIXLBench backend" even when the agent can see the plugin.
   → `patches/nixl/04-nixlbench-extra-backends.patch`.

8. **`nixlbench --help` exits 1** (gflags convention), so any check of the form
   `nixlbench --help && echo ok` silently never fires. Checks now assert on
   output content.

9. **The ASIO rendezvous port is a live hazard.** nixlbench's socket runtime
   picks initiator/target by "first to bind wins". With `--network=host` on
   this login node, something already holds `127.0.0.1:12345`, so *both* ranks
   failed to bind, *both* fell back to `connect()`, both decided they were the
   target, and the run died on "ASIO Receive timeout" with no useful message.
   → `run-nixlbench.sh` probes for a bindable port first and says so.

## Patches now in the tree

`patches/nixl/` (applied in lexical order to `ai-dynamo/nixl` v1.3.2):

| Patch | What |
|---|---|
| `01-nixl-mori-io-backend.patch` | New `src/plugins/mori_io/` — a NIXL backend engine on top of MORI-IO, plus the meson wiring (`-Dmori_path=`). ~900 lines, mostly new files. |
| `02-nixlbench-rocm-tomlplusplus-noinline.patch` | The HIP/toml++ `__noinline__` collision above. |
| `04-nixlbench-extra-backends.patch` | Teach nixlbench's `--backend` about MORI_IO. |

`patches/mori/` is still empty — MORI v1.2.2 builds pristine.

**Patch 01 is developed as a real checkout, not by editing the .patch.**
`patches/nixl/regen-mori-io-patch.sh <work-tree>` re-exports it with its
comment header intact. Note patches 02/03 touch the same file
(`benchmark/nixlbench/src/utils/utils.h`), so 03 must be diffed against a tree
that already has 01+02 applied — I got that wrong once and `make patch-check`
caught it.

### How the plugin maps NIXL onto MORI

| NIXL | MORI-IO |
|---|---|
| agent | `IOEngine`, keyed by the NIXL agent name |
| `getConnInfo` | msgpack(`EngineDesc`) |
| `loadRemoteConnInfo` | `RegisterRemoteEngine` |
| `registerMem` | `RegisterMemory` → `MemoryDesc` |
| `getPublicData` | msgpack(`MemoryDesc`) |
| `postXfer` WRITE/READ | `BatchWrite` / `BatchRead` |
| `checkXfer` | poll `TransferStatus` (`WaitFor(0)`) |
| `getNotifs` / `genNotif` | nothing in MORI — the plugin carries its own TCP channel |

Two details worth remembering: MORI addresses memory as
`(MemoryDesc, offset)` while NIXL descriptors are absolute, so the plugin
keeps each registration's base and range-checks every descriptor against it;
and MORI's batch API is one level deeper than NIXL's (a vector of descriptor
*pairs*, each with its own offset/size vector, one `TransferStatus` per pair),
so consecutive NIXL ranges sharing a registration pair are folded into one
MORI entry.

## Where to build

`make dist-build-here` builds ON the benchmark node (the storage partition's
MI300X box, 384 cores) and skips the tarball entirely: ~13 minutes, and the
image is already where it is needed.

`make dist-build` is the other path — build on a CPUONLY node, `docker save` to
/scratch, `make dist-load NM_TARGETS=<node>` on each target. Use it only when
the image has to reach more than one node: the round trip is 68 GB of shared
filesystem traffic and adds ~35 minutes per iteration.

## Building off the login node

```bash
make dist-build                      # build on a CPUONLY node, save to /scratch
make dist-load NM_TARGETS=<node>     # docker load it there
```

`.slurm/run-build.sh` submits via `sbatch --wait` and streams the log back into
`logs/`. Defaults: partition `am`, constraint `CPUONLY`, 32 CPUs, 64G, 3h,
tarball under `/scratch/$USER/nixl-mori-images/`. `NM_ROCM_ARCH` defaults to
`gfx942` and matters — a CPU node has no GPU to autodetect from, and the value
is baked into MORI's `GPU_TARGETS`. The Makefile deliberately does *not*
forward the login node's autodetected arch (it detects `gfx90a` here, which is
not what anyone wants to ship).

First run on a fresh node spends ~10 minutes pulling and extracting the 20 GB
ROCm base image before anything else happens; subsequent runs are minutes.

## Log

### 2026-08-11 evening — scaffolding
- rocm-aic layout: top-level `Makefile`, `docker/` with one multi-stage
  Dockerfile + `docker/scripts/`, `patches/<component>/`.
- Stage graph `base -> ucx -> nixl`, `base -> mori`, joined in `runtime`.
  Later the `mori -> nixl` edge was added for the MORI C++ SDK, which is why
  the mori stage sits before nixl in the file.
- `make patch-check` verified by generating a probe patch (reports OK) and
  then mangling it (reports FAIL with the `git apply` diagnostic). It also
  turned out to have a real bug — it reset tracked files but left files a
  patch *created*, so a second run of a file-adding patch reported a bogus
  "already exists" FAIL. Fixed with `git clean`.

### 2026-08-11 late — builds green
- Fixed environment bugs 1–5 above; full image builds and smoke-tests clean.
- `make test` passes on the login node.

### 2026-08-11 late — nixlbench
- nixlbench built for ROCm; UCX pair runs end to end.
- MORI_IO plugin written, built, loaded, selected.
- Hit NIXL's `supportsRemote() => supportsNotif()` rule. MORI has no
  arbitrary-message channel (`PopInboundTransferStatus` is a per-transfer
  completion signal, not a payload channel), so the plugin grew its own: a
  per-engine TCP listener on an ephemeral port, advertised inside `getConnInfo`
  next to the msgpack'd `EngineDesc`, length-prefixed `[agent][payload]`
  frames, one reader thread. `genNotif` sends, `getNotifs` drains.
  `postXfer` stores the message and `checkXfer` sends it once on the
  transition to complete — NIXL's semantics are "the target learns after the
  data lands", so sending at post time would be wrong.
- With that, `--backend MORI_IO` completes the full nixlbench sweep on VRAM.


### 2026-08-11 end of session — wrap-up
- Notif channel implemented; `--backend MORI_IO` completes the full sweep.
- Slurm CPU-only build ran green end to end on `ctr-smc-s22-17` and saved a
  34 GB tarball to `/scratch/stebates/nixl-mori-images/`.
- `.slurm/run-bench.sh` + `make dist-bench` / `dist-bench-2node` written;
  could not be executed (GPU queue ~13 days out).
- Final state verified: all shell scripts parse, `docker buildx build --check`
  is clean, all three patches apply to the pinned tag, `make build` succeeds,
  `make test` passes, `make bench-compare` completes both backends with exit 0.
- Nothing is committed to git — the tree is all working-copy changes.
