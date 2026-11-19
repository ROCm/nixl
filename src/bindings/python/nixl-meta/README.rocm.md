# rocm-nixl

NIXL (NVIDIA Inference Xfer Library) Python bindings for AMD ROCm.

`rocm-nixl` is one wheel per Python version that holds the NIXL backends for
every supported ROCm version, currently 7.14, 10.1 and 10.2. `import nixl`
selects the backend matching the ROCm version installed on the host at import
time. Linux x86_64 only.

```bash
pip install rocm-nixl
```

## With PyTorch

`rocm-nixl` does not depend on PyTorch. To install it together with AMD's
official ROCm build of PyTorch, name the extra for your ROCm version and add
AMD's package index for that ROCm version:

```bash
# ROCm 10.2 (Python 3.11 or newer)
pip install "rocm-nixl[rocm10.2]" --extra-index-url https://stable.repo.amd.com/rocm/whl-next/

# ROCm 10.1
pip install "rocm-nixl[rocm10.1]" --extra-index-url https://stable.repo.amd.com/rocm/whl-next/

# ROCm 7.14
pip install "rocm-nixl[rocm7.14]" --extra-index-url https://repo.amd.com/rocm/whl-multi-arch/
```

Each extra pins the ROCm build of `torch` exactly, so it can only be
installed from AMD's index. The `torch` package on PyPI is the CUDA build and
does not work with `rocm-nixl`.

AMD publishes no Python 3.10 build of PyTorch for ROCm 10.2, so on Python 3.10
pip cannot install the `rocm10.2` extra and reports that no matching `torch`
is available. `rocm-nixl` without the extra supports ROCm 10.2 on Python 3.10.

The extras need pip 23.3 or newer (`python -m pip install -U pip`); older pip
ignores them with a warning and installs no PyTorch.

AMD's PyTorch depends on AMD's pip ROCm packages (`rocm`, `rocm-sdk-core`,
`rocm-sdk-libraries`), so the extra installs those too. The host ROCm
installation described below is still required.

## Requirements

A ROCm installation (7.14, 10.1 or 10.2) is required on the host, with its
libraries resolvable by the dynamic loader (e.g. `/opt/rocm/lib` in
`LD_LIBRARY_PATH`). ROCm is not bundled in the wheel.

OpenSSL 3 (`libssl.so.3`) is also required on the host; distributions that
ship only OpenSSL 1.1, such as EL8, are not supported.

The ROCm version is read from an installed ROCm build of PyTorch, or from
`$ROCM_PATH` (default `/opt/rocm`): `.info/version` or `core/.info/version`.
If ROCm is not installed under `/opt/rocm`, e.g. only `/opt/rocm/core-<X.Y>`
exists, set `ROCM_PATH` to that prefix (`export ROCM_PATH=/opt/rocm/core-7.14`)
and put its `lib` directory on the loader path. If no ROCm version can be
detected, the backend for the newest ROCm version is used.

## Do not install together with `nixl`

`rocm-nixl` and `nixl` (the NVIDIA CUDA distribution) both provide the `nixl`
import package and must not be installed in the same environment.
