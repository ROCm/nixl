<!--
SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# AMD nixlbench programme

This is for maintainers of the AMD fork of NIXL. It covers what the nightly
nixlbench programme is for, the nixlbench wheel, the `AMD Nightly Bench`
workflow ([`amd-nightly-bench.yml`](../../.github/workflows/amd-nightly-bench.yml))
and its lanes, the `AMD Bench Probe` workflow
([`amd-bench-probe.yml`](../../.github/workflows/amd-bench-probe.yml)) that
answers the questions the programme is designed around, and the answers once
they exist. The branches, the nightly and the results site are in
[amd-ci.md](amd-ci.md).

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
| 1 First lanes | A nixlbench wheel built beside the NIXL wheels; container and one-guest lanes and the two-guest network lane, with records kept as artifacts only. The one-guest fabric lanes (loopback, NVMe-oF, S3) start here as a job that is allowed to fail. |
| 2 Data and site | The publisher and the bench pages. The Parquet shards live on `gh-pages` of this repository under `data/bench/v1/`, and the query engine beside the site under `perf/`, until their size calls for a repository of their own. |
| 3 Suites | The unit and plugin test suites, in containers and in the guest. |
| 4 Single-guest fabric | The fabric lanes become gating once they have passed for ten nights. |
| 5 Local PCIe | The SPDK lanes, and the comparison of the same workload on SPDK and on NVMe-oF. |
| 6 Product branches | The `for-upstream/` changes nixlbench needs: JSON output, the `NVME_KV` plugin, the hipObject engine, libfabric over verbs. A bench-only job merges the listed branches in its workspace, pushes nothing, and builds only the nixlbench wheel. |
| 7 Depth | A weekly deep tier, failure cases and regression flags. |

Key Value starts on the SPDK controller: the vendor-neutral `NVME_KV` plugin
is the upstream proposal, the SPDK KV namespace is its first target, and the
rocm-ernic KV namespace follows on the same plugin. The site's Node and React
source will be under `.ci/site/`.

## The nixlbench wheel

