<!--
SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# NIXL on AMD: the ROCm fork

This repository is AMD's fork of [NIXL](https://github.com/ai-dynamo/nixl),
the NVIDIA Inference Xfer Library. You are reading the `main-amd` branch, which
is upstream NIXL plus AMD's changes for ROCm.

NIXL itself is described in the [project README](../README.md): what it is,
how to build it and how to use it. This page covers only what differs in the
fork: how its branches are laid out and how it stays in step with upstream.

GitHub shows this file instead of the project README because it lives in
`.github/`. Upstream has no file at this path, so the two never conflict.

## Layout

| Branch | Holds |
| --- | --- |
| `main` | An exact copy of upstream `ai-dynamo/nixl` `main`. It never holds an AMD commit. |
| `main-amd` | Upstream `main` plus AMD's changes. The default branch, and the base for pull requests. |
| `amd-sync/upstream-main` | The head of the pull request that brings new upstream commits into `main-amd`. |
| `gh-pages` | The nightly results site and the pip index of the nightly wheels. Written by workflows only. |

What `main-amd` adds to upstream falls into two groups:

- **Changes meant for upstream.** ROCm support in NIXL itself, such as the
  `rocm-nixl` wheel build under `contrib/`. Each is proposed to upstream and
  lives here until it is merged there.
- **Files that stay in the fork.** The CI that needs AMD's infrastructure:
  the ROCm wheel workflows and the nightly (`.github/workflows/rocm-wheels*.yml`,
  `.github/workflows/amd-*.yml`), the tests that run in QEMU guests with an
  emulated AMD GPU (`.ci/rocjitsu/`, `.ci/scripts/rocm_wheel_*.py`), the
  maintainer documents (`.ci/docs/amd-*.md`) and this file. They sit at paths
  upstream does not use, so that merging upstream does not conflict with them.

## Staying in step with upstream

Upstream changes arrive through `main`, once a night:

1. The `AMD Nightly` workflow fast-forwards `main` to upstream `main`.
2. When `main` then has commits that `main-amd` lacks, the workflow moves
   `amd-sync/upstream-main` to the tip of `main` and makes sure one pull
   request from that branch into `main-amd` is open.
3. A maintainer reviews and merges that pull request, with a merge commit.
   The workflow never merges it.

If upstream conflicts with an AMD change, merge `main-amd` into
`amd-sync/upstream-main`, resolve the conflict there and push. `main` stays
untouched.

AMD's changes go the other way by pull request to upstream:

1. A change lands in `main-amd` first, by pull request, where the nightly
   tests it.
2. A change meant for upstream is then proposed to `ai-dynamo/nixl` from a
   branch based on `main`, without the files that stay in the fork.
3. Once upstream merges it, it comes back through the nightly sync, and the
   difference between `main-amd` and `main` shrinks by that much.

The aim is for that difference to end up as the fork-only files and nothing
else.

## Contributing

Open pull requests against `main-amd`. Do not base work on `main` unless it is
going to upstream, and do not commit to `main` or `gh-pages`.

## Nightly tests and wheels

Each night `AMD Nightly` builds the `rocm-nixl` wheels from `main-amd` and
`main`, tests them in a container and in the emulated-GPU guests, and adds the
results to the site at `https://<owner>.github.io/<repo>/`. `main` is skipped
until upstream can build ROCm wheels.

Wheels from a night that passed are published as GitHub pre-releases, not to
PyPI:

```bash
pip install --pre \
    --index-url https://<owner>.github.io/<repo>/wheels/main-amd/simple/ \
    --extra-index-url https://pypi.org/simple \
    rocm-nixl
```

More detail:

- [AMD CI on the ROCm fork](../.ci/docs/amd-ci.md): the nightly, the sync, the
  repository settings and the results site.
- [Nightly rocm-nixl wheels](../.ci/docs/amd-wheels.md): installing a nightly
  and what the versions mean.
- [Building the wheels](../contrib/README.md).
