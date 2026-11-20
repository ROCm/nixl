## Build options

### Build & install in a conda environment
Refer to [README.md](../README.md) for instructions on how to install prerequisites.
```
$ conda activate <ENV_NAME>
$ export PKG_CONFIG_PATH=<PATH_TO_UCX>/lib/pkgconfig:$PKG_CONFIG_PATH

$ meson setup nixl-build -Ducx_path=<PATH_TO_UCX> --prefix=<PATH_TO_CONDA_ENV>
$ cd nixl-build
$ ninja
$ ninja install

$ cd ..
$ pip install . --config-settings=setup-args="-Ducx_path=<PATH_TO_UCX>"
$ pip list | grep nixl
$ python -c "from nixl._api import nixl_agent; print('Success')"
```

### Build wheels with OpenSSL 4

`Dockerfile.manylinux` defaults to OpenSSL 3.0.16. To use OpenSSL 4, pass its
version and release SHA-256 checksum:

```bash
docker build -f contrib/Dockerfile.manylinux \
    --build-arg OPENSSL_VERSION=4.0.3 \
    --build-arg OPENSSL_SHA256=325b5c806167c13b40b1ffeadfe0248197c00eccc4cf123ec1e28d2d2fd216d9 \
    --build-arg WHL_PYTHON_VERSIONS=3.13 \
    --build-arg BUILD_NIXL_EP=false \
    -t nixl-wheel:openssl4 .
```

Run from the repository root. Wheels are written to `/workspace/nixl/dist` in
the image. They require OpenSSL 4 runtime libraries (`libssl.so.4` and
`libcrypto.so.4`), which are not bundled in the wheel.

### Build ROCm wheels

`rocm-nixl` is one wheel per Python version holding the NIXL backends of
every supported ROCm version (7.14, 10.1 and 10.2, x86_64 only); `import nixl`
selects the backend matching the installed ROCm. It is built in three steps,
all run from the repository root.

1. Build the backend wheels, one build per ROCm version. `Dockerfile.manylinux`
   builds CUDA wheels by default; `--rocm-version X.Y.Z` builds against that
   ROCm release instead:

   ```bash
   for rocm in 10.2.0 10.1.0 7.14.1; do
       ./contrib/build-container.sh \
           --dockerfile contrib/Dockerfile.manylinux \
           --rocm-version "$rocm" \
           --python-versions 3.10,3.11,3.12,3.13,3.14 \
           --wheel-output-dir "dist-rocm$rocm"
   done
   ```

   `--wheel-output-dir DIR` writes only the wheels to `DIR` (the `wheel_dist`
   stage, exported as a BuildKit local output) and creates no image. Without
   it the build produces the image named by `--tag`, with the wheels in
   `/workspace/nixl/dist`, as for CUDA.

   Behind a TLS-intercepting proxy, add `--proxy-ca-cert FILE` to pass the
   proxy's root CA certificate to the build as a BuildKit secret.

   The ROCm in the build image is not a usable ROCm install: it has the
   runtime, the HIP, HSA and hipFile headers and `hipify-perl`, but of ROCm's
   LLVM only the libraries the runtime links (no compiler, no
   `amdrocm-llvm-devel`). NIXL and its dependencies are compiled with the
   image's GCC.

   Each `dist-rocm<version>` then holds the intermediate backend wheels
   `nixl_rocm<X>_<Y>-<version>-cp3*-cp3*-manylinux_2_28_x86_64.whl`
   (distribution `nixl-rocm<X>.<Y>`, import package `nixl_rocm<X>_<Y>`), one
   per Python version. They are not published. The platform tag is
   `manylinux_2_28_x86_64` unless `--wheel-base` is given.

2. Merge the backends of each Python version into one `rocm-nixl` wheel:

   ```bash
   for py in cp310 cp311 cp312 cp313 cp314; do
       ./contrib/wheel_merge_rocm.py --output-dir dist-rocm \
           dist-rocm*/nixl_rocm*-"$py"-"$py"-*.whl
   done
   ```

   `wheel_merge_rocm.py` copies every backend, adds the `nixl` dispatcher and
   `nixl_meta_utils.py` from `--dispatcher-dir` (default
   `src/bindings/python/nixl-meta`) and writes the `rocm-nixl` metadata. It
   refuses backends that differ in NIXL version, Python/ABI tag or platform
   tag, two backends for the same ROCm version, and any file present in more
   than one input. The backends' `.data` and `.scripts` directories are left
   out, so `rocm-nixl` does not install the example programs
   (`nixl_example`, `nixl_etcd_example`, `telemetry_reader`) or the asio
   headers that the backend wheels carry.

