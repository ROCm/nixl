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

`nixl-rocm` is the NIXL wheel for AMD ROCm (x86_64 only). It holds one build
of NIXL, called a backend, for each supported ROCm version, and `import nixl`
selects the backend for the ROCm it finds. Its
[README](../src/bindings/python/nixl-meta/README.rocm.md) describes how to
install and use it.

The wheel is built in two steps: build the backend wheels of each ROCm
version in a container, then merge them. You need an x86_64 host with Docker
(BuildKit) and Python 3, and a checkout without a `build` directory. Run the
commands from the repository root.

1. Build the backend wheels. Repeat this for each ROCm version, giving the
   full release number (X.Y.Z):

   ```bash
   rocm=10.1.0
   out=/tmp/nixl-wheels
   ./contrib/build-container.sh \
       --dockerfile contrib/Dockerfile.manylinux \
       --rocm-version "$rocm" \
       --python-versions 3.10,3.11,3.12,3.13,3.14 \
       --wheel-output-dir "$out/nixl-rocm$rocm"
   ```

   This writes one backend wheel per Python version to the output directory:
   `nixl_rocm<X>_<Y>-<version>-cp3*-cp3*-manylinux_2_28_x86_64.whl`. Backend
   wheels are intermediate files and are not published.

2. Merge the backend wheels, once per Python version:

   ```bash
   for py in cp310 cp311 cp312 cp313 cp314; do
       ./contrib/wheel_merge_rocm.py --output-dir "$out/dist" \
           "$out"/nixl-rocm*/nixl_rocm*-"$py"-"$py"-*.whl
   done
   ```

   The result is `nixl_rocm-<version>-cp3*-cp3*-manylinux_2_28_x86_64.whl` in
   `$out/dist`, one wheel per Python version.

#### Supported ROCm and Python versions

The supported ROCm versions are the entries of the `ROCM_PYTORCH` table in
`wheel_merge_rocm.py`. Each entry names the ROCm build of PyTorch that AMD
publishes for that ROCm version, and the package index it is on. From the
table and the backends it is given, the merge generates the wheel's
`rocm<X>.<Y>` extras, the version list and install commands of its README, and
the install commands that `import nixl` prints when it finds a CUDA build of
PyTorch.

To support another ROCm version:

1. Add an entry to the table. The merge refuses a backend whose ROCm version
   has no entry.
2. Update the version list and the install commands in
   [README.rocm.md](../src/bindings/python/nixl-meta/README.rocm.md), between
   its `<!-- rocm-... -->` markers. The file shows them for every entry so
   that it can be read in the repository, and
   `test/python/test_rocm_wheel_merge.py` fails until they match the table.
3. If the version is neither 7.14 nor 10.0 or newer, add its package
   repository to the `case` on `ROCM_MM` in `Dockerfile.manylinux`, which
   rejects it otherwise.
4. Build the backend for that version and merge it with the others.

`--rocm-version` accepts ROCm 7.14, and ROCm 10.0 or newer. ROCm 7.13 is in
the same package repository as 7.14, but without the hipFile packages the
build needs. The releases before 7.13 are in other repositories.

ROCm 10.0 is not supported: the build accepts it, but no backend for it has
been built and tested.

The Python versions above are the ones the CUDA wheels are built for; NIXL
supports Python 3.10 and newer. Every backend builds for all of them, because
a ROCm backend is not built against PyTorch. AMD's PyTorch for a ROCm version
may need a newer Python, which `min_python` in the table records. It only adds
a note to the README: on an older Python, pip cannot install that extra.

#### Build options

- `--wheel-output-dir DIR` writes only the wheels to `DIR` and creates no
  image. Without it, the build creates the image named by `--tag`, with the
  wheels in `/workspace/nixl/dist`, as a CUDA build does.
- `--proxy-ca-cert FILE` is for a build behind a TLS-intercepting proxy. It
  passes the proxy's root CA certificate (a PEM file) to the build as a
  BuildKit secret.
- `--wheel-base` changes the platform tag, which defaults to
  `manylinux_2_28` with `--rocm-version`.
- `--torch-versions` and `--build-nixl-ep` are rejected with
  `--rocm-version`. `nixl_ep` is CUDA-only, so a ROCm build leaves it out, and
  `--torch-versions` only selects the PyTorch versions `nixl_ep` is built
  against.

#### What the merge does

`wheel_merge_rocm.py` copies the backends into one wheel and adds:

- the `nixl` package that selects the backend at import time (the
  dispatcher), from `--dispatcher-dir` (default
  `src/bindings/python/nixl-meta`);
- the README, the `nixl/_api.py` type stub and
  `nixl/rocm_pytorch_install.txt` (the install commands for the dispatcher's
  error message), generated for the backends that were merged;
- the metadata: a dependency on `numpy` only, and one extra per backend. Each
  extra pins AMD's PyTorch exactly, so pip can never satisfy it with the CUDA
  build of `torch` from PyPI.

It refuses backends that differ in NIXL version, Python tag or platform tag,
two backends for one ROCm version, and a file that is in more than one
backend. It leaves out the example programs (`nixl_example`,
`nixl_etcd_example`, `telemetry_reader`) and the asio headers that the backend
wheels carry.

#### What build-wheel.sh does for ROCm

`build-container.sh` builds NIXL in the manylinux image, then runs
`build-wheel.sh` there once per Python version, from `/workspace/nixl`:

```bash
./contrib/build-wheel.sh --rocm-path /opt/rocm --python-version 3.12 \
    --platform manylinux_2_28_x86_64 --ucx-plugins-dir /usr/lib64/ucx \
    --nixl-plugins-dir /usr/local/nixl/lib64/plugins --output-dir dist
```

To run it by hand, start a shell in an image built with `--tag`. The script
needs what that image holds: ROCm, UCX and NIXL installed where the command
says, `uv` and `auditwheel`. The `--output-dir` directory must exist.

- The ROCm version is read from `<rocm-path>/.info/version` or
  `<rocm-path>/core/.info/version`.
- The script rewrites `pyproject.toml`: the name becomes `nixl-rocm<X>.<Y>`
  and `torch` is removed from the dependencies.
- The script fails if the wheel cannot be merged or would misbehave once
  installed. That is the case if it contains a copy of a ROCm library (the
  backend must use the ROCm of the machine it runs on, not a copy of the
  build's), the `nixl` dispatcher (the merge adds it), or `nixl_ep` (CUDA-only).
