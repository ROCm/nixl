<!--
SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# AMD CI on the ROCm fork

This is for maintainers of the AMD fork of NIXL. It covers the branches, the
`AMD Nightly` workflow ([`amd-nightly.yml`](../../.github/workflows/amd-nightly.yml)),
the repository settings the workflow needs, and the results site. How to
install the nightly wheels is in [amd-wheels.md](amd-wheels.md); how the wheels
are built is in [`contrib/README.md`](../../contrib/README.md).

## Branches

| Branch | Holds | Changes by |
| --- | --- | --- |
| `main` | An exact copy of upstream `ai-dynamo/nixl` `main`. No AMD commits, ever. | The nightly fast-forwards it. |
| `main-amd` | Upstream `main` plus AMD's changes. The default branch. | Pull requests only. Nothing pushes to it, neither a person nor a workflow. |
| `amd-sync/upstream-main` | The head of the pull request that brings new upstream commits into `main-amd`. | The nightly, and a maintainer who has a conflict to resolve. |
| `gh-pages` | The results site and the pip index of the nightly wheels. | The nightly and the wheel publisher, one commit per run. |

`main-amd` is the default branch because GitHub reads scheduled workflows,
`workflow_run` workflows and the "Run workflow" button from the default branch
only. The VM-based tests stay on this fork and are not proposed upstream.

Merge every pull request into `main-amd` with a merge commit. The branch
protection must not require linear history: after a squash or a rebase of the
sync pull request `main-amd` still lacks upstream's commits, and the next
night opens the same pull request again.

### Setting the fork up

Once, in this order:

1. Create `main-amd` at the commit of upstream `main`.
2. Make `main-amd` the default branch and protect it: require a pull request,
   do not require linear history.
3. Merge the ROCm wheel build support (`feat/rocm-wheels`, the part meant for
   upstream) into `main-amd` by pull request.
4. Open the CI series, which holds the wheel workflows, the VM tests and the
   nightly, as a pull request against `main-amd` from its own branch
   (`amd/ci-nightly`, based on the tip of `feat/rocm-wheels`) and merge it.
5. Open the wheel publishing series as a pull request against `main-amd` from
   its own branch (`amd/wheel-publishing`, based on `amd/ci-nightly`) and merge
   it last. Its workflow is started by `AMD Nightly` and uses the artifacts of
   that workflow, so it has nothing to do before step 4.
6. Set the variable and the secret from the settings below and enable the
   schedule, then start `AMD Nightly` once by hand. That run creates
   `gh-pages` and warns that Pages is not being served.
7. Switch Pages on for `gh-pages`, `/`.

Neither workflow does anything before its file is on `main-amd`.

## What the nightly does

`AMD Nightly` runs once a night and from the "Run workflow" button. Its jobs:

1. **Sync with upstream.** Fast-forwards `main` to upstream `main`. When `main`
   then has commits that `main-amd` lacks, it moves `amd-sync/upstream-main` to
   the tip of `main` and makes sure one pull request from that branch into
   `main-amd` is open. It never merges, approves or auto-merges that pull
   request, and never pushes to `main-amd`.
2. **Select the branches.** Reads
   [`.github/amd-nightly-branches.txt`](../../.github/amd-nightly-branches.txt)
   from the default branch and resolves each branch to a commit.
3. **wheels (`<branch>`).** One call of the `ROCm Wheels` workflow
   ([`rocm-wheels.yml`](../../.github/workflows/rocm-wheels.yml)) per branch:
   the wheels for Python 3.10 to 3.14, stamped `<version>.dev<YYYYMMDD>` with
   the UTC date of the run, then the container test and the rocjitsu VM tests.
   The source is the branch's; the test scripts and the VM definitions are
   those of the commit the nightly itself runs at.
4. **Report.** Collects one `results.json` per branch, writes
   `nightly-summary.json` (artifact `amd-nightly-summary`) and adds the run to
   the results site.

The tests run on the branches as they are that night, whether or not the sync
worked and whether or not the sync pull request has been merged.

A branch that cannot build ROCm wheels is skipped with a notice and does not
fail the run. That is `main` today: upstream has neither the `--gpu-stack`
option of `contrib/build-container.sh` nor `contrib/wheel_merge_rocm.py`. The
skip ends by itself once upstream has both.

The run fails when the sync fails, or when a branch that can build ROCm wheels
does not pass. A branch passes when the `plan`, `build`, `merge`, `test` and
`rocjitsu` jobs of its wheel workflow all succeed and wheels came out. The
two-guest job, `rocjitsu-2vm`, is allowed to fail and is only reported.

### The sync pull request

- To resolve a conflict, merge `main-amd` into `amd-sync/upstream-main` and
  push. `main` is not touched. The nightly then leaves the branch alone, with
  a warning, until the pull request is merged; upstream commits that arrive in
  the meantime come with the next pull request.
- After the merge the nightly moves `amd-sync/upstream-main` back to the tip
  of `main` the next time `main-amd` is behind.
- The sync fails, and changes nothing, when `main` has a commit that upstream
  lacks. Reset `main` to upstream by hand to repair it.
- The pull request's author is the owner of `AMD_SYNC_TOKEN`. If `main-amd`
  requires an approving review, that person cannot be the one who approves.
- Pull-request workflows run on it as on any other pull request, because the
  token is not the workflow's own. A pull request opened with the workflow's
  own token would wait for approval before its workflows start, and would need
  "Allow GitHub Actions to create and approve pull requests" as well.

## Repository settings

