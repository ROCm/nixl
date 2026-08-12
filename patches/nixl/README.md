# NIXL patchset

Applied to `ai-dynamo/nixl` at `NIXL_REF` (see the `Makefile`), in lexical
filename order. See [../README.md](../README.md) for the rules.

| Patch | What |
|---|---|
| `01-nixl-mori-io-backend.patch` | New `src/plugins/mori_io/` — a NIXL backend engine implemented on MORI-IO, plus the meson wiring (`-Dmori_path=`). |
| `02-nixlbench-rocm-tomlplusplus-noinline.patch` | Fixes the nixlbench ROCm build: HIP defines `__noinline__` as a macro, which breaks toml++'s `__has_attribute` check. |
| `04-nixlbench-extra-backends.patch` | Adds `MORI_IO` to nixlbench's accepted `--backend` list. |

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
