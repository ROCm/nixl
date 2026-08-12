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
| nixlbench accepting `--backend MORI_IO` | done (patch 03) |
| **nixlbench driving MORI end to end** | **done — full result table, exit 0** |
| Slurm CPU-only build + tarball distribution | done — 34 GB tarball on /scratch |
| `make dist-bench` (GPU-node benchmark) | written and syntax-clean, **never executed** — see below |

### Both backends run end to end

The goal is met: one harness, one flag apart.

```bash
make bench BACKEND=UCX     SEG_TYPE=VRAM
make bench BACKEND=MORI_IO SEG_TYPE=VRAM
make bench-compare                          # both, back to back
```

Both exit 0 on both ranks and print nixlbench's full block-size sweep.

**Read the numbers below as a plumbing check, not as a comparison.** They were
taken on the login node, which has ONE gfx90a and no RDMA NIC. So:

- both ranks share a single GPU — this is loopback, not device-to-device;
- MORI's RDMA transport cannot initialise at all (no NIC), leaving XGMI/IPC,
  which on one device is close to a local copy;
- UCX has no ROCm-capable transport to select here either and is plainly on a
  degraded path (it flatlines at ~0.5 GB/s regardless of block size, which is
  not a real UCX number).

Comparing 359 GB/s against 0.5 GB/s would be meaningless. What these runs
prove is that both paths get through registration, connection, transfer and
completion without error.

| Block size | UCX (GB/s) | MORI_IO (GB/s) |
|---|---|---|
| 4 KiB | 0.10 | 0.10 |
| 64 KiB | 0.41 | 1.47 |
| 1 MiB | 0.51 | 21.6 |
| 8 MiB | 0.52 | 97.7 |
| 64 MiB | 0.52 | 359.4 |

A real comparison needs an MI300X + ConnectX-7 node — see Open items.

### The GPU queue is the blocker for real numbers

`make dist-bench` and `make dist-bench-2node` are written and parse, but I was
**never able to run them**: every GFX942 node on this cluster is booked about
two weeks out.

```
$ sbatch --test-only --partition=defq --constraint=GFX942 --gres=gpu:2 --time=00:20:00 --wrap=true
Job ... to start at 2026-08-24T05:58:36 using 32 processors on nodes ppac-cyxtera-cx62-3
```

So treat those two targets as untested code. Two things I did fix while
finding this out, which you would otherwise hit immediately:

- `aioss` is not a partition this account can submit to ("invalid partition
  specified"), despite `sinfo` listing the MI300X+CX7 nodes under it. `defq`
  reaches the same nodes and works.
- `-C GFX942&RDMA` is rejected outright with "Invalid feature specification" —
  an unknown feature is an error, not an empty match. The default is now plain
  `GFX942`.

If you already hold an allocation, skip Slurm entirely:

```bash
make dist-load NM_TARGETS=<node>       # once
# then on the node:
docker run --rm --device=/dev/kfd --device=/dev/dri --device=/dev/infiniband \
    --cap-add=IPC_LOCK --network=host --ipc=host --shm-size=16g \
    -e BACKEND=MORI_IO -e SEG_TYPE=VRAM \
    nixl-mori-test:latest run-nixlbench
```

### One real limitation found: MORI_IO cannot do DRAM here

`make bench BACKEND=MORI_IO SEG_TYPE=DRAM` fails at completion-check time with
MORI's own error:

```
No available backend found, please create backend first   (StatusCode 14)
```

This is not a plugin bug. MORI's `SelectBackend` finds nothing that can carry
host DRAM to host DRAM on this machine: RDMA (which would handle it) never
initialised for lack of a NIC, and XGMI is a GPU-memory transport. On a node
with a NIC the RDMA backend covers DRAM and this should just work — worth
re-testing there rather than treating it as a code issue.

## Open items (in the order I would do them)

1. **Get onto a GPU node and run `make dist-bench`.** Everything is staged for
   it — the image tarball is on `/scratch`, `run-bench.sh` handles loading it
   and running both backends — but the queue meant it never executed, so
   expect to debug it a little on first run. The 2-node variant
   (`make dist-bench-2node`) is the one that actually exercises RDMA and is
   the least tested of all.
2. **Re-test `SEG_TYPE=DRAM` there.** It fails on this node only because no
   MORI transport can carry host memory without a NIC; with RDMA up it should
   work, and if it does not, that is a genuine plugin bug to chase.
3. **The image is 34 GB**, mostly ROCm base + torch. `make dist-build
   MAKE_ARGS=INSTALL_TORCH=0` drops torch; nixlbench and `import mori` do not
   need it, only NIXL's Python `_api` does. Worth doing if the tarball
   round-trip becomes annoying.
4. Consider whether `mori_backends=auto` should prefer RDMA over XGMI when
   both exist (currently it creates both and lets MORI route). On a node with
   both, a benchmark meaning to measure RDMA should pass
   `mori_backends=rdma` explicitly — that makes it required, so it fails
   loudly instead of quietly measuring XGMI.

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
   → `patches/nixl/03-nixlbench-mori-io-backend.patch`.

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
| `03-nixlbench-mori-io-backend.patch` | Teach nixlbench's `--backend` about MORI_IO. |

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
