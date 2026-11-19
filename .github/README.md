<!--
SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# NIXL on AMD: the ROCm fork

<!-- CI status on main-amd -->
[![AMD Nightly](https://img.shields.io/github/actions/workflow/status/ROCm/nixl/amd-nightly.yml?branch=main-amd&label=nightly&style=flat-square)](https://github.com/ROCm/nixl/actions/workflows/amd-nightly.yml)
[![AMD Nightly Wheels](https://img.shields.io/github/actions/workflow/status/ROCm/nixl/amd-nightly-wheels.yml?branch=main-amd&label=nightly%20wheels&style=flat-square)](https://github.com/ROCm/nixl/actions/workflows/amd-nightly-wheels.yml)
[![ROCm Wheels](https://img.shields.io/github/actions/workflow/status/ROCm/nixl/rocm-wheels.yml?label=rocm%20wheels&style=flat-square)](https://github.com/ROCm/nixl/actions/workflows/rocm-wheels.yml)
[![AMD Copyright Checks](https://img.shields.io/github/actions/workflow/status/ROCm/nixl/amd-copyright-checks.yml?label=copyright&style=flat-square)](https://github.com/ROCm/nixl/actions/workflows/amd-copyright-checks.yml)
[![Nightly results](https://img.shields.io/badge/nightly-results-informational?style=flat-square)](https://rocm.github.io/nixl/)
[![Last commit](https://img.shields.io/github/last-commit/ROCm/nixl/main-amd?label=last%20commit&style=flat-square)](https://github.com/ROCm/nixl/commits/main-amd)

<!-- Nightly rocm-nixl wheels -->
[![Nightly wheel version](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Frocm.github.io%2Fnixl%2Fwheels%2Ffor-upstream-feat-rocm-wheels%2Findex.json&query=%24.version&label=rocm-nixl%20nightly&color=blue&style=flat-square)](https://rocm.github.io/nixl/wheels/for-upstream-feat-rocm-wheels/simple/rocm-nixl/)
[![Nightly wheel date](https://img.shields.io/badge/dynamic/json?url=https%3A%2F%2Frocm.github.io%2Fnixl%2Fwheels%2Ffor-upstream-feat-rocm-wheels%2Findex.json&query=%24.date&label=published&color=informational&style=flat-square)](https://github.com/ROCm/nixl/releases/tag/nightly-for-upstream-feat-rocm-wheels)
[![Wheel index](https://img.shields.io/badge/pip%20index-wheels-blue?style=flat-square)](https://rocm.github.io/nixl/wheels/)
[![Python](https://img.shields.io/badge/python-3.10%20%7C%203.11%20%7C%203.12%20%7C%203.13%20%7C%203.14-blue?style=flat-square&logo=python&logoColor=white)](https://rocm.github.io/nixl/wheels/for-upstream-feat-rocm-wheels/simple/rocm-nixl/)
[![ROCm](https://img.shields.io/badge/ROCm-7.14%20%7C%2010.1%20%7C%2010.2-ED1C24?style=flat-square)](https://rocm.docs.amd.com/)
[![Platform](https://img.shields.io/badge/platform-manylinux__2__28%20x86__64-lightgrey?style=flat-square)](https://rocm.github.io/nixl/wheels/for-upstream-feat-rocm-wheels/simple/rocm-nixl/)

This repository is AMD's fork of [NIXL](https://github.com/ai-dynamo/nixl),
the NVIDIA Inference Xfer Library. You are reading the `main-amd` branch, which
is upstream NIXL plus the CI that AMD runs on this fork. AMD's changes to NIXL
itself, such as the `rocm-nixl` wheel build, are not on this branch: each
lives on a `for-upstream/` branch until upstream takes it.

NIXL itself is described in the [project README](../README.md): what it is,
how to build it and how to use it. This page covers only what differs in the
fork: how its branches are laid out and how it stays in step with upstream.

GitHub shows this file instead of the project README because it lives in
`.github/`. Upstream has no file at this path, so the two never conflict.

## Layout

| Branch | Holds |
| --- | --- |
| `main` | An exact copy of upstream `ai-dynamo/nixl` `main`. It never holds an AMD commit. |
| `main-amd` | Upstream `main` plus the fork's CI. The default branch. It holds no change to NIXL itself. |
| `for-upstream/<name>` | A change to NIXL itself, based on `main` and proposed to upstream from there. |
| `amd-sync/upstream-main` | The head of the pull request that brings new upstream commits into `main-amd`. |
| `gh-pages` | The nightly results site and the pip index of the nightly wheels. Written by workflows only. |

AMD's work is kept in two places:

- **Changes meant for upstream** are on `for-upstream/` branches. That is ROCm
  support in NIXL itself, such as the `rocm-nixl` wheel build under `contrib/`
  on `for-upstream/feat/rocm-wheels`. Each branch is based on `main`, so it
  holds nothing but upstream and that one change.
- **Files that stay in the fork** are on `main-amd`, and are all that
  `main-amd` adds to upstream. They are the CI that needs AMD's
  infrastructure: the ROCm wheel workflows and the nightly
  (`.github/workflows/rocm-wheels*.yml`, `.github/workflows/amd-*.yml`), the
  tests that run in QEMU guests with an emulated AMD GPU (`.ci/rocjitsu/`,
  `.ci/scripts/rocm_wheel_*.py`), the maintainer documents
  (`.ci/docs/amd-*.md`) and this file. They sit at paths upstream does not
  use, so that merging upstream does not conflict with them. Two upstream
  workflows also carry a check that keeps them from running outside
  `ai-dynamo/nixl`.

The workflows on `main-amd` build and test the `for-upstream/` branches; they
do not build `main-amd` itself, which has no ROCm wheel code.

## Staying in step with upstream

Upstream changes arrive through `main`, once a night:

1. The `AMD Nightly` workflow fast-forwards `main` to upstream `main`.
2. When `main` then has commits that `main-amd` lacks, the workflow moves
   `amd-sync/upstream-main` to the tip of `main` and makes sure one pull
   request from that branch into `main-amd` is open.
3. A maintainer reviews and merges that pull request, with a merge commit.
   The workflow never merges it.

If upstream conflicts with one of the fork's changes, merge `main-amd` into
`amd-sync/upstream-main`, resolve the conflict there and push. `main` stays
untouched.

AMD's changes go the other way by pull request to upstream:

1. A change to NIXL itself is made on a `for-upstream/<name>` branch based on
   `main`. The nightly builds and tests every such branch.
2. It is proposed to `ai-dynamo/nixl` from that branch.
3. Once upstream merges it, it comes back through the nightly sync, first to
   `main` and then to `main-amd`, and the branch is no longer needed.

## Contributing

A change to the fork's CI or to these documents is a pull request against
`main-amd`. A change to NIXL itself goes on a `for-upstream/` branch based on
`main`, not into `main-amd`. Do not commit to `main` or `gh-pages`.

A push to a `for-upstream/` branch does not start the wheel workflow, which
is on `main-amd` only; the next nightly is the first to build the change. To
build and test it before that, start the workflow on its branch by hand:

```bash
gh workflow run rocm-wheels.yml -f ref=<branch with the change>
```

## Nightly tests and wheels

Each night `AMD Nightly` builds the `rocm-nixl` wheels from `main` and from
every `for-upstream/` branch, tests them in a container and in the
emulated-GPU guests, and adds the results to the site at
`https://rocm.github.io/nixl/`. A branch that cannot build ROCm wheels yet is
skipped; that is `main` until upstream can.

The wheels hold a backend for each of ROCm 7.14, 10.1 and 10.2. All three are
tested in a container. The emulated-GPU guests carry ROCm 10.1, so only that
backend is tested there.

One branch is published: the wheels branch, named in
[`amd-wheels-branch.txt`](amd-wheels-branch.txt), which is
`for-upstream/feat/rocm-wheels`. Its wheels from a night that passed go out as
a GitHub pre-release, not to PyPI. Every other branch is built and tested
only.

```bash
pip install --pre \
    --index-url https://rocm.github.io/nixl/wheels/for-upstream-feat-rocm-wheels/simple/ \
    --extra-index-url https://pypi.org/simple \
    rocm-nixl
```

The extras `rocm7.14`, `rocm10.1` and `rocm10.2` add AMD's PyTorch for that
ROCm version. It is on AMD's index only, which the command above does not
name: for `rocm10.1` and `rocm10.2` add
`--extra-index-url https://stable.repo.amd.com/rocm/whl-next/`; Building the
wheels, linked below, gives the index for each extra. The `rocm10.2` extra is
for Python 3.11 and newer: AMD publishes no PyTorch for ROCm 10.2 on Python
3.10.

The older index `wheels/main-amd/` and the release `nightly-main-amd` are no
longer updated.

More detail:

- [AMD CI on the ROCm fork](../.ci/docs/amd-ci.md): the nightly, the sync, the
  repository settings and the results site.
- [Nightly rocm-nixl wheels](../.ci/docs/amd-wheels.md): installing a nightly
  and what the versions mean.
- [Building the wheels](https://github.com/ROCm/nixl/blob/for-upstream/feat/rocm-wheels/contrib/README.md#build-rocm-wheels),
  on the wheels branch.
