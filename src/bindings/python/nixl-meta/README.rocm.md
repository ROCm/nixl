# rocm-nixl

NIXL (NVIDIA Inference Xfer Library) Python bindings for AMD ROCm.

## Install

```bash
pip install rocm-nixl
```

Requirements:

- Linux on x86_64.
- One of these ROCm versions: <!-- rocm-versions -->7.14, 10.1 and 10.2<!-- /rocm-versions -->.
  ROCm is not part of the wheel: use AMD's pip packages, which the PyTorch
  extras below install, or a ROCm install on the host.
- OpenSSL 3 (`libssl.so.3`) on the host. Distributions that ship only OpenSSL
  1.1, such as EL8, are not supported.
- No `nixl` package in the same environment. `nixl` is the CUDA distribution
  and provides the same `nixl` import package.

## Install with PyTorch

`rocm-nixl` does not depend on PyTorch. To install AMD's ROCm build of PyTorch
with it, use the command for your ROCm version:

<!-- rocm-pytorch-install: this block and the version list above must match
ROCM_PYTORCH in contrib/wheel_merge_rocm.py, which regenerates them for the
wheel. test/python/test_rocm_wheel_merge.py checks that they do. -->
```bash
# ROCm 10.2 (Python 3.11 or newer)
pip install "rocm-nixl[rocm10.2]" --extra-index-url https://stable.repo.amd.com/rocm/whl-next/

# ROCm 10.1
pip install "rocm-nixl[rocm10.1]" --extra-index-url https://stable.repo.amd.com/rocm/whl-next/

# ROCm 7.14
pip install "rocm-nixl[rocm7.14]" --extra-index-url https://repo.amd.com/rocm/whl-multi-arch/
```
<!-- /rocm-pytorch-install -->

- `--extra-index-url` is required. AMD publishes its PyTorch and ROCm packages
  on its own package index, not on PyPI, and each extra pins AMD's PyTorch
  exactly.
- Do not install `torch` from PyPI. It is the CUDA build, and `import nixl`
  fails with it.
- "Python N or newer" means that AMD publishes no PyTorch for that ROCm
  version on an older Python. There pip reports that no matching `torch` is
  available. `rocm-nixl` without the extra still supports that ROCm version.
- The extras need pip 23.3 or newer (`python -m pip install -U pip`). Older
  pip ignores them with a warning and installs no PyTorch.
- AMD's PyTorch depends on AMD's pip ROCm packages (`rocm`, `rocm-sdk-core`,
  `rocm-sdk-libraries`). The extra installs them too, and `rocm-nixl` uses
  that ROCm.

To use AMD's pip ROCm without PyTorch, replace `"rocm-nixl[rocm<X.Y>]"` in the
command for your ROCm version with `rocm-nixl "rocm==<X.Y.Z>"`.

## How ROCm is found

The wheel holds one build of NIXL, called a backend, for each supported ROCm
version. `import nixl` uses the backend for the first ROCm it finds, in this
order:

1. The ROCm already loaded in the process. AMD's PyTorch loads its pip ROCm,
   and `import nixl` imports PyTorch when it is installed.
2. The ROCm version an imported PyTorch was built for (the `+rocm<X.Y.Z>` in
   its version), for a PyTorch that bundles its own ROCm.
3. `$ROCM_PATH`.
4. AMD's pip ROCm packages.
5. `/opt/rocm`, if `ROCM_PATH` is not set.

For `$ROCM_PATH` and `/opt/rocm`, ROCm can be in the directory itself, in its
`core` subdirectory, or in its `core-<X.Y>` subdirectory when there is exactly
one.

`import nixl` also loads the HIP runtime of the ROCm it found, so
`LD_LIBRARY_PATH` does not have to name it.

With several ROCm versions installed, set `ROCM_PATH` to the one to use:

```bash
export ROCM_PATH=/opt/rocm/core-<X.Y>
```

`ROCM_PATH` only takes effect when no ROCm is loaded yet. With AMD's PyTorch
installed, `import nixl` uses the ROCm of that PyTorch: to change the ROCm
version, install the PyTorch for that version.

## Troubleshooting

- **A warning that it could not tell which ROCm install to use.** No ROCm was
  found, or several `core-<X.Y>` versions are installed side by side.
  `import nixl` then uses the backend for the newest ROCm version, which may
  not be the ROCm you have. Set `ROCM_PATH` to your ROCm.
- **An error that there is no backend for the detected ROCm version.** That
  ROCm version is not supported. The message says where the version was read
  from:
  - PyTorch, or a loaded HIP runtime: the process already uses that ROCm, and
    `ROCM_PATH` does not change it. Install the PyTorch for a supported ROCm
    version.
  - A directory: if it is not the ROCm you meant to use, set `ROCM_PATH`.
- **An error that a CUDA build of PyTorch was found.** `torch` was installed
  from PyPI. Run `pip uninstall torch`, then the command for your ROCm version
  under "Install with PyTorch".
- **`nixl` and `rocm-nixl` are both installed.** Run
  `pip uninstall nixl nixl-cu12 nixl-cu13`, then
  `pip install --force-reinstall --no-deps rocm-nixl`. If `torch` is the CUDA
  build from PyPI, replace it as well, as in the previous item.
