# nixl-mori-test

Build [NIXL](https://github.com/ai-dynamo/nixl) and [MORI](https://github.com/ROCm/mori)
from **tagged upstream releases plus local patches**, into a single ROCm image,
driven by a top-level `Makefile` — and drive **both** from one benchmark,
`nixlbench`, one `--backend` flag apart.

This is a ROCm build, not CUDA: no CUDA toolkit is present, NIXL's CUDA-only
backends are disabled, its bindings are built as the `rocm` wheel variant,
nixlbench is built with `-Duse_rocm=true`, and UCX is source-built from
`ROCm/ucx` so NIXL's UCX backend understands HIP memory. MORI is ROCm-native
(`-DUSE_ROCM=ON`).

MORI is reachable from NIXL through a **`MORI_IO` backend plugin** that lives
in `patches/nixl/` — it does not exist upstream. See
[patches/nixl/README.md](patches/nixl/README.md) and
[OVERNIGHT.md](OVERNIGHT.md) for what it does and what was learned building it.

## Layout

```
Makefile                  Everything is driven from here (make help)
VERSION                   Version of this tree; first component of the image tag
docker/
  Dockerfile              base -> ucx --------> nixl -\
                            \--> mori --(SDK)-----^     >-> runtime
                                  \---------------------/
  scripts/
    clone-src.sh          Clone a component at its pinned ref + apply its patchset
    build-ucx.sh          ROCm/ucx with HIP memory support
    build-nixl.sh         NIXL meson build, ROCm variant
    build-nixlbench.sh    nixlbench, -Duse_rocm=true
    build-mori.sh         MORI wheel + C++ SDK install
    fix-rocm-cmake.sh     Scrub build-host paths out of ROCm's CMake configs
    image-tag.sh          Derive the image tag from the component pins
    patch-check.sh        Host-side dry run of every patchset (no image build)
    smoke-test.sh         In-container checks, as nixl-mori-smoke-test
    run-nixlbench.sh      Two-process nixlbench pair, as run-nixlbench
patches/
  nixl/                   Applied to ai-dynamo/nixl @ NIXL_REF
  mori/                   Applied to ROCm/mori      @ MORI_REF
.slurm/
  run-build.sh            Build on a CPU-only node, ship via /scratch
  run-bench.sh            Run the benchmark pair on GPU nodes
```

## Quick start

```bash
make help          # every target and every resolved pin
make print-config  # just the pins
make build         # combined image
make test          # smoke test (imports, NIXL plugins, nixlbench, MORI devices)
make shell         # interactive shell with GPUs + RDMA passed through
```

Behind the AMD Zscaler proxy, pass the corporate CA as a BuildKit secret — it
is mounted into one layer and never baked into the image:

```bash
make build TLS_CERT=/etc/ssl/certs/zscaler-ca.crt
```

## Benchmarking both stacks

`nixlbench` drives NIXL, and the `MORI_IO` plugin makes MORI one of NIXL's
backends — so the same harness, buffers, warmup and timing code measure both,
and the only thing that changes is the flag:

```bash
make bench BACKEND=UCX     SEG_TYPE=VRAM
make bench BACKEND=MORI_IO SEG_TYPE=VRAM
make bench-compare                        # both, back to back
```

Each run starts a target and an initiator process that rendezvous over
nixlbench's ASIO socket runtime (no etcd needed) and prints its block-size
sweep.

**A login node cannot produce a meaningful number.** With one GPU and no RDMA
NIC, both ranks share a device and MORI's RDMA transport never initialises. For
real numbers, run on GPU+NIC nodes via Slurm:

```bash
make dist-build                    # build on a CPU-only node -> /scratch tarball
make dist-bench                    # UCX + MORI_IO on one GPU node (2 GPUs)
make dist-bench-2node              # initiator and target on two nodes (RDMA path)
```

`.slurm/run-bench.sh` documents the `NM_*` knobs (partition, constraint,
nodelist, backends, segment type).

## Pinned components

| Component | Default ref | Source |
|---|---|---|
| ROCm base | `7.14.0` | `rocm/dev-ubuntu-24.04:${ROCM_VERSION}-full` |
| NIXL | `v1.3.2` | `ai-dynamo/nixl` |
| MORI | `v1.2.2` | `ROCm/mori` |
| UCX | `v1.19.x` | `ROCm/ucx` (AMD fork) |

Override any of them on the command line; the image tag follows automatically:

```bash
make build NIXL_REF=v1.3.1 MORI_REF=v1.2.1
make print-tag        # nixl-mori-test:0.1.0-rocm7.14.0-nixl1.3.1-mori1.2.1
```

The defaults live in **two** places that must agree: the `Makefile` (which
passes them as build args) and the `ARG` lines in `docker/Dockerfile` (the
fallback when the Dockerfile is built directly, and the source `image-tag.sh`
reads). Bump both.

## Patches

Each patchset is its own folder under `patches/`, applied to a fresh checkout
of the pinned tag in lexical filename order. A patch that does not apply fails
the build. See [patches/README.md](patches/README.md) for the workflow.

```bash
make patch-list                 # what will be applied
make patch-check                # dry-run against the pinned tags, no image build
make patch-check COMPONENT=nixl
```

`patches/mori/` is empty — MORI v1.2.2 builds pristine. `patches/nixl/` carries
three patches:

| Patch | What |
|---|---|
| `01-nixl-mori-io-backend.patch` | New `src/plugins/mori_io/`: a NIXL backend engine on top of MORI-IO, plus meson wiring (`-Dmori_path=`). This is what makes `--backend MORI_IO` possible. |
| `02-nixlbench-rocm-tomlplusplus-noinline.patch` | ROCm defines `__noinline__` as a macro, which breaks toml++'s `__has_attribute` check and the nixlbench ROCm build. |
| `04-nixlbench-extra-backends.patch` | nixlbench gates `--backend` on a hardcoded list; this adds MORI_IO to it. |

Patch 01 is developed as a real checkout rather than by editing the `.patch` —
`patches/nixl/regen-mori-io-patch.sh <work-tree>` re-exports it with its
comment header intact.

## Partial builds

The NIXL and MORI stages are independent, so BuildKit builds them in parallel
and either can be built alone:

```bash
make build-nixl    # base -> ucx -> nixl
make build-mori    # base -> mori
make wheels        # export the MORI wheel to ./dist, no image loaded
```

## Inside the image

| Path / variable | |
|---|---|
| `/opt/nixl` | NIXL meson install prefix (`NIXL_PREFIX`) |
| `/opt/nixl/lib/x86_64-linux-gnu/plugins` | NIXL backend plugins, incl. `libplugin_MORI_IO.so` (`NIXL_PLUGIN_DIR`) |
| `/opt/nixlbench` | nixlbench, on `PATH` (`NIXLBENCH_PREFIX`) |
| `/opt/mori` | MORI C++ SDK: headers + `libmori_*.so` (`MORI_PREFIX`) |
| `/opt/rocnixl-ucx` | ROCm UCX install prefix (`UCX_PREFIX`) |
| `PYTHONPATH=/opt/nixl/lib/python3/dist-packages` | so `import nixl` resolves |
| `amd_mori` wheel | pip-installed system-wide; `import mori` |
| `nixl-mori-smoke-test` | the smoke test, on `PATH` |
| `run-nixlbench` | the two-process benchmark runner, on `PATH` |

NIXL's ROCm bindings install as `nixl_rocm`; a small shim package re-exports
them as `nixl`, so consumers keep writing `import nixl`.

MORI JIT-compiles its GPU kernels with `hipcc` on first use and caches them in
`~/.mori/jit`, so the first run in a fresh container pays a compile cost. The
image build skips precompilation deliberately (no GPU in a build layer); to
warm the cache in a running container:

```bash
MORI_PRECOMPILE=1 python3 -c "import mori"
```
