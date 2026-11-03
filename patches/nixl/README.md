# NIXL patchset

Applied to `ai-dynamo/nixl` at `NIXL_REF` (see the `Makefile`), in lexical
filename order. See [../README.md](../README.md) for the rules.

| Patch | What |
|---|---|
| `01-nixl-rocm-hip-ais-mt.patch` | ROCm/HIP detection plus the `AIS` and `AIS_MT` plugins. |
| `02-nixl-mori-io-backend.patch` | New `src/plugins/mori_io/` — a NIXL backend engine implemented on MORI-IO, plus the meson wiring (`-Dmori_path=`). |
| `03-nixlbench-rocm-tomlplusplus-noinline.patch` | Fixes the nixlbench ROCm build: HIP defines `__noinline__` as a macro, which breaks toml++'s `__has_attribute` check. |
| `04-nixlbench-extra-backends.patch` | Adds `MORI_IO` to nixlbench's accepted `--backend` list. |
| `05-nixl-rocm-ais-async.patch` | Makes the AIS plugins' transfers asynchronous. |
| `06-nixl-libfabric-verbs-provider.patch` | Lets the LIBFABRIC backend select the `verbs` provider. |
| `07-nixl-libfabric-rocr-vram.patch` | Restores the ROCm/VRAM support v1.5.2 removed from the LIBFABRIC backend. |
| `08-nixl-obj-hipobject-engine.patch` | New `src/plugins/obj/s3_accel/amd/` — a hipObject S3-over-RDMA engine for the OBJ plugin, the ROCm counterpart of the CUDA-only Dell one, plus the meson wiring (`-Dhipobj_path=`). |

## Why a MORI plugin exists here

Upstream NIXL has no ROCm/HIP support of its own — `meson.build` detects CUDA
only — and no knowledge of MORI. Building on a ROCm-only image (as this tree
does) gives a NIXL whose UCX backend is ROCm-aware, because UCX itself is
source-built against ROCm here, but nothing that can drive MORI.

`01` closes that: it implements `nixlBackendEngine` on top of `mori::io::IOEngine`,
so MORI becomes a NIXL backend like any other and every NIXL consumer —
`nixlbench` above all — can select it by name. The concept mapping is
documented at the top of `mori_io_backend.h`; the short version:

| NIXL | MORI-IO |
|---|---|
| agent | `IOEngine`, keyed by the NIXL agent name |
| `getConnInfo` / `loadRemoteConnInfo` | msgpack'd `EngineDesc` / `RegisterRemoteEngine` |
| `registerMem` / `getPublicData` | `RegisterMemory` → `MemoryDesc` / msgpack'd `MemoryDesc` |
| `postXfer` | `BatchWrite` / `BatchRead` |
| `checkXfer` | poll `TransferStatus` |
| `getNotifs` / `genNotif` | no MORI equivalent — the plugin runs its own small TCP channel |

That last row is the one surprise. NIXL refuses to create a backend where
`supportsRemote()` is true and `supportsNotif()` is false, and MORI has no
arbitrary-message channel (`PopInboundTransferStatus` is a per-transfer
completion signal, not a payload channel). So the plugin carries a per-engine
TCP listener on an ephemeral port, advertised to peers inside `getConnInfo`.

## Two things to know before editing

**Patch 01 is developed as a checkout, not by editing the `.patch`.** It is
~1200 lines and mostly new files:

```bash
WORK=/tmp/nixl-mori-work/nixl
git clone --depth 1 --branch v1.3.2 https://github.com/ai-dynamo/nixl.git "$WORK"
git -C "$WORK" apply patches/nixl/02-nixl-mori-io-backend.patch
# ...edit $WORK...
patches/nixl/regen-mori-io-patch.sh "$WORK"
make patch-check COMPONENT=nixl && make build-nixl
```

**Patches 02 and 03 touch the same file** (`benchmark/nixlbench/src/utils/utils.h`).
Patch 03 must therefore be diffed against a tree that already has 01+02
applied, or it will swallow 02's hunk and fail to apply. `make patch-check`
catches this.

## The build side

`docker/scripts/build-nixl.sh` passes `-Dmori_path=$MORI_INSTALL_PREFIX` only
when the patched tree actually declares that meson option, so removing patch 01
leaves a pristine tag that still configures. The MORI C++ SDK it points at is
staged at `/opt/mori` by the mori stage — including the msgpack and spdlog
headers MORI's public headers include but its install rules do not ship.

## The OBJ plugin and the `hipobj` engine (patch 08)

Two things have to be true before patch 08 does anything, and the first is not
about this patch at all.

**The OBJ plugin needs the AWS SDK for C++.** `src/utils/object/meson.build`
looks up `aws-cpp-sdk-s3`, `aws-cpp-sdk-s3-crt` and `aws-cpp-sdk-core` by
pkg-config, and without all three it skips `obj_utils`; `src/plugins/obj/meson.build`
then calls `subdir_done()` and the entire plugin vanishes — every engine,
with one warning in the meson log and no error. That is why this image had no
object backend at all until `docker/scripts/build-aws-sdk.sh` was added.

That script has two workarounds worth knowing about, because both failures look
like something else:

- The SDK's generated `.pc` files carry `includedir=include` and `libdir=lib`
  with no `prefix=` line, so they are *relative* and resolve against whatever
  directory the consumer builds in. pkg-config reports success and `ld` then
  reports `cannot find -laws-cpp-sdk-s3` for a library that is plainly
  installed. The script rewrites them.
- Even with that fixed, no `-I` reaches the compile line, because
  `src/utils/object/meson.build` narrows all three with
  `partial_dependency(compile_args: false, includes: true, ...)` — and for a
  pkg-config dependency meson keeps the `-I` flags in `compile_args`, which
  that call discards. (It discards them deliberately: the SDK also puts
  `-std=c++17` and `-fno-exceptions` in its Cflags.) So `build-nixl.sh` puts
  the prefix on `CPLUS_INCLUDE_PATH` instead. Upstream never sees either
  problem because its CI installs the SDK into a default prefix.

**Then the engine needs hipObject**, staged at `/opt/hipobject` and passed as
`-Dhipobj_path=`, in the same opt-in shape as `-Dmori_path=`.

Selecting it at run time takes two backend parameters:

```
accelerated=true
type=hipobj
```

and optionally `hipobj_gpu=<hip device index>` (default `-1`, meaning the
calling thread's current device) and `hipobj_nic=<ibv device name>` for a host
where the GPU-to-NIC topology lookup cannot choose, or where there is no GPU.