nixlbench ships as a wheel of its own, `rocm-nixlbench`, which installs the
`nixlbench` command and requires `rocm-nixl` at exactly its own version. A
branch can build it when it has the nixlbench wheel scripts; today that is
`for-upstream/feat/rocm-wheels`, whose `contrib/README.md` describes the
wheel
([here](https://github.com/ROCm/nixl/blob/for-upstream/feat/rocm-wheels/contrib/README.md#nixlbench-wheel)).
What the lanes depend on:

- It has no ETCD runtime, so every network case runs with
  `--runtime_type ASIO`.
- It needs a ROCm install and OpenSSL 3 on the machine. It finds ROCm under
  `$ROCM_PATH` or `/opt/rocm`.
- `NIXLBENCH_WHEEL_DEBUG=1` makes it print which backend, libraries and ROCm
  install it selected.

`ROCm Wheels` ([`rocm-wheels.yml`](../../.github/workflows/rocm-wheels.yml))
builds it in two places, neither of which is a required job or step:

| Where | Does | Artifact |
| --- | --- | --- |
| The last steps of `build`, one leg per ROCm version | Builds `nixlbench_rocm<X>_<Y>` after the NIXL backend wheels have been uploaded, from the same build cache, so that it links against that same NIXL build. Checks that the NIXL wheels it was linked against are the uploaded ones, by hash, and uploads nothing otherwise. | `rocm-nixlbench-backend-<rocm><suffix>` |
| The `nixlbench wheel` job, after `merge` | Checks each backend again against the NIXL backend wheels of this run, drops one that does not match, and merges the rest into `rocm_nixlbench-<version>-py3-none-manylinux_2_28_x86_64.whl`. | `rocm-nixlbench-wheel<suffix>`, with `nixlbench-build.json` |

Each of those steps is allowed to fail, and so is the job: a nixlbench failure
is a warning on the run and a `nixlbench` entry in `results.json`, never a
failed `build` or a missing `rocm-nixl` wheel. The build is stopped after
`NIXLBENCH_CAP_S` (1080 seconds), and not started at all when the runner has
less than `NIXLBENCH_MIN_FREE_GB` (8 GB) of disk free.

The repository variable `AMD_NIXLBENCH_WHEEL` decides whether a run builds it:

| Run | Unset | `true` | `false` |
| --- | --- | --- | --- |
| `ROCm Wheels` started by hand | Built | Built | Not built |
| The nightly, a pull request, a tag | Not built | Built | Not built |

"Built" holds for a branch that has the nixlbench wheel scripts. The wheels
branch, which a pull request and a tag build, does not have them today. A run
started by hand never publishes, so it builds the wheel without the variable.
A plan step says so in the run's summary when a source could build the wheel
and the run does not.

**The nightly builds no nixlbench wheel until the variable is `true`**, so
until then `AMD Nightly Bench` finds nothing to run after a night. Setting it
accepts two things:

- Up to 25 minutes more in each leg of `build`, and so before the nightly's
  wheels are published. `merge` waits for `build`, and the wheel publisher
  starts when the whole nightly has ended. The nixlbench build itself is
  stopped after 18 minutes (`NIXLBENCH_CAP_S`) and its step after 20; the
  check, the upload and the report that follow have step limits of 2, 2 and
  1 minutes. The legs run side by side, so the limits do not add up across
  ROCm versions. `Results` can spend up to 2 minutes more on the nixlbench
  entry of `results.json`, and waits for the `nixlbench wheel` job (10
  minutes at most), which matters only if that job outlasts the tests.
- A runner lost during those steps fails that leg of `build`. The cap and the
  disk floor make that unlikely and cannot rule it out. It can reach the
  published wheels only once the wheels branch itself has the nixlbench
  scripts.

To switch it off again, unset the variable or set it to `false`; `false` also
stops the runs started by hand from building the wheel.

## The bench workflow

`AMD Nightly Bench` starts when an `AMD Nightly` run completes, and by hand on
the id of an `AMD Nightly` or `ROCm Wheels` run. It runs the wheels that run
built, so it refuses a run of any other workflow or of a fork. Its jobs:

1. **plan.** Lists the run's artifacts. A branch is benchmarked when the run
   holds both its `rocm-nixl-wheels<suffix>` and its
   `rocm-nixlbench-wheel<suffix>`, at most four branches in one run. The lanes
   and their cases come from the matrix under [`.ci/bench`](../bench).
2. **container** and **vm.** One job per branch and lane, below.
3. **collect.** One job per branch. It merges the lanes' records with
   [`amd_bench_collect.py`](../scripts/amd_bench_collect.py), adds a record
   for every case the matrix expects and no lane reported, and fails when a
   required case did not pass. No case is required yet.

**Nothing is published.** The workflow has read-only tokens, pushes nothing to
`gh-pages` and uploads nothing to a release; the records exist as artifacts of
the bench run only. It cannot change the result of `AMD Nightly` or of
`AMD Nightly Wheels`. No bench job runs on a pull request; `AMD CI Tests`
checks the matrix and the scripts there (see [amd-ci.md](amd-ci.md)).

### Lanes

| Lane | Runs in | Env template | Offers at most | Tiers |
| --- | --- | --- | --- | --- |
| `bench-container` | An `ubuntu:24.04` job container with the ROCm runtime and `seccomp=unconfined`, no GPU. One job per ROCm version in the nixlbench wheel. | `gha-container` | `io_uring`, `redis` | `pr`, `nightly` |
| `vm1-local` | One rocjitsu guest with two spare NVMe drives ([`fleet-overlay-bench-local.xml`](../rocjitsu/fleet-overlay-bench-local.xml)): ext4 on the first, the second raw. | `gha-kvm-vm1` | `blkdev`, `gpu`, `io_uring` | `pr`, `nightly` |
| `vm2-net` | Two guests on the rocm-ernic mesh ([`fleet-overlay-bench-net.xml`](../rocjitsu/fleet-overlay-bench-net.xml)). | `gha-kvm-vm2` | `gpu`, `rdma`, `two_hosts` | `nightly` |
| `vm1-loop` | One guest and the rocm-ernic `loopback` backend ([`fleet-overlay-backends.xml`](../rocjitsu/fleet-overlay-backends.xml)). | `gha-kvm-vm1-loopback` | `gpu`, `rdma` | `nightly` |
| `vm1-fabric` | One guest and the rocm-ernic `nvmeof` backend, the same overlay. | `gha-kvm-vm1-nvmeof` | `blkdev`, `gpu`, `io_uring` | `nightly` |

The container lane is [`amd-bench-container.yml`](../../.github/workflows/amd-bench-container.yml)
and the four VM lanes are [`amd-bench-vm.yml`](../../.github/workflows/amd-bench-vm.yml).
The VM lanes run the ROCm 10.2 backend only, which is what the guest image
of qemu-tool v2.1.0 carries. The runner,
[`amd_bench_run.py`](../scripts/amd_bench_run.py), runs on the hosted runner
and reaches each guest over SSH.

The VM jobs boot their guests with
[qemu-tool-action](https://github.com/sbates130272/qemu-tool-action), pinned
by commit. Each fleet is qemu-tool's own `rocm-golden.xml` with two overlays
on top: [`fleet-overlay-common.xml`](../rocjitsu/fleet-overlay-common.xml),
which pins the QEMU, rocm-ernic, rocjitsu and guest images by digest, and the
lane's own. The action's pin, `QEMU_MINIMAL_REF` and those four references
move together. A job fails when the qemu-tool it installed is not
`QEMU_MINIMAL_REF` or, after the boot, when an image tag in the overlay is not
the one `rocm-golden.xml` names.

An env template, `.ci/bench/envs/<env class>.toml`, says what a lane's
environment looks like. The job renders it to the JSON file the runner reads,
with the `env` command of [`amd_bench_ci.py`](../scripts/amd_bench_ci.py). That
sets the values written `"@job"` in the template (the SSH configuration, a
device path, UCX's device list) and takes out of `provides` what the job could
not set up. A case that needs something the environment does not provide is
recorded as `skip` with the reason `env`, and the lane goes on:

| Need | Taken out when | Cases that then skip |
| --- | --- | --- |
| `io_uring` | `io_uring_setup` is refused in the job container. | POSIX with `URING` |
| `redis` | `redis-server` could not be installed or does not answer. | REDIS |
| `s3` | Always: no S3 server is started until one is chosen (E20). | OBJ |
| `gpu` | The GPU firmware or `amdgpu` could not be loaded in a guest. | Every case with VRAM, and AIS |
| `blkdev` | The guest has one spare drive only, or its user cannot keep access to the raw device. | GUSLI |
| `rdma` | The RDMA device of a guest could not be named `ernic0`, or its port is not active. In `vm2-net` the tcp cases still run. | UCX over rc and ud |

The templates also name the probes the runner runs between cases: the GPU's
compute device node after each case that used the GPU, and the RDMA port
after each case that used it, in every guest. After a failed probe the
remaining cases of that device are recorded as `skip` with the reason
`device-suspect`.

Each VM template has one more probe, `cpu-amd-zen`, run once in every guest
at the start. It is the run's record of the guest's CPU, as a `test` record
of the suite `probe`: `pass` on an AMD CPU of family 23 (Zen) or later, and
otherwise `fail` with the vendor, family and model name in its excerpt. It
makes no device suspect and no case is skipped for it; see
[The guest's CPU and peer-to-peer DMA](#the-guests-cpu-and-peer-to-peer-dma).

### Budgets and failures

| Lane | Job timeout | Runner's budget | Allowed to fail |
| --- | --- | --- | --- |
| `bench-container` | 45 minutes | 33 minutes from the job's first step | No |
| `vm1-local` | 60 minutes | 46 minutes | No |
| `vm2-net`, `vm1-loop`, `vm1-fabric` | 60 minutes | 46 minutes | Yes |

The runner starts no case that could not end, kill included, before the
budget does; the rest are recorded as `skip` with the reason `budget` and the
run as `partial`. What is left of the job is for the last probes, the
diagnostics, the uploads and the teardown.

No case is `required` yet, so a case that fails is a record and does not fail
its lane. A lane job fails when its set-up fails, when
`nixlbench --version` does not run from the installed wheels, or when the
runner itself cannot run. For the three lanes that are allowed to fail that
is a warning on the bench run; for `bench-container` and `vm1-local` it fails
the bench run, and nothing else.

At most three container jobs and four VM jobs run at once (`max-parallel`),
beside `collect`. They start when the nightly ends, which is also when
`AMD Nightly Wheels` starts, and the organisation's limit on concurrent jobs
has not been verified. Setting the repository variable `AMD_NIGHTLY_BENCH` to
`false` stops the bench from following the nightly, with no pull request; a
run started by hand still works.

### Artifacts

`<rocm>` is the ROCm version as X.Y.Z; `<suffix>` is the nightly's `-<slug>`
for the branch, or empty for a `ROCm Wheels` run started by hand.

| Artifact | From | Holds |
| --- | --- | --- |
| `bench-records-<lane>-<rocm><suffix>` | Each lane job | `records.ndjson` and `logs/<case>/` with the output of every process. |
| `bench-diagnostics-<lane>-<rocm><suffix>` | Each lane job | `runner.log`, the preflight output (`nixlbench --version`, `ldd`), the rendered `env.json` and `run-meta.json`, `e1-vram-timing.txt`; for a VM lane also the guests' kernel logs and device state, the NIC server's statistics, the compose logs, the fleet files the guests were booted from (`fleet-layers.txt`), the action's own logs (`action-logs/`) and the files listed under [The guest's CPU and peer-to-peer DMA](#the-guests-cpu-and-peer-to-peer-dma). |
| `bench-collected<suffix>` | `collect` | `bench-<stamp>-<slug>-<run>.a<attempt>.ndjson.gz` (and `.zst`), the logs as `.logs.tar.gz`, `matrix-<sha256>.json`, `collect.json` and `summary.md`. |

### The fabric lanes

`vm1-loop` and `vm1-fabric` select the NIC server's backend with the compose
override described under
[Selecting the rocm-ernic backend](#selecting-the-rocm-ernic-backend), with the
same guards as the probe: the overlay must set one guest and no TAP, the
pinned image must accept the backend string, and after the boot the server
must report the requested backend.

Both give the guest's RDMA netdev its address, 192.168.100.11. `vm1-loop`
then names the RDMA device `ernic0`, whatever it was called. Its template and
probes use that name, so a device that cannot be renamed, or whose port is not
`ACTIVE`, takes `rdma` out instead of running cases against a guess.
`vm1-fabric` runs no RDMA case and offers no `rdma`: the kernel's `nvme-rdma`
uses the device under whatever name it has, and a connect that fails, for a
missing or inactive device as for anything else, fails the lane.

`vm1-fabric` starts the target with
`nvmeof:ip=192.168.100.1,size=1024M,bs=4096,ns=2,size=64M,bs=512,ns=3,kv=1,size=64M,keys=4096`:

| nsid | Size, block | Use |
| --- | --- | --- |
| 1 | 1 GiB, 4096 bytes | ext4 at `/mnt/amd-bench/fs`, for the POSIX and AIS cases |
| 2 | 64 MiB, 512 bytes | Raw, for the GUSLI cases |
| 3 | Key Value | Not written. Its generic node is linked at `/run/amd-bench/kv`, and one probe at the start of the run records whether it is there. |

The guest connects with `nvme connect -t rdma` after loading `nvme-fabrics`
and `nvme-rdma` and adding the static neighbour entry (see
[Two NVMe paths](#two-nvme-paths)). The guest also has local NVMe drives, and
a wrong pick would be executed as a write, so the lane holds to these rules:

- The controller is the only one in sysfs whose transport is `rdma` and whose
  NQN is the target's, and its address names the target. None, or more than
  one, fails the lane.
- A namespace is found by its `nsid` under that controller, never by its
  device name.
- Before every `mkfs`, write or udev rule the lane checks again that every
  controller behind the block device is that one, that the namespace id is the
  expected one, that nothing is mounted from it and it carries no signature,
  and that its block size and size are those of the backend string. Any
  difference fails the lane with nothing written.
- A `pcie` controller is never written.
- The lane disconnects from the target only at the end of the job.

`vm1-local` picks its two drives with the same care: `pcie` controllers other
than the one that holds `/`, unmounted, with no partition and no signature, of
exactly 1 TiB, in the order of their serial numbers. With one such drive the
lane runs without the raw device; with none it fails.

The two lanes run the same storage workloads on different namespaces: 1 TiB
drives with 512-byte blocks in `vm1-local`, the two namespaces above in
`vm1-fabric`. A record tells them apart by its `attach` (`pcie-qemu` or
`fabric-rdma`). The fields that would describe the namespace itself
(`target_impl`, `ns_type`, `ns_lba_bs`, `ns_bytes`) are not filled in yet and
are `null` in every record; the block size and size the lane checked are in
its diagnostics artifact.

Both lanes hand the raw device to the guest's user with a udev rule, not with
`chown`: udev puts the owner back whenever a writer closes the device, which
the lane's own seeding and GUSLI both do. The lane writes the pattern the
GUSLI READ case checks for (32 MiB of `0xaa`, 1 MiB into the device), reads it
back as that user, and checks that the access survives a writer closing the
device. If not, `blkdev` is taken out.

### The wait on emulated VRAM (E1)

Every wait of nixlbench's ASIO runtime gives up after 15 seconds, and each
process allocates and fills its buffer between two of them. In a pair where
one side has DRAM and the other VRAM, the DRAM side waits while the other
allocates on the emulated GPU. If that takes more than 15 seconds the case
aborts, and the cases after it may be recorded as `device-suspect`. That
affects the DV and VD cases: 4 in `vm1-local`, 12 in `vm2-net`, 8 in
`vm1-loop`.

It shows as:

- `error_class` `asio` on the case record;
- `ASIO Receive timeout` in the standard error of the DRAM side;
- a `proc_wall_s` counter of about 15 seconds plus start-up.

`e1-vram-timing.txt` in the diagnostics artifact lists every case with VRAM,
its status, its `error_class` and the wall time of each process, with the
number of `asio` failures at the top. **A DV or VD failure of this shape is
E1, not a defect of the wheel.** The remedies are a smaller VRAM buffer in
the profiles or a longer wait in nixlbench.

### Running it by hand

Build the wheels, then benchmark that run:

```bash
gh workflow run rocm-wheels.yml -f ref=for-upstream/feat/rocm-wheels
gh workflow run amd-nightly-bench.yml -f run-id=<id of that run> \
  [-f lanes="<lanes>"] [-f tier=pr] [-f only="<patterns>"]
```

- `run-id` is an `AMD Nightly` or a `ROCm Wheels` run that has completed.
- `lanes` is a list separated by spaces, such as `"vm1-loop vm1-fabric"`, and
  `all` by default. An unknown name fails the plan.
- `tier` is `nightly` by default. `pr` runs the short cases only, and only
  `bench-container` and `vm1-local` have any, so the other lanes are left out
  with a notice. `weekly` adds the experiment cases to the nightly ones, with
  five rounds of each sentinel; no schedule runs it, and it is meant to be
  used with `only`, see
  [Experiments you can dispatch](#experiments-you-can-dispatch).
- `only` is a list of case key patterns separated by spaces, such as
  `"vm1-loop/ucx/rc/*"`; the other cases of a lane are recorded as `skip` with
  the reason `filter`.

To see what a lane would run without running it:

```bash
python3 .ci/scripts/amd_bench_run.py list --lane vm1-loop --tier nightly \
  --env .ci/bench/envs/gha-kvm-vm1-loopback.toml --commands
python3 .ci/scripts/amd_bench_run.py validate --lane vm1-loop \
  --env .ci/bench/envs/gha-kvm-vm1-loopback.toml
```

### The guest's CPU and peer-to-peer DMA

A device that reads or writes GPU memory directly, the RDMA NIC or an NVMe
drive, does peer-to-peer DMA. Across a host bridge the guest's kernel allows
that only on an AMD CPU of family 23 (Zen) or later (`cpu_supports_p2pdma()`
in `drivers/pci/p2pdma.c`). qemu-tool starts every guest with `-cpu EPYC`,
which under KVM sets the family and the model name and may leave the vendor
that of the runner's own CPU, and a hosted runner is not always AMD. No
setting of the fleet XML changes that CPU string at the pinned qemu-tool
(v2.1.0): it is fixed in `run_vm.py`, and the only setting that reaches it is
the integer `vm-cpu-phys-bits`. Making a guest report AuthenticAMD on any
runner therefore needs a change to qemu-tool, a `vendor=AuthenticAMD` on its
`-cpu` argument, and a new `QEMU_MINIMAL_REF` here. That change has been
requested for qemu-tool's next release; whether such a guest then runs these
cases is not known until it is tried.

In the first run of the lanes `vm1-local` drew an Intel runner. Its two AIS
`fast-required` cases failed with "No such device", and the guest's kernel
logged `amdgpu 0000:02:00.0: AIS: P2P DMA not accessible` so often that the
boot messages, which name the CPU, were gone from the log by the end of the
lane. The vendor that guest showed is therefore not known. Every VM lane now
records it, after the GPU is bound and before the first case:

| Where | Holds |
| --- | --- |
| A notice on the lane job | The vendor, family, model and model name each guest reports, and the runner's CPU. |
| A warning on the lane job | Only when a guest's CPU is not an AMD one of family 23 or later: peer-to-peer DMA is expected to be refused, so the cases that move data between VRAM and the NIC or an NVMe drive are expected to fail on this runner. |
| The `test` record `cpu-amd-zen/start` (and `cpu-amd-zen-tgt/start` in `vm2-net`) | `pass` or `fail`, as above. The runner's own CPU is `runner_cpu_model` of the run record. |
| `runner-cpu.txt` | The first processor of the runner's `/proc/cpuinfo`, and `lscpu`. |
| `<guest>-facts.txt` | The guest's CPU and `lscpu`; the lines of its kernel log that mention p2p; the `p2pmem` directory of each AMD PCI function, when it has one, and the PCI tree; the properties of each KFD node (`simd_count`, `array_count`, `cu_per_simd_array` and the rest) and of its memory banks; the ROCm version, the HIP, HSA and hipFile libraries with their versions, and `hipconfig --version`. |
| `<guest>-dmesg-start.txt` | The whole kernel log before the first case. |
| `<guest>-dmesg-p2p.txt` | At the end of the lane: how many lines of the kernel log mention p2p, and the 40 most frequent without their timestamps. A notice on the job says so when there are any. |
| `notes.txt` | The text of the notice and of the warning. |

**Nothing is gated on the CPU.** No case is skipped or expected to fail in
its record, and no need is withheld: that an Intel runner explains those
failures is not yet proven, and the record is what will show it.

### Experiments you can dispatch

The first run of the lanes (bench run 44820022442) left three questions, and
one dispatch answers them. The cases for it are in the `weekly` tier, with
`x-vram` as the second segment of their key, so no nightly and no pull
request runs one. Each is a nightly case with one thing changed, and its key
is that case's with `x-vram/<change>/` after the lane.

```bash
gh workflow run amd-nightly-bench.yml -f run-id=<id> -f tier=weekly \
  -f lanes="vm2-net vm1-loop vm1-fabric" \
  -f only="*/x-vram/* vm2-net/ucx/rc/*/*/S vm1-loop/ucx/rc/*/*/S vm1-fabric/ais/*/*/S vm2-net/ucx/rc/VV/WRITE/N-r vm1-fabric/ais/compat-allowed/WRITE/N"
```

`<id>` is an `AMD Nightly` or `ROCm Wheels` run whose wheel artifacts have
not expired. The first pattern selects the 33 experiment cases. The other
five select the 22 nightly cases they are compared with, so that both run
on the same runner in the same job: the 20 rc and AIS cases at `S`, and the
two at `N-r` and `N` that `buf32` is compared with. The nightly
`ucx/ud/VD/WRITE/S` is left out, because `long` is that case with more time.
Every other case of the three lanes is recorded as `skip` with the reason
`filter`. Add `--ref <branch>` while the cases are on a branch only: the
tier, the matrix and the scripts are those of the ref the workflow is started
at.

**1. Is it the size of the buffer, or the timing?** 14 cases failed
nixlbench's data check: rc with GPU memory in `vm2-net` (6) and between host
and GPU memory in `vm1-loop` (4), and AIS in `vm1-fabric` (4). In each a
device does DMA on GPU memory and the profile is `S`, which has a 32 MiB
buffer and makes all of its transfers within about 0.1 s, soon after the
buffer is filled. The same transfers passed at `N` and `N-r`, whose VRAM
buffer is 64 MiB and which run for longer. One explanation, not proven, is
that nixlbench fills the buffer with a `hipMemset` it does not wait for, so
that the fill and the device write the same memory at once.

| Change | Cases | Differs from the nightly case in |
| --- | --- | --- |
| `buf64` | `vm2-net/x-vram/buf64/ucx/rc/{VV,DV,VD}/{WRITE,READ}/S`, `vm1-loop/x-vram/buf64/ucx/rc/{DV,VD}/{WRITE,READ}/S`, `vm1-fabric/x-vram/buf64/ais/{compat-allowed,fast-required}/{WRITE,READ}/S` | `total_buffer_size` 67108864 in place of 33554432. |
| `buf32` | `vm2-net/x-vram/buf32/ucx/rc/VV/WRITE/N-r`, `vm1-fabric/x-vram/buf32/ais/compat-allowed/WRITE/N` | `total_buffer_size` 33554432 in place of 67108864, in a case that passed. |
| `warm` | The 14 keys of `buf64`, with `warm` for `buf64` | `warmup_iter` 16384 in `vm2-net`, 65536 in `vm1-loop` and 8192 in `vm1-fabric` in place of 16. An iteration is a batch of 4 blocks, and in the first run took about 0.8 ms in `vm2-net`, 0.2 ms in `vm1-loop` and 1.6 ms in `vm1-fabric` (the latter at `N`: AIS printed no row at `S`). At those rates each is a warm-up of about 13 s, where the 80 iterations of `S` take 16 to 125 ms and its whole process lives 2 to 6 s. |

| Result | Means |
| --- | --- |
| The nightly case fails, `buf64` passes and `buf32` fails | The 32 MiB buffer itself: look at what the HIP runtime, the driver or the emulated GPU does differently for that size. |
| The nightly case and `buf64` fail, `buf32` passes | Not the size: something else that `S` and the larger profiles differ in, of which the time between the fill and the first transfer is one. |
| `warm` passes where the nightly case fails | The result depends on how old the buffer is, which is what a fill that is still running predicts. Waiting for the fill in nixlbench is then the fix to try. |
| `warm` fails as well | Not a race that ends: the device reads or writes other memory than the process sees, however long it runs. |
| The nightly cases pass this time | Not deterministic. Compare the runners' CPUs of the two runs, and dispatch again. |

**2. How was the GPU memory registered?** `vm2-net/x-vram/ucxdebug/ucx/rc/VV/WRITE/S`
is the failing rc case with `UCX_LOG_LEVEL=debug`, for its log alone; its
status may differ from the nightly case's because logging slows it. In
`init.ucx.log` and `tgt.ucx.log` of its log directory look for `dmabuf is
supported` or `dmabuf is not supported` from the RDMA device and from ROCm,
and for `failed to export dmabuf handle`. Those lines are what the case is
for, not the protocol table that every UCX case logs: in the first run that
table read the same in the failing `S` case and in the passing `N-r` case.
The initiator of an rc `VV` `WRITE` has no entry "from rocm" at all; its own
VRAM buffer is listed as "from host memory to rocm", `zero-copy` over
`rc_verbs/ernic0:1`. That the local buffer is classed as host memory is a
lead on how it was registered; what it implies is not verified. UCX logs
each `ibv_reg_dmabuf_mr` call at the trace level only, which a release build
may not have.

**3. Is ud from GPU to host memory slow, or stuck?** `ucx/ud/VD/WRITE/S`
printed no row in 180 s in `vm2-net` and in `vm1-loop`, where the other ud
cases take seconds. `vm1-loop/x-vram/long/ucx/ud/VD/WRITE/S` has 900 s, and
`vm1-loop/x-vram/short/ucx/ud/VD/WRITE/S` runs 8 iterations in place of 80.
They are the last two cases of the lane, so that one the runner has to kill
costs no other case its device, and `long` is the first of the two, so that
`short` cannot cost it its own. A case of the weekly tier may have a timeout
of up to 900 s; any other, 480 s. As for every case, the runner starts `long`
only if it could end, kill included, within the lane's
[budget](#budgets-and-failures): 930 s of the 46 minutes must be left when
its turn comes, about 3 minutes after the first case.

| Result | Means |
| --- | --- |
| Both pass | Slow. The durations give the time an iteration takes; fewer iterations or a longer timeout make the nightly case pass. |
| `short` passes, `long` times out | It stalls after some iterations, or an iteration takes more than 11 s. |
| Both time out | Stuck, or an iteration takes more than 22 s. |
| `long` is `skip` with the reason `budget` | The boot and the installation left it less than 930 s. Dispatch again with `lanes` set to `vm1-loop` and `only` to `*/x-vram/long/*`, so that it is the first case started. |
| `long` times out and `short` is `skip` with the reason `device-suspect` | `long` did not stop when asked and the runner had to kill it. For the rate, dispatch again with `only` set to `*/x-vram/short/*`. |

The same run records the CPU of every guest it boots; see
[The guest's CPU and peer-to-peer DMA](#the-guests-cpu-and-peer-to-peer-dma).
Which runner a job draws is not chosen. For one more draw, and the AIS cases
that failed on the Intel runner, add `vm1-local` to `lanes` and
`vm1-local/ais/*` to `only`.

The dispatch starts three VM jobs side by side. At the rates of the first run
the selected cases take about 5 minutes in `vm2-net`, 3 in `vm1-loop` before
the two ud cases and 2 in `vm1-fabric`, on top of the boot and the
installation every lane job has. The two ud cases add between seconds and 19
minutes, the latter if both run to their timeout. The nightly runs none of
these cases; what it gains is the record of the CPU, a few seconds in each VM
lane.

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
| `guest-facts` | Boots the guest of the `vm1-local` lane and runs [`amd_bench_probe_guest.py`](../scripts/amd_bench_probe_guest.py) in it: limits, disk, kernel modules, tools, ROCm, NVMe, RDMA, the cost of `apt-get update`. Records the runner's free disk, CPUs and memory before the boot, after it and at the end. | `amd-bench-probe-guest-facts` |
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

The pinned qemu-tool (v2.1.0) always starts the NIC server on its `tcp` mesh
and has no setting for anything else; the server takes its backend from its
command line only. The
`ernic-backend` job therefore writes a `docker-compose.override.yml` into the
stack directory before the boot. The action and quick-start, `--clean-overlays`
included, leave that file alone, and quick-start runs compose in that directory
with no `-f`, so compose merges it. The
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

[`fleet-overlay-backends.xml`](../rocjitsu/fleet-overlay-backends.xml) sets
one guest, no TAP and P4 off, the last so that the generated file does not
claim otherwise. The job refuses any overlay that does not set one guest and
no TAP, and after the boot checks that the server reports the requested
backend and that its container has no entrypoint.

A first-class backend setting in qemu-tool's fleet file exists on its main
branch and is in no release yet. Until the pin can move to one, a two-guest or
TAP stack cannot use these backends.

What is kept only until something else happens, to be removed then:

- **A qemu-tool release with the backend setting:** the override, the limits
  above, and the steps of `amd-bench-vm.yml` and of the probe that write it,
  check how compose merged it and compare what it repeats.
- **A qemu-tool release that can set the guest's CPU vendor:** nothing is
  removed. The pin moves, and the section on the guest's CPU is rewritten once
  a run shows what such a guest does.
- **A qemu-tool-action release** with a GPU bring-up that reports without
  failing the boot, host aliases of the caller's choice and the digests of the
  images it ran: the lanes' own GPU step, the SSH configuration rewritten from
  the action's manifest, and the comparison of the overlay's tags with
  qemu-tool's fleet file.
- **The first run in which all four lanes pass:** what only confirms that the
  action did as it was asked, namely the comparison of each container's image
  in `fleet-check` and the retries around the first SSH login.
- **A decision of our own:** `<vm-nvme-vram>false` in
  [`fleet-overlay-common.xml`](../rocjitsu/fleet-overlay-common.xml), which
  keeps the guests' NVMe drives off GPU memory as they were before the pin
  moved.

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
| E1 | How long do `hipMemset` and a whole-buffer read-back take for 64 to 256 MiB in the ROCm 10.1 guest? | No probe, and the wheel has no timer for it. The VM lanes answer for the sizes they use: `e1-vram-timing.txt`, see [The wait on emulated VRAM](#the-wait-on-emulated-vram-e1). | not yet run |
| E2 | What does the guest offer (`ulimit -l`, `ais-stats`, `nvme-rdma` and `nvme-fabrics`, free disk, CPUs, memory, the cost of `apt-get update`), and how much disk does the runner have before and after a VM job? | `probes=guest-facts`; the runner figures are in every VM job. | Guest (ROCm 10.1.0, Ubuntu 26.04, kernel 7.2.6): 4 CPUs, 8 GB, memlock unlimited, `ais-stats` present, `nvme-rdma` and `nvme-fabrics` present, io_uring works, `apt-get update` 13 s. Absent: etcd, redis-server, the `aws` CLI, libgflags, libhwloc, libfabric. Runner: 87 GB free before boot, 74 GB after (run 44649409003). |
| E3 | Does nixlbench compile with the manylinux toolchain against the ROCm 10.1 HIP headers, do it and every plugin resolve their libraries in the guest and in `ubuntu:24.04`, and how long does the nixlbench build take? | The nixlbench steps of `build` give the time; the lanes' preflight (`nixlbench --version`, `ldd`) gives the libraries. | Partly. nixlbench compiles with the manylinux toolchain against ROCm 10.1 and runs from a wheel in a clean `ubuntu:24.04` container (local build of `for-upstream/feat/rocm-wheels`). The guest and the build time in CI are not yet run. |
| E4 | Does a block of 16 MiB or more fail cleanly on the mesh; what is the limit on `loopback`; does an interrupted RDMA transfer leave the device usable? | No probe yet. `probes=ernic-backend-loopback` shows only that `loopback` carries a ping-pong; `vm1-loop` stays at blocks of 2 MiB until this is answered. | not yet run |
| E5 | Do two nixlbench processes work on device 0 with VRAM in one guest? | The VV, DV and VD cases of `vm1-local` and `vm1-loop`. | not yet run |
| E6 | What are the spread between rows and the duration of a case in each job type? | The records of the lanes: `duration_s` of each case, and the sentinel and A/A repeats. | not yet run |
| E7 | Does `queue: max` work on job-level concurrency, so that every `gh-pages` writer can share one group? | No probe yet. | not yet run |
| E8 | Does `libplugin_LIBFABRIC.so` load in each environment, which libfabric does it resolve, and which interface does it bind across guests? | No probe yet; needs the NIXL and nixlbench wheels. | not yet run |
| E10 | For `nvmeof`: is the TAP needed; does `ip=` on the guest's subnet work, and with which neighbour address; does the multi-namespace grammar parse; do the LBA and KV namespaces appear? | `probes=ernic-backend-nvmeof`. Two NIC servers with different backends in one guest are not covered. | No TAP is needed. `ip=192.168.100.1` works with a static neighbour entry; the target answers no ARP, so any link-layer address does (`02:00:00:00:c0:01` was used). Three namespaces parse and appear; a 1 MiB O_DIRECT write reads back on both LBA namespaces; the KV namespace has a generic node only (run 44649409003). |
| E19 | Does `--security-opt seccomp=unconfined` in a job container's options enable io_uring on hosted runners? | `probes=container-uring`. | Yes. `io_uring_setup` fails with EPERM under the default profile and works with `seccomp=unconfined` (run 44649409003). |
| E20 | Which S3 server suits the container lanes: versitygw, Garage or SeaweedFS, or a frozen MinIO binary? | No probe yet; needs nixlbench. | not yet run |

The `ernic-backend` job also records what the override depends on:

| Check | Question | Answer |
| --- | --- | --- |
| `compose.merge` | Does compose replace, not append to, `command` and `healthcheck.test`? | Yes, both are replaced and there is no entrypoint (run 44649409003). |
| `backend.container` | Is the compose project named after the stack directory? | Yes: `nixl-rocjitsu-backend` (run 44649409003). |
| `image.backends`, `image.backend-string` | Does the pinned NIC server image have `loopback`, `nvmeof` and `s3`, and accept the backend strings? | Yes to both (run 44649409003). |
| `guest.nvme-rdma` | Does the guest kernel have `nvme-rdma`? | Yes (run 44649409003). |
| `nvmeof.namespaces`, `nvmeof.kv-generic-node` | How are the namespaces named in sysfs, and does the KV one appear as `/dev/ngXnY` only? | Names do not follow namespace ids: nsid 3 (KV) is `nvme1n1`, nsid 2 is `nvme1n2`, nsid 1 is `nvme1n3`. The KV namespace has `/dev/ng1n1` and a hidden, zero-size block device (run 44649409003). |
| `s3.put`, `s3.get`, `s3.rdma-client` | Does the S3 store take and return an object over HTTP and over RDMA? | Yes: an 8 KiB object over HTTP, and all 24 checks of `s3_rdma_client -F` over RDMA (run 44649409003). |
| `server.state` | How much memory does the NIC server use with RAM-backed namespaces? | 7 to 10 MiB at rest, before any namespace is written (run 44649409003). |
| none | Does `--p4` work beside `nvmeof` or `s3`? Not covered by the probe. | not yet run |