3. The result is in `dist-rocm`:
   `rocm_nixl-<version>-cp3*-cp3*-manylinux_2_28_x86_64.whl`, one per Python
   version.

`rocm-nixl` depends on `numpy` only. It declares one extra per ROCm version
(`rocm7.14`, `rocm10.1`, `rocm10.2`), each pinning AMD's ROCm build of PyTorch
exactly, so it can only be installed from AMD's index and never from PyPI,
whose `torch` is the CUDA build:

```bash
pip install "rocm-nixl[rocm10.2]" --extra-index-url https://stable.repo.amd.com/rocm/whl-next/
pip install "rocm-nixl[rocm10.1]" --extra-index-url https://stable.repo.amd.com/rocm/whl-next/
pip install "rocm-nixl[rocm7.14]" --extra-index-url https://repo.amd.com/rocm/whl-multi-arch/
```

The extras need pip 23.3 or newer; older pip ignores them with a warning.
AMD's PyTorch depends on AMD's pip ROCm packages (`rocm`, `rocm-sdk-core`,
`rocm-sdk-libraries`), so the extra installs those as well.

AMD publishes no Python 3.10 build of PyTorch for ROCm 10.2. The cp310 wheel
still holds the ROCm 10.2 backend and declares the `rocm10.2` extra, but pip
cannot install that extra on Python 3.10: it reports that no matching `torch`
is available.

The pins and index URLs are the `ROCM_PYTORCH` table in
`wheel_merge_rocm.py`; a ROCm version without an entry cannot be merged.

The ROCm runtime is not bundled: it must be installed on the host with its
libraries resolvable by the dynamic loader. `rocm-nixl` and `nixl` both
provide the `nixl` import package: do not install them in the same
environment.

`--cuda-version`, `--torch-cuda-index`, `--torch-versions` and
`--build-nixl-ep` are rejected with `--rocm-version` (`nixl_ep` is
CUDA-only).

Inside an environment that already has ROCm, UCX and NIXL installed the way
`Dockerfile.manylinux` sets them up, `build-wheel.sh` builds one backend wheel
into `--output-dir` (default `dist`):

```bash
./contrib/build-wheel.sh --rocm-path /opt/rocm --python-version 3.12
```

The wheel is tagged for `--platform` (default `manylinux_2_39_x86_64`); pass
the tag matching the glibc of the build environment, e.g.
`--platform manylinux_2_28_x86_64` in the manylinux image. The ROCm version is
read from `<rocm-path>/.info/version` (or `<rocm-path>/core/.info/version`).
The script rewrites `pyproject.toml` (name `nixl-rocm<X>.<Y>`, `torch` removed from the
dependencies), keeps the ROCm runtime out of the repaired wheel and fails if a
ROCm library was vendored or the wheel contains the `nixl` dispatcher or
`nixl_ep`.

#### nixlbench wheel

`rocm-nixlbench` installs the `nixlbench` command. It holds the nixlbench
binary of each ROCm version, built with HIP (VRAM) support and without the
ETCD runtime, and requires `rocm-nixl` at exactly the same version: the binary
uses the `libnixl` and plugins of that wheel, and `nixlbench` refuses to start
on another version. It is tagged `py3-none`, so one wheel serves every Python
version. Without the ETCD runtime, which is nixlbench's default, network
backends such as UCX need `--runtime_type ASIO --asio_address <address>`;
storage backends need no runtime.

