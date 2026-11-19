<!--
SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# AMD nixlbench programme

This is for maintainers of the AMD fork of NIXL. It covers what the nightly
nixlbench programme is for, the `AMD Bench Probe` workflow
([`amd-bench-probe.yml`](../../.github/workflows/amd-bench-probe.yml)) that
answers the questions it is designed around, and the answers once they exist.
The branches, the nightly and the results site are in [amd-ci.md](amd-ci.md).

## What it is for

The nightly proves that the ROCm wheels build and that a transfer works. The
bench programme adds nixlbench runs over the NIXL backends an AMD machine can
use, in containers and in the rocjitsu guests, and publishes one record per
case so that a change from one night to the next is visible. Everything runs
on emulated devices on shared runners: the numbers show change and say nothing
about real hardware.

## Phases

| Phase | Holds |
| --- | --- |
| 0 Probe | The branch limit of twelve and the hardened `gh-pages` writers; the probe workflow; an answer recorded below for each Phase 0 experiment. |
| 1 First lanes | A nixlbench bundle built beside the wheels; container and one-guest lanes and the two-guest network lane, with records kept as artifacts only. The one-guest fabric lanes (loopback, NVMe-oF, S3) start here as a job that is allowed to fail. |
| 2 Data and site | The publisher and the bench pages. The Parquet shards live on `gh-pages` of this repository under `data/bench/v1/`, and the query engine beside the site under `perf/`, until their size calls for a repository of their own. |
| 3 Suites | The unit and plugin test suites, in containers and in the guest. |
| 4 Single-guest fabric | The fabric lanes become gating once they have passed for ten nights. |
| 5 Local PCIe | The SPDK lanes, and the comparison of the same workload on SPDK and on NVMe-oF. |
| 6 Product branches | The `for-upstream/` changes nixlbench needs: JSON output, the `NVME_KV` plugin, the hipObject engine, libfabric over verbs. A bench-only job merges the listed branches in its workspace, pushes nothing, and builds only the nixlbench bundle. |
| 7 Depth | A weekly deep tier, failure cases and regression flags. |

Key Value starts on the SPDK controller: the vendor-neutral `NVME_KV` plugin
is the upstream proposal, the SPDK KV namespace is its first target, and the
rocm-ernic KV namespace follows on the same plugin. The site's Node and React
source will be under `.ci/site/`.

## The probe workflow

`AMD Bench Probe` is started by hand only, needs no nixlbench binary and
publishes nothing. GitHub offers it once its file is on `main-amd`; `--ref`
then picks the branch whose copy of the workflow, scripts and overlays runs:

```bash
gh workflow run amd-bench-probe.yml --ref <branch> -f probes="guest-facts ernic-backend-nvmeof"
```

`probes` is a list separated by spaces, `all` by default. An unknown name
fails the run.

| Probe | Does | Artifact |
| --- | --- | --- |
| `guest-facts` | Boots the smoke test's guest and runs [`amd_bench_probe_guest.py`](../scripts/amd_bench_probe_guest.py) in it: limits, disk, kernel modules, tools, ROCm, NVMe, RDMA, the cost of `apt-get update`. Records the runner's free disk, CPUs and memory before the boot, after it and at the end. | `amd-bench-probe-guest-facts` |
| `ernic-backend` | One job per backend, `loopback`, `nvmeof` and `s3`; `ernic-backend-<lane>` selects one. Boots one guest with that rocm-ernic backend and uses it from the guest. | `amd-bench-probe-ernic-backend-<lane>` |
| `container-uring` | Calls `io_uring_setup` in an `ubuntu:24.04` job container, once with the default seccomp profile and once unconfined. Allowed to fail. | `amd-bench-probe-container-uring-<seccomp>` |

Each job writes a table of its checks to the run's summary page. The artifact
holds the same checks as `checks.json`, with `guest-facts.json` or
`io-uring.json` beside it where the guest script ran. A check is `pass`,
`fail`, `warn`, `skip` or `info`; only `fail` fails the job, and several checks
are there to record an answer, not to pass. The two VM jobs also upload a
`-diagnostics` artifact with the guest and container logs, the override file
and the merged compose configuration.

## Selecting the rocm-ernic backend

qemu-tool always starts the NIC server on its `tcp` mesh and has no setting for
anything else; the server takes its backend from its command line only. The
`ernic-backend` job therefore writes a `docker-compose.override.yml` into the
stack directory before `quick-start up`. quick-start leaves that file alone and
runs compose in that directory with no `-f`, so compose merges it. The
override replaces two things for `ernic-1`: the `command`, to carry the wanted
`-b <backend>`, and the healthcheck, which otherwise waits for a mesh
connection that never comes.

Its limits:

- **One guest.** A second NIC server would wait for a mesh manager that no
  longer exists, and the boot would fail.
- **No TAP.** With `<ernic-tap>true` qemu-tool starts the server through an
  entrypoint, which ignores a replaced `command`.
- **No P4.** The override replaces the whole command, P4 flags included. To
  keep P4, restate `--p4 --p4-guest-scope load` in the override.
- The override repeats the socket path, the MAC and the statistics path that
  qemu-tool generates. The job compares them with the generated file and fails
  when they differ.