| Setting | Value | Why |
| --- | --- | --- |
| Default branch | `main-amd` | See [Branches](#branches). |
| Branch protection on `main-amd` | Pull request required, linear history not required | See [Branches](#branches). |
| Actions | Enabled, and the `AMD Nightly` schedule enabled | Scheduled workflows are off by default in a fork, and GitHub switches them off again after 60 days without activity in the repository. |
| Variable `AMD_NIGHTLY_ENABLED` | `true` | Every job checks it, so a copy of the workflow in another fork does nothing on its schedule. A run started by hand does not need it. |
| Secret `AMD_SYNC_TOKEN` | See below | The sync job's pushes and its pull request. |
| Pages | Deploy from a branch, `gh-pages`, `/` | Serves the results site. The nightly creates the branch on its first run; Pages itself has to be switched on once by an administrator. |

`AMD_SYNC_TOKEN` is a fine-grained personal access token whose resource owner
is the organisation, limited to this repository, with:

- **Contents: read and write**, to push `main` and `amd-sync/upstream-main`.
- **Workflows: read and write**, because upstream's commits change files under
  `.github/workflows`, and GitHub refuses such a push without it. This is why
  the workflow's own token cannot do the sync: it can never have this
  permission.
- **Pull requests: read and write**, to open the sync pull request.

The classic equivalent is the `repo` and `workflow` scopes. Without the secret
the sync job is skipped with a warning and everything else still runs. The
token expires; the sync then fails with "could not fetch" or "could not
fast-forward" until the secret is replaced. A GitHub App cannot stand in for
it as the workflow is written: an App's tokens last an hour, and the workflow
has no step that mints one.

The sync job is the only user of the secret. Everything else runs with the
workflow's own token: read-only, except in the report job, which has
`contents: write` to push to `gh-pages`.

A push made with `AMD_SYNC_TOKEN` starts workflows like any other push, so the
upstream workflows that run on a push to `main` start in this fork each time
the mirror moves. Disable the ones that cannot work here:

```bash
gh workflow disable aws_efa_validation.yml
gh workflow disable build_validation.yml
gh workflow disable fern-check.yml
gh workflow disable request-nvskills-ci.yml
```

`pre-commit.yml` also runs on every push and can be left on or disabled.

Two upstream pull-request checks fail on pull requests into `main-amd`, so
neither can be a required check there; `gh workflow disable` switches them off:

- `copyright-checks` cannot start in the fork, because the pull of its
  container image is denied. Run `.github/workflows/copyright-check.sh <base>`
  locally instead.
- `PR Size Check` fails a pull request that adds more than 500 lines to files
  that already exist.

The dependency stage of the wheel build is cached per ROCm version, about
3.5 GB each against a limit of 10 GB for the repository. Only a build of the
commit the workflow runs at writes that cache, which for the nightly is the
`main-amd` leg; every other build reads it. Pull requests into `main-amd` read
it as well.

Every branch the nightly builds is trusted as much as `main-amd`. The build
runs the branch's own `contrib/build-container.sh` with access to that shared
build cache and to the run's artifacts, which the wheel publisher reads. Only
list, or start a run on, a branch whose contents you would merge.

## Adding a branch

Add its name on a line of `.github/amd-nightly-branches.txt` on `main-amd`.
Names may hold letters, digits, `.`, `_`, `-` and `/`. A `/` becomes `-` in
artifact names, in the release tag `nightly-<slug>` and in the site's paths.
At most six branches are built in one run. A listed branch that does not exist
is skipped with a warning.

A new branch is tested and shown on the results site. Whether its wheels are
published is the wheel publisher's decision; see [amd-wheels.md](amd-wheels.md).

## Running one branch by hand

For the whole nightly on chosen branches, results site included:

```bash
gh workflow run amd-nightly.yml -f branches="main-amd" -f skip-sync=true
```

`branches` is a list separated by spaces and replaces the file for that run.
When `main-amd` or `main` is among them and passes, `AMD Nightly Wheels`
republishes that branch's nightly wheels as well: the version is the same as
that day's scheduled run, the contents are new. Use the form below when
nothing should be published.

For the wheel workflow alone, with nothing published:

```bash
gh workflow run rocm-wheels.yml -f ref=main-amd -f version-suffix=.dev20261118
```

Both inputs are optional. Without `ref` the workflow builds the ref it runs
on; without `version-suffix` the version is the one in `pyproject.toml`.

## Reading the site

The site is `https://<owner>.github.io/<repo>/`, written by
[`amd_results_site.py`](../scripts/amd_results_site.py) into `gh-pages`:

- `data/nightly/<stamp>/summary.json` is one night's `nightly-summary.json`.
  `<stamp>` is the UTC start of the run, as `YYYYMMDDTHHMMSSZ`.
- `data/nightly/index.json` lists the nights, newest first, with each branch's
  result, version and commit.
- `index.html` shows, per branch, a grid of every check by night (pass, fail,
  skip, not run) and the trends: build and test durations, wheel sizes, and
  transfer rates with the transport or storage path each one used.

The transfer rates come from emulated devices on shared runners. Each is the
bytes a check moved divided by the time the whole check took, set-up included.
They show whether something changed from one night to the next and say nothing
about real hardware.

Each branch entry of the summary has `passed`, a `reason` when it did not
pass, the `wheels_artifact` name, and under `results` that branch's
`results.json` from the wheel workflow: the result and duration of each job,
the wheels with their sizes and hashes, and every check of every test script.
The two-guest job runs one script eight times, so its entries carry a `label`
such as `stage2-initiator`.