1. Build the NIXL backend wheels, then the nixlbench backend wheel against
   them, with the same options and with both output directories outside the
   checkout:

   ```bash
   for rocm in 10.2.0 10.1.0 7.14.1; do
       common=(--dockerfile contrib/Dockerfile.manylinux --rocm-version "$rocm"
               --python-versions 3.10,3.11,3.12,3.13,3.14)
       ./contrib/build-container.sh "${common[@]}" \
           --wheel-output-dir "/tmp/dist-rocm$rocm"
       ./contrib/build-container.sh "${common[@]}" \
           --nixlbench-wheel-output-dir "/tmp/dist-nixlbench-rocm$rocm" \
           --nixlbench-nixl-wheel-dir "/tmp/dist-rocm$rocm"
   done
   ```

   The second build takes the NIXL build of the first from the build cache,
   whose key is the content of the checkout. A file written into the checkout
   between the two, such as a `--wheel-output-dir` inside it, makes the second
   build compile NIXL again: its nixlbench is then linked against other NIXL
   wheels than the ones in `--wheel-output-dir`. `nixl-wheels.sha256`, written
   beside the nixlbench wheel, lists the NIXL wheels nixlbench was linked and
   tested against. `--nixlbench-nixl-wheel-dir` compares them with the wheels
   of the first build and fails the second if they differ: do not use the
   nixlbench wheel then. Both options refuse a directory inside the checkout
   (`--nixlbench-nixl-wheel-dir` only without `--nixlbench-base-image`).

   Without the build cache, build the `wheel` image first (the same command
   without an output option, plus `--tag IMAGE`) and pass
   `--nixlbench-base-image IMAGE` to the nixlbench build. The NIXL wheels are
   then in `/workspace/nixl/dist` of that image. The build still needs the ROCm
   base stage, built or from the cache, to test the nixlbench wheel in.

   Each nixlbench output directory holds
   `nixlbench_rocm<X>_<Y>-<version>-py3-none-manylinux_2_28_x86_64.whl`
   (distribution `nixlbench-rocm<X>.<Y>`, which requires
   `nixl-rocm<X>.<Y>==<version>`). It is not published. The build fails unless
   the wheel installs and runs against every NIXL wheel of the first build.
   A nixlbench build failure does not affect the NIXL wheels.

2. Merge the NIXL backends as above, and the nixlbench backends:

   ```bash
   for py in cp310 cp311 cp312 cp313 cp314; do
       ./contrib/wheel_merge_rocm.py --output-dir /tmp/dist-rocm \
           /tmp/dist-rocm*/nixl_rocm*-"$py"-"$py"-*.whl
   done
   ./contrib/wheel_nixlbench.py merge --output-dir /tmp/dist-rocm \
       /tmp/dist-nixlbench-rocm*/nixlbench_rocm*.whl
   ```

   The result, `rocm_nixlbench-<version>-py3-none-manylinux_2_28_x86_64.whl`,
   requires `rocm-nixl==<version>`. Any subset of the backends can be merged.

3. Install it with the `rocm-nixl` wheel of the same build and run it:

   ```bash
   pip install /tmp/dist-rocm/rocm_nixl-*-cp312-*.whl /tmp/dist-rocm/rocm_nixlbench-*.whl
   mkdir -p /tmp/nixlbench
   nixlbench --backend POSIX --filepath /tmp/nixlbench --op_type WRITE \
       --total_buffer_size 80000000 --max_block_size 16384
   ./contrib/test-nixlbench-wheel.sh --installed "$(command -v nixlbench)"
   ```

`nixlbench` needs a ROCm install and OpenSSL 3 on the host; neither is in the
wheel. It selects the backend from the ROCm install on disk, not from the
installed PyTorch, and puts that install's `lib` directory on the loader path
itself, so `LD_LIBRARY_PATH` is not needed. The install is the first of these
that has `.info/version` and `lib/libamdhip64.so*`:

- `$ROCM_PATH`, or `/opt/rocm` if it is unset;
- its `core` subdirectory;
- its `core-<X.Y>` subdirectory, when there is exactly one.

With several `core-<X.Y>` and no `.info` beside them, set `ROCM_PATH` to the
one to use. A ROCm version with no backend in the wheel is an error. If no
install is found, `nixlbench` warns, runs the backend for the newest ROCm
version and leaves finding ROCm to the dynamic loader. A ROCm installed only
from pip wheels is not found. `NIXL_PLUGIN_DIR`, when set, still overrides the
plugins of the `rocm-nixl` wheel; `nixlbench` warns about it.
`NIXLBENCH_WHEEL_DEBUG=1` prints what was selected.

Do not install a `nixlbench-rocm<X>.<Y>` backend wheel and `rocm-nixlbench` in
the same environment: both install the `nixlbench` command and the
`nixlbench_rocm<X>_<Y>` package.
