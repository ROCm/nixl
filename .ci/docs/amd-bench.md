<!--
SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
SPDX-License-Identifier: Apache-2.0
-->

# AMD nixlbench programme

This is for maintainers of the AMD fork of NIXL. It covers what the nightly
nixlbench programme is for, the nixlbench wheel, the `AMD Nightly Bench`
workflow ([`amd-nightly-bench.yml`](../../.github/workflows/amd-nightly-bench.yml))
and its lanes, the `AMD Bench Publish` workflow
([`amd-bench-publish.yml`](../../.github/workflows/amd-bench-publish.yml)) that
puts the records on the site, the `AMD Bench Probe` workflow
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
| 2 Data and site | The publisher and the bench pages: every bench run, failures included, as gzip NDJSON shards under `data/bench/v1/` on `gh-pages` of this repository and as static pages under `bench/`; see [Publishing](#publishing). The explorer under `perf/` comes next and adds Parquet files beside the NDJSON ones in the same shard directories. The data stays in this repository until its size calls for one of its own. |
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

nixlbench ships as a wheel of its own, `nixlbench-rocm`, which installs the
`nixlbench` command and requires `nixl-rocm` at exactly its own version. A
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
| The last steps of `build`, one leg per ROCm version | Builds `nixlbench_rocm<X>_<Y>` after the NIXL backend wheels have been uploaded, from the same build cache, so that it links against that same NIXL build. Checks that the NIXL wheels it was linked against are the uploaded ones, by hash, and uploads nothing otherwise. | `nixlbench-rocm-backend-<rocm><suffix>` |
| The `nixlbench wheel` job, after `merge` | Checks each backend again against the NIXL backend wheels of this run, drops one that does not match, and merges the rest into `nixlbench_rocm-<version>-py3-none-manylinux_2_28_x86_64.whl`. | `nixlbench-rocm-wheel<suffix>`, with `nixlbench-build.json` |

Each of those steps is allowed to fail, and so is the job: a nixlbench failure
is a warning on the run and a `nixlbench` entry in `results.json`, never a
failed `build` or a missing `nixl-rocm` wheel. The build is stopped after
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
   holds both its `nixl-rocm-wheels<suffix>` and its
   `nixlbench-rocm-wheel<suffix>`, at most four branches in one run. The lanes
   and their cases come from the matrix under [`.ci/bench`](../bench).
2. **container** and **vm.** One job per branch and lane, below.
3. **collect.** One job per branch. It merges the lanes' records with
   [`amd_bench_collect.py`](../scripts/amd_bench_collect.py), adds a record
   for every case the matrix expects and no lane reported, and fails when a
   required case did not pass. No case is required yet.

**The bench workflow itself publishes nothing.** It has read-only tokens,
pushes nothing to `gh-pages` and uploads nothing to a release; it leaves the
records as artifacts of the bench run. `AMD Bench Publish` takes them from
there, see [Publishing](#publishing). Neither can change the result of
`AMD Nightly` or of `AMD Nightly Wheels`. No bench job runs on a pull request;
`AMD CI Tests` checks the matrix and the scripts there (see
[amd-ci.md](amd-ci.md)).

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
of qemu-tool v3.0.0 carries. The runner,
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
which under KVM sets the family and the model name and by itself leaves the
vendor that of the runner's own CPU, and a hosted runner is not always AMD.
Up to qemu-tool v2.1.0 no setting changed that. The pinned qemu-tool (v3.0.0)
adds `vendor=AuthenticAMD` to the `-cpu` argument of every guest unless
`<vm-cpu-vendor>` says otherwise, which no overlay here does. Whether a guest
on an Intel runner then runs these cases is not known until a lane draws one;
the record below says which vendor each guest showed.

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

## Publishing

`AMD Bench Publish`
([`amd-bench-publish.yml`](../../.github/workflows/amd-bench-publish.yml))
puts the records of a bench run on the site, failures included, so that a
case that failed, timed out, was skipped or is blocked can be read the next
morning without opening an artifact. The publisher is
[`amd_bench_publish.py`](../scripts/amd_bench_publish.py) and the pages come
from [`amd_bench_pages.py`](../scripts/amd_bench_pages.py). Its jobs:

1. **plan.** Checks each run id and lists the run's `bench-collected<suffix>`
   artifacts, one per branch.
2. **publish.** Downloads the artifacts and checks each one, archives the raw
   files in a release, and commits the data and the pages to `gh-pages`.
3. **verify.** Waits until the site serves what was pushed, then checks that
   the results page and the wheel index still answer. It runs whenever
   something was pushed, also when the `publish` job then ended red.

What a reader gets, with no script and nothing loaded from elsewhere:

- `bench/index.html`: for each branch the latest run, with its verdict and its
  failed cases on the page itself, then every run as a row of lanes.
- `bench/runs/<shard id>.html`: one run. Failed probes and tests, the failures
  grouped by cause and then case by case with the exit codes, the error
  excerpt, the command line and where the logs are; the skipped cases; the
  lanes; what each blocked case waits for; the declared ratios.

The root page links to `bench/index.html` from the first nightly after the
first publication; until then the page is reached by its address.

### When it runs

It follows a bench run by itself only when all of these hold:

- the variable `AMD_NIGHTLY_ENABLED` is `true` and `AMD_BENCH_PUBLISH` is not
  `false`;
- the bench run is one of this repository's default branch that followed a
  nightly. A bench run started by hand may hold a few lanes or a filter, so it
  is published by hand only;
- the bench run did something. A night on which the bench is switched off, or
  on which no branch built a nixlbench wheel, publishes nothing and is not a
  failure.

Any result of the bench run is accepted: a run whose lanes failed is the one
most worth publishing. Setting `AMD_BENCH_PUBLISH` to `false` stops only this
automatic path.

By hand:

```bash
gh workflow run amd-bench-publish.yml -f run-id="<bench run id> [<bench run id> ...]" \
  [-f target-branch=gh-pages-staging] [-f dry-run=true] [-f rerender=true] \
  [-f max-shard-mib=<n>]
```

| Input | Default | Meaning |
| --- | --- | --- |
| `run-id` | none | One to ten ids of completed `AMD Nightly Bench` runs of this repository, separated by spaces. One job handles them all. |
| `target-branch` | `gh-pages` | `gh-pages-staging` rehearses the push on a branch that is not served. |
| `dry-run` | `false` | Builds the data and the pages and keeps them as the artifact `bench-publish-preview` for 7 days. Nothing is pushed or archived. |
| `rerender` | `false` | Also rebuilds the derived files of every run already published; see [Repairs](#repairs). It still needs a `run-id`. |
| `max-shard-mib` | `4` | The size limit of one shard, for this run only; see [Size](#size). |

To publish several runs, a backfill for instance, name them all in one
`run-id`. The `publish` jobs of this workflow run one at a time, with at most
one more waiting; a third one queued would cancel the one waiting, before it
archived anything. A dry run is outside that queue: it writes nowhere, so it
neither waits nor makes another wait.

### Trying it first

```bash
gh workflow run amd-bench-publish.yml -f run-id=<id> -f dry-run=true
gh workflow run amd-bench-publish.yml -f run-id=<id> -f target-branch=gh-pages-staging
gh workflow run amd-bench-publish.yml -f run-id=<id>
```

| Mode | Proves | Leaves out |
| --- | --- | --- |
| Dry run | The download, the check of each artifact, the data and the pages as they would be committed, and the guards on what is staged. The pages can be looked at. | The push, the release, the heal, the served check. |
| `gh-pages-staging` | The same, and that the branch can be created and pushed to. | The release, the heal, the served check. |
| `gh-pages` | Everything. | |

To look at the pages of a dry run:

```bash
gh run download <id of the publish run> --name bench-publish-preview --dir /tmp/preview
tar -xzf /tmp/preview/bench-publish-preview.tar.gz -C /tmp/preview
```

and open `/tmp/preview/bench/index.html`. `report.json` and `plan.json` beside
the tarball say what was published, what was skipped and how many bytes it
added.

`gh-pages-staging` is a history of its own. The workflow creates it from
`gh-pages` the first time and never brings it level again, so a rehearsal of a
run it already holds does nothing. To start over, delete the branch by hand;
the workflow never deletes a branch.

Without any workflow, from a checkout:

```bash
gh run download <bench run id> --name bench-collected --dir /tmp/bench-collected
mkdir -p /tmp/site
python3 .ci/scripts/amd_bench_publish.py publish --site /tmp/site --input /tmp/bench-collected
```

and open `/tmp/site/bench/index.html`.

### The layout on `gh-pages`

This layout is a contract: the pages, and the explorer that follows, read it,
and a published path never changes its meaning. `<shard>` stands for
`data/bench/v1/<night or weekly>/<stamp>/<slug>/<bench run id>.a<attempt>`.

| Path | Kind | Holds |
| --- | --- | --- |
| `data/bench/v1/manifest.json` | Rewritten by every publication | Every shard: its identity, its files with rows, bytes and SHA-256, its lanes with their counts, what it supersedes and what supersedes it. Also `quarantine`, `schema_minor` and `generation`, which grows by one whenever anything under `data/bench/v1/` changes. One line per shard. |
| `data/bench/v1/latest.json` | Rewritten | Per branch the headline run: lanes, failed cases, declared ratios. Enough for a status page with no other file. |
| `data/bench/v1/matrix/<sha256>.json` | Written once | The matrix a run was planned from, byte for byte. |
| `<shard>/{runs,cases,samples,counters,tests}.ndjson.gz` | Written once | The records of one branch of one bench run, one file per record kind, always all five. |
| `<shard>/index.json` | Derived | The summary the run page is made from: lanes, cases needing attention, blocked cases, ratios, links. |
| `bench/index.html` | Rewritten | The landing page. |
| `bench/runs/<shard id>.html` | Derived | The page of one run. |

- **Written once** means never changed and never deleted: the publisher's
  guard refuses a commit that modifies or removes such a file. **Derived**
  files can be rebuilt from the written-once ones, and are, by `rerender`
  only.
- The tiers `nightly` and `pr` go under `night/`, `weekly` under `weekly/`.
- A record file is gzip NDJSON. Each line is a record that passed
  [`schema/v1.json`](../bench/schema/v1.json), with the fields of its kind in
  a fixed order, so the types are those of the schema and nothing is inferred.
  Browsers and DuckDB read the files as they are.
- Parquet comes with the explorer, as more files in the same shard
  directories, listed in the manifest's `files` with their format. Nothing
  published before is rewritten for it.
- A `run_uid` or `case_uid` is unique within a shard only: two branches of one
  bench run share them. Key by shard id and uid.
- Reserved and not written yet: `data/bench/v1/annotations.json`,
  `data/bench/v1/month/`, `*.parquet`, and a `.g<k>` suffix on a shard
  directory.
- The publisher stages nothing outside `data/bench/v1/` and `bench/`. The
  root page, `data/nightly/`, `wheels/` and `perf/` belong to other writers.

### Shards, supersession and the latest run

A **shard** is one branch of one bench run attempt. Its id is
`<stamp>-<slug>-<bench run id>.a<attempt>`, the name of the collected file
without `bench-` and `.ndjson.gz`. `<stamp>` is that of the source run, so the
shards of one night share it. A second branch, a bench started by hand on the
same night and a re-run each make a shard of their own; none replaces another.

Which shard speaks for a lane is worked out per night (one stamp and one
branch) and per lane key, `<lane>/<ROCm version>`, and written to the
manifest as `superseded_by` and `supersedes`:

- A lane that reported beats one recorded as `missing`.
- Then the higher tier wins: `weekly` over `nightly` over `pr`. A `pr`-tier
  run started by hand later the same night never replaces the nightly's lanes.
- Then a run with no `only` filter beats a filtered one.
- Among equals the latest bench run and attempt wins. A lane that is `blocked`
  is the exception: the earliest shard with the same matrix keeps it, so a
  later run of a few lanes does not take the blocked lanes from the full run.
- A later night never supersedes an earlier one.

A reader loads lane K from shard S when S is `authoritative` and K is not in
its `superseded_by`.

The **latest run** of a branch, the headline of the landing page, is:

1. the newest night that has a shard of a bench that followed a nightly
   (`reason` is `nightly`), if there is one;
2. otherwise the night with no filtered lane, then with the most lanes,
   then the newest. A lane counts whatever became of it, so a full run in
   which a lane did not report is still the headline, with that lane shown
   as missing, while a run of a few lanes holds fewer and is not. The page
   says the run was started by hand. This holds only until the branch's
   first automatic bench.

A run started by hand that is newer than the headline is shown as one line,
"newer manual run", under the headline and never replaces it, so a debugging
run of two lanes cannot push the nightly's failures off the first screen. A
branch whose newest run of any kind is more than 14 days older than the
newest run on the site is folded into "Branches with no recent run", where
its line still names a run newer than its headline.

### The raw releases

The collected files are kept, as they were collected, as assets of a monthly
pre-release, `bench-raw-<YYYY>-<MM>` after the shard's stamp:

| Asset | Holds |
| --- | --- |
| `bench-<shard id>.ndjson.gz` | The records, byte for byte. Everything on `gh-pages` can be rebuilt from it. |
| `bench-<shard id>.logs.tar.gz` | The output of every process of every case. A record's `log_ref` is a directory in it. |
| `bench-<shard id>.collect.json` | The collector's summary. |
| `matrix-<sha256>.json` | The matrix, shared by the shards planned from it. |

Assets are added and never replaced; one that is already there with other
contents is reported as a conflict and left alone.

**The release is public and permanent.** In an artifact the logs expire and
need a login; in a release anyone can read them for good. So:

- **A bench lane may use throwaway credentials only**, such as the keys of an
  S3 server started for the job. The checks below do not recognise every form
  of credential.
- The publisher checks the logs tarball itself before archiving it: regular
  `.log` files with relative paths only, within size limits, plain text with
  no line longer than 8 KiB, nothing in the archive beside its members, and
  no member whose name or text looks like a secret or would be changed by
  the runner's redaction. A tarball that fails is not archived. The run is
  published all the same, its page says "no logs archived", and the warning
  names the member and the rule, never the text. So a lane writes text logs
  named `*.log` under `logs/` and nothing else there.
- The records, the matrix and the collect file are archived as they were
  collected, so each is refused when it says something its parsed form does
  not: a JSON object with a key twice, or a gzip header with a file name.
- The `bench-diagnostics-*` artifacts are not archived. They stay with the
  bench run and expire with it; the run page names them.

### Repairs

- **One bad branch does not stop the others.** Each artifact is checked on its
  own: every record against the schema and of this repository, no
  secret-looking string, its matrix present, its size within the limit, its
  run page renderable. One that fails, or that the publisher trips over, is
  reported with the reason and fails the job at its end; the valid ones are
  published first. When none is valid nothing is written anywhere.
- **A failed archive does not stop the pages.** The job fails at its end, and
  the pages link to the release by name, so running the job again, or starting
  the workflow on the same run id, completes the archive with nothing to
  repair. Publishing a shard that is already published changes nothing.
- **Heal.** Before it pushes, the job lists the raw assets of this month's and
  last month's releases and publishes those of the last 14 days that the
  manifest neither lists nor quarantines, at most 20. That picks up a run that
  was archived and then lost its push. It is best effort: a failure in it is a
  warning, an archived run the publisher cannot take is left out, and when
  the publisher fails with the archived runs the job publishes without them.
- **`rerender`.** Checks the record files of every published shard against
  the manifest, rebuilds each `index.json` and every page from them, and
  works the lanes and the supersession out again. Use it after a change to the
  pages or to the summary. A normal publication writes `index.json` and the
  run page of new shards only, and warns when an older `index.json` cannot be
  read. The workflow cannot be started without a `run-id`: name any bench
  run whose `bench-collected` artifact has not expired. Naming a run that is
  already published changes no data.
- **Quarantine.** To keep a shard from being published, by the heal as well,
  add `{"id": "<shard id>", "reason": "...", "at": "<UTC time>"}` to
  `quarantine` in `manifest.json` on `gh-pages`, by hand. Nothing in the
  workflow adds or removes an entry. A quarantined shard is not archived
  either, so to take a leaked file out of a release, add the entry first and
  delete the asset second: without the entry, starting the workflow on that
  run id again uploads the asset again. When the manifest cannot be read the
  job archives nothing, publishes the pages and fails at its end.
- **A refused push.** As for the other writers: the job starts again from a
  fresh shallow clone after a random pause, five times at most, then fails
  with `gh-pages` as it was. The raw files may already be in the release, and
  the heal publishes them with the next run.
- **A red `verify` job.** The commit is on `gh-pages`; Pages took more than
  15 minutes to serve it, or a page of another writer did not answer. Nothing
  is rolled back. Run the job again. When a later publication is served
  first, the check accepts its landing page and `latest.json` in place of
  the ones it pushed.

### Size

The data stays in this repository, on a branch that is never rewritten, so
every byte published is kept for good. Each publication therefore says what it
cost, in the commit message and on the run's summary page:

- the bytes added to the tree, and the tree's size under `data/bench/v1/` and
  `bench/`;
- the bytes the commit stores, before git packs them;
- the size of the repository as GitHub reports it.

The tree figures are in `report.json` and the stored bytes in `staged.env`,
which every run that is not a dry run keeps with `plan.json` as the artifact
`bench-publish-report`. The size of the repository is on the summary page
only.

One shard, its five record files and `index.json`, may take 4 MiB at most. An
artifact above that is refused like any other invalid one; above 1 MiB the
publisher warns. `max-shard-mib` raises the limit for one run started by hand;
the automatic path always uses 4.

The first run's record files take 65 KB, and do not compress further. With
`index.json`, the run page and the three files every publication rewrites
(the manifest, `latest.json` and the landing page), a night costs about 90 KB
of history per branch once git has compressed it, some 33 MB a year, beside a
repository of about 30 MiB today. A new matrix version adds about 8 KB once.
That growth is what to watch, and what decides when the data moves to a
repository of its own.

`manifest.json` has no bound yet: it grows by about 2.5 KB a shard, is
rewritten whole by every publication and is read whole by every reader. The
stored-bytes figure of a publication therefore rises slowly by itself, about
0.5 KB a night, so the size of the repository is the figure to compare from
month to month. How the manifest is split, by month for instance, is to be
settled before the explorer fixes on its shape.

## Blocker codes

A lane or a case whose matrix entry has `status = "blocked"` is not run. The
collector writes a `blocked` record for each of its cases with the codes it
waits for, and the run page lists them under "Blocked". A `W` code is a piece
of work of the programme, an `E` code an [experiment](#experiments), and a
name stands for a defect.

| Code | Waits for | Blocks |
| --- | --- | --- |
| `W4` | The lane's job in the bench workflow: a boot that selects the rocm-ernic `s3` backend with the compose override. | The lane `vm1-s3`. |
| `W10` | An SPDK vfio-user NVMe controller in the guest's stack. | The lane `vm1-pcie`. |
| `W11` | The integration build: a bench-only job that merges the listed `for-upstream/` branches in its workspace and builds nixlbench from the result, so that a change not yet on the wheels branch reaches a lane. | Every case that needs such a change: with `W13`, `W14` and `W15` below. |
| `W13` | The vendor-neutral `NVME_KV` plugin and its nixlbench backend. | The Key Value cases of `vm1-pcie` and `vm1-fabric`. |
| `W14` | The hipObject engine of the OBJ plugin. | The S3-over-RDMA cases of `vm1-s3`. |
| `W15` | LIBFABRIC selecting its verbs provider, and a libfabric built with verbs in the guests. | The lane `vm2-lfverbs`. |
| `E8` | The answer to E8: whether the LIBFABRIC plugin loads in each environment, which libfabric it resolves and which interface it binds. | The LIBFABRIC tcp cases of `bench-container` and `vm2-net`. |
| `ucx-shm-ep` | A UCX transport list that works for shared memory. With `UCX_TLS=shm`, and with `shm,self` and `posix,sysv,self`, nixlbench fails at endpoint creation with "Destination is unreachable". | The UCX shm cases of `bench-container`. |
| `nixlbench-redis-read-sweep` | A nixlbench READ sweep that Redis can serve. nixlbench seeds each key once with the largest block, and the plugin fails a GET whose reply is not the size asked for, so every row below the largest block fails. | The REDIS READ cases of the `N` profile in `bench-container`. |

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

Up to qemu-tool v2.1.0 the NIC server was always started on its `tcp` mesh,
with no setting for anything else; the server takes its backend from its
command line only. The pinned qemu-tool (v3.0.0) can name a backend in a
`<devices>` block of the fleet XML, which the jobs do not use yet. The
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

- **The backend setting of qemu-tool, which v3.0.0 has** (`<backend>` of an
  ernic `<server>` in a `<devices>` block): the override, the limits above,
  and the steps of `amd-bench-vm.yml` and of the probe that write it, check
  how compose merged it and compare what it repeats. Not done with the move
  of the pin.
- **The guest's CPU vendor, which qemu-tool v3.0.0 sets:** nothing is
  removed. The section on the guest's CPU is rewritten once a run shows what
  such a guest does.
- **A qemu-tool-action release** with a GPU bring-up that reports without
  failing the boot and host aliases of the caller's choice, which v2.2.0 has
  (`gpu: optional`, `ssh-aliases`), and the digests of the images it ran,
  which it has not: the lanes' own GPU step, the SSH configuration rewritten
  from the action's manifest, and the comparison of the overlay's tags with
  qemu-tool's fleet file. Not done with the move of the pin.
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
