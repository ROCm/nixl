<!--
SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# Nightly rocm-nixl wheels

The fork builds `rocm-nixl` wheels every night and publishes those of one
branch, the wheels branch, when it passed. They are development builds for
people who want to try a change before it is released. They are **not PyPI
releases**: nothing in this flow uploads to PyPI, and the wheels have only been
tested against emulated devices on hosted runners, never on hardware.

## Installing

```bash
pip install --pre \
    --index-url https://rocm.github.io/nixl/wheels/for-upstream-feat-rocm-wheels/simple/ \
    --extra-index-url https://pypi.org/simple \
    rocm-nixl
```

The index path is `https://<owner>.github.io/<repo>/wheels/<slug>/simple/`.
`<owner>` and `<repo>` name the fork, and `<slug>` is the published branch
with each `/` replaced by `-`: `for-upstream-feat-rocm-wheels` for
`for-upstream/feat/rocm-wheels`.

All three options matter:

- `--index-url` replaces PyPI rather than adding to it, and the nightly index
  holds `rocm-nixl` only. Without `--extra-index-url https://pypi.org/simple`
  pip cannot find `numpy`, which `rocm-nixl` depends on.
- `--pre` is there because every nightly is a development release
  (`X.Y.Z.devN`), which pip ignores by default whenever a final release of the
  project is on offer.
- pip has no index priority. It pools the candidates from both indexes and
  takes the highest version, and `X.Y.Z.devN` sorts *below* `X.Y.Z`. As soon as
  a final `rocm-nixl X.Y.Z` is on PyPI, the command above installs that
  instead of the nightly, with or without `--pre`.

To get one exact nightly whatever else is published, pin the version. A pin on
a development version selects it without `--pre`, which also keeps pip from
choosing pre-releases of the dependencies:

```bash
pip install --no-cache-dir \
    --index-url https://<owner>.github.io/<repo>/wheels/<slug>/simple/ \
    --extra-index-url https://pypi.org/simple \
    rocm-nixl==<version>
```

The version currently published is the `version` field of
`https://<owner>.github.io/<repo>/wheels/<slug>/index.json`, which also gives
the commit, the date and each file's size and SHA-256.
`https://<owner>.github.io/<repo>/wheels/` lists every index and says which
are no longer updated.

`--no-cache-dir` is for the case where a nightly is rebuilt on the same day:
the version is the same, the contents are not, and pip would otherwise reuse
its cached copy.

A ROCm installation matching one of the wheel's backends is still needed at
run time; see `contrib/README.md` on the wheels branch
([here](https://github.com/ROCm/nixl/blob/for-upstream/feat/rocm-wheels/contrib/README.md#build-rocm-wheels)).

## The published branch

Only one branch is published: the wheels branch, named in
`.github/amd-wheels-branch.txt` on `main-amd`.

| Slug | Branch | What it is |
| --- | --- | --- |
| `for-upstream-feat-rocm-wheels` | `for-upstream/feat/rocm-wheels` | Upstream `main` plus the ROCm wheel build. The wheels branch, and the one to use. |
| `main-amd` | `main-amd` | No longer updated. `main-amd` holds the fork's CI only and builds no wheels. The index and the `nightly-main-amd` release keep the last build made from it. |

The nightly also builds and tests `main` and every other `for-upstream/`
branch, without publishing them. `main` cannot build ROCm wheels until upstream
can; the nightly skips it with a notice. Another `for-upstream/` branch is
built as `main` plus its own change, so its wheels do not hold the wheels
branch's changes; the branches are never combined.

When the wheels branch changes, the new branch is published under its own
slug. The old slug's index and release stay as they are.

## Versions

A nightly's version is the project version with `.dev<YYYYMMDD>` appended,
where the date is the UTC day of the nightly run: `1.6.0.dev20261118`. The
suffix is stamped into `pyproject.toml` and `meson.build` in the CI checkout
before the build, and the workflow fails if a wheel's file name does not carry
the stamped version. These are PEP 440 development releases of the *next*
`1.6.0`, so they sort before it.

Only the latest passing night is kept for each branch. Older nightlies are
removed; a pin on yesterday's version stops resolving once tonight's is
published.

## How it is published

`.github/workflows/amd-nightly-wheels.yml` ("AMD Nightly Wheels") runs when an
"AMD Nightly" run finishes, or by hand with the id of such a run. For the
wheels branch:

1. The branch is published only if its entry in the nightly's
   `nightly-summary.json` has `passed: true`, that is, the branch is
   supported and every required build and test job succeeded. Otherwise it is
   skipped with a notice and its previous release and index stay as they are.
2. The wheels go to a GitHub pre-release tagged `nightly-<slug>` as release
   assets. New files are uploaded and verified first; the previous night's
   files are removed only after the index has stopped pointing at them.
3. `.ci/scripts/amd_wheel_index.py` writes a PEP 503 index under
   `wheels/<slug>/simple/` on the `gh-pages` branch, with links to the release
   assets carrying their SHA-256 and `Requires-Python`, plus
   `wheels/<slug>/index.json`.

The `nightly-<slug>` tag is created once and never moved. The release notes and
`index.json` give the commit the wheels were built from; the tag does not.

The workflow acts only on runs of the fork's default branch started by the
schedule or by hand, never on pull requests, and only when the repository
variable `AMD_NIGHTLY_ENABLED` is `true` or it is dispatched by hand. See
[amd-ci.md](amd-ci.md) for the nightly itself and the repository settings.