- Only `quick-start up` and `down` are safe; `test` and `report` assume the
  mesh.

[`fleet-overlay-backends.xml`](../rocjitsu/fleet-overlay-backends.xml) is
`fleet-overlay.xml` with P4 off, so that the generated file does not claim
otherwise. The job refuses any overlay that does not set one guest and no TAP,
and after the boot checks that the server reports the requested backend and
that its container has no entrypoint.

A first-class backend setting in qemu-tool's fleet file has been requested.
Until it exists, a two-guest or TAP stack cannot use these backends.

## Two NVMe paths

The programme measures two different NVMe devices, as separate lanes:

| | rocm-ernic `nvmeof` | SPDK |
| --- | --- | --- |
| What it is | An NVMe-over-Fabrics target inside the emulated RDMA NIC's server | A local PCIe NVMe controller that SPDK presents to the guest |
| The guest reaches it by | `nvme connect -t rdma` across the emulated NIC | The PCI bus; no network |
| Controller `transport` in sysfs | `rdma` | `pcie` |
| Namespaces | LBA and Key Value | LBA and Key Value |
| Available | Now, with the override above | With the SPDK stack of qemu-tool, at a later pin |

A Key Value namespace has no block device, only a generic one, `/dev/ngXnY`.
Device names do not follow namespace ids, and the guest also has the spare
NVMe that QEMU itself emulates, which is `pcie` too. Find a controller by its
transport and NQN in sysfs, never by its index: the `nvmeof` probe does, and
never writes to a `pcie` disk.

The `nvmeof` target is not a host on the segment and answers no ARP, so the
guest needs a static neighbour entry for its address; any link-layer address
will do. The `s3` endpoint answers ARP itself.

## Experiments

Phase 0 ends when every row has an answer. Replace "not yet run" with the
answer and the URL of the run it came from.

| # | Question | How to run it | Answer |
| --- | --- | --- | --- |
| E1 | How long do `hipMemset` and a whole-buffer read-back take for 64 to 256 MiB in the ROCm 10.1 guest? | No probe yet. | not yet run |
| E2 | What does the guest offer (`ulimit -l`, `ais-stats`, `nvme-rdma` and `nvme-fabrics`, free disk, CPUs, memory, the cost of `apt-get update`), and how much disk does the runner have before and after a VM job? | `probes=guest-facts`; the runner figures are in every VM job. | not yet run |
| E3 | Does nixlbench compile with the manylinux toolchain against the ROCm 10.1 HIP headers, do it and every plugin resolve their libraries in the guest and in `ubuntu:24.04`, and how long does the bundle job take? | No probe yet; needs the bundle job. | not yet run |
| E4 | Does a block of 16 MiB or more fail cleanly on the mesh; what is the limit on `loopback`; does an interrupted RDMA transfer leave the device usable? | No probe yet; needs nixlbench. `probes=ernic-backend-loopback` shows only that `loopback` carries a ping-pong. | not yet run |
| E5 | Do two nixlbench processes work on device 0 with VRAM in one guest? | No probe yet; needs nixlbench. | not yet run |
| E6 | What are the spread between rows and the duration of a case in each job type? | No probe yet; needs nixlbench. | not yet run |
| E7 | Does `queue: max` work on job-level concurrency, so that every `gh-pages` writer can share one group? | No probe yet. | not yet run |
| E8 | Does `libplugin_LIBFABRIC.so` load in each environment, which libfabric does it resolve, and which interface does it bind across guests? | No probe yet; needs the wheel and the bundle. | not yet run |
| E10 | For `nvmeof`: is the TAP needed; does `ip=` on the guest's subnet work, and with which neighbour address; does the multi-namespace grammar parse; do the LBA and KV namespaces appear? | `probes=ernic-backend-nvmeof`. Two NIC servers with different backends in one guest are not covered. | not yet run |
| E19 | Does `--security-opt seccomp=unconfined` in a job container's options enable io_uring on hosted runners? | `probes=container-uring`. | not yet run |
| E20 | Which S3 server suits the container lanes: versitygw, Garage or SeaweedFS, or a frozen MinIO binary? | No probe yet; needs nixlbench. | not yet run |

The `ernic-backend` job also records what the override depends on:

| Check | Question | Answer |
| --- | --- | --- |
| `compose.merge` | Does compose replace, not append to, `command` and `healthcheck.test`? | not yet run |
| `backend.container` | Is the compose project named after the stack directory? | not yet run |
| `image.backends`, `image.backend-string` | Does the pinned NIC server image have `loopback`, `nvmeof` and `s3`, and accept the backend strings? | not yet run |
| `guest.nvme-rdma` | Does the guest kernel have `nvme-rdma`? | not yet run |
| `nvmeof.namespaces`, `nvmeof.kv-generic-node` | How are the namespaces named in sysfs, and does the KV one appear as `/dev/ngXnY` only? | not yet run |
| `s3.put`, `s3.get`, `s3.rdma-client` | Does the S3 store take and return an object over HTTP and over RDMA? | not yet run |
| `server.state` | How much memory does the NIC server use with RAM-backed namespaces? | not yet run |
| none | Does `--p4` work beside `nvmeof` or `s3`? Not covered by the probe. | not yet run |
