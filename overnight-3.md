# Overnight work log, session 3

Unattended session, evening of 2026-09-08. Newest entries at the bottom.
Sessions 1 and 2 are in `OVERNIGHT.md` and `overnight-2.md`; this file assumes
both.

## The ask

1. Work with the storage nodes that are free — the array this project has been
   benchmarking on is held by someone else.
2. Mount any unmounted NVMe on those nodes for testing.
3. Collect statistics with hsa-snoop (latest release) running as a sidecar in a
   docker compose container, rather than inside the benchmark container.
4. Write this report.

Extended part-way through the night:

5. Copy the Prometheus + Grafana compose pattern from `Projects/rocm-aic` so
   results can be dumped from a running stack rather than only scraped to TSV.
6. Put fio in the container, in the version that has hipFile support —
   `sbates130272`'s Docker Hub images have a working recipe.
7. Find out what metrics NIXL itself exposes and capture those too.

## Result, up front

**The new AIS plugin works, and it does not scale.** It is capped at
**~5.5 GB/s** no matter how many files, threads, streams or drives it is given,
while the existing thread-pool `AIS_MT` reaches **20 GB/s writing and 34.7 GB/s
reading** on the same seven drives, the same buffers and the same library. On
16-file reads that is a **6× gap in AIS_MT's favour**.

**The cap is in hipFile's async submission path, not in the plugin and not in
the hardware — and this is now proven from outside NIXL.** fio's `libhipfile`
engine, added to the container later in the session, drives the identical
hipFile calls with none of NIXL above them. Across the same seven drives:

| submission mode | write GB/s | read GB/s |
|---|---|---|
| `hipfile_mode=sync` (= `AIS_MT`) | 28.59 | 18.87 |
| `hipfile_mode=stream` (= `AIS`) | **5.05** | **2.00** |

5.05 GB/s from fio against ~5.5 GB/s from the AIS plugin: two independently
written submitters landing within 10% of each other on a ceiling the
synchronous path clears by nearly 6×. The plugin is extracting essentially all
of what `hipFileWriteAsync` will give.

**The cap is structural, and it is HIP's, not hipFile's.**
`Fastpath::async_io` runs the whole transfer inside a `hipLaunchHostFunc`
callback — that is how it honours the stream-ordering promise the async API
makes — and HIP services host functions from exactly one thread per process
across all streams. A 20-line HIP program with no hipFile in it
(`docker/scripts/hostfunc-serial.hip.cpp`) reproduces it: 8 streams × 4 × 100 ms
of host-function work takes 3205 ms, not 400.

**So the fix was to implement the one submission API that is not subject to it,
and it works.** `patches/hipfile/01-hipfile-batch-worker-pool.patch` implements
hipFile's batch API — a stub upstream, accepting submissions and moving no
bytes — over a worker pool running the synchronous path. Same drives, same
process, same fio invocation (see 06:15):

| submission mode | write GB/s | read GB/s |
|---|---|---|
| `hipfile_mode=sync` | 28.30 | 19.10 |
| `hipfile_mode=stream` | 4.94 | 2.13 |
| **`hipfile_mode=batch`** | **29.19** | **47.24** |

Batch ties the synchronous path on writes and beats it **2.5×** on reads —
47 GB/s is the highest single-process number this array has produced. 29/29
correctness checks pass in-image on the MI300X. This also un-shelves the
original plan's `AIS` NIXL plugin, which is a batch-API port of upstream's
`cuda_gds` and was dropped when the batch API turned out to be a stub.

**The async path behaves as a single server with two constants.** Fitting the
fio block-size curve gives `service time ≈ 24 µs + size / 5.7 GB/s`, and that
one line accounts for everything observed all night: queue depth changes
nothing (throughput flat to 2% from `iodepth` 1 to 64 while latency rises
exactly linearly, 1.65 ms → 107.6 ms); drives change nothing past two
(3.76 → 5.45 → 4.66 → 5.05 GB/s over 1/2/4/7 drives, against `sync`'s near-linear
3.73 → 28.61); and a second *process* gets its own full ~5 GB/s because it gets
its own context. The model also predicts the AIS plugin's number without being
told about it — 5.52 GB/s at the 4 MiB the sweeps use, against ~5.5 measured.

Before fio was available, the same conclusion rested on elimination — three
experiments, each of which could have exonerated hipFile:

- Stream pool depth 16 / 32 / 128 — no change (5.30 / 5.60 / 5.54 GB/s).
- Ops-chained-per-stream 1 / 2 / 4 / 8 / 128 — no change (5.51 / 5.48 / 5.50 /
  5.38 / 5.59 GB/s). At 1, every request gets its own stream, so the plugin's
  fan-out is definitively not the limit.
- **Two independent processes on disjoint drives get ~5 GB/s each**, ~9.7 GB/s
  aggregate. A per-process ceiling that lifts when you add a process is not a
  drive limit and not a PCIe limit — it is serialization inside one hipFile
  context.

So `hipFileReadAsync`/`hipFileWriteAsync` at `bd0bc233` appear to funnel through
a single per-context dispatcher. The batch API was already known to be a stub;
the async API is real but does not go faster than one submitter. **The
recommendation is that `AIS_MT` remains the backend to use**, and that the AIS
plugin's value for now is as the harness that demonstrates this — it is correct
code sitting on a library path that is not yet ready.

**The AIS plugin works on real hardware.** The plugin added in the previous
commit is not the batch-API stub failure mode — a single-file, single-stream
WRITE from VRAM to a Solidigm NVMe sustains 2–4 GB/s, and hsa-snoop
independently confirms the traffic as GPU-to-NVMe direct I/O, attributed to
PCIe device `0000:47:00.0` with the right vendor and the right GPU on the other
end. That is the first end-to-end evidence that the whole chain
(source-built hipFile → async submission path → NIXL plugin → nixlbench)
carries data.

**At one drive and one stream, AIS, AIS_MT and POSIX are indistinguishable.**
3.99 / 3.96 / 3.80 GB/s. That is expected and it is the drive, not the software:
a single PCIe Gen4 x4 NVMe is the bottleneck long before any of the three
submission paths is. The comparison only becomes interesting when the drives
stop being the limit, which is what the `drives` and `threads` sets are for.

## Where session 2 left off

Session 2 fixed the intra-node GPU RMA gap in UCX and established the storage
baseline on `ctr-smc-mi300x-cx68-25`: across 16 NVMe, AIS_MT sustains ~56 GB/s
read and write, POSIX peaks at 48 and collapses to 23 past 8 threads. Commit
`d0ad82c` then showed that the 56 GB/s figure is the GPU's own PCIe link, not
the drive array.

Since then, and not yet measured: NIXL was bumped to v1.4.1 / MORI to v1.2.3,
hipFile moved from the ROCm-packaged 0.3.0 to a source build of
`ROCm/hipFile@develop` at `bd0bc233`, and a second hipFile-backed NIXL
plugin — `AIS`, driving the async stream API — was added alongside the existing
thread-pool `AIS_MT`. Everything in this session is the first hardware exposure
of that stack.

The complication is that `ctr-smc-mi300x-cx68-25`, the node with the 16-drive
array all previous numbers were taken on, is held by another user until roughly
02:00. Hence the ask.

## Plan

| # | Step | Why |
|---|---|---|
| 1 | Find a free storage node and inventory its drives | Session 2's numbers all come from one node's pre-built array. Anything measured elsewhere needs its hardware written down or it is not comparable. |
| 2 | Format and mount the blank NVMe as `/mnt/nixl-nvme-N` | The sweep scripts glob that path; without it `--filepath` is silently dropped and the benchmark measures the container's overlay filesystem. |
| 3 | Build an hsa-snoop v1.1.0 sidecar image | No container image is published, only a `.deb`, so the sidecar has to be built. |
| 4 | Wire it into a compose stack beside the benchmark container | A collector inside the benchmark container cannot survive a sweep: forty-odd short nixlbench processes means forty-odd counter resets. |
| 5 | Prove the AIS plugin moves data before trusting a sweep | "Submits cleanly, moves nothing" is a real hipFile failure mode — the batch path does exactly that — and it is far easier to spot in one tiny transfer than in a 40-row table. |
| 6 | Run the sweep sets with the sidecar tracing underneath | The measurement. |
| 7 | Keep the queued job on cx68-25 | It is the only place a hipFile-bump regression can be checked against session 2's baseline on identical hardware. |

## Log

### 2026-09-08 20:20 — the free node has GPUs after all, and seven blank drives

Corrected an error from earlier in the day: I had recorded the idle storage
nodes as having no GPUs, on the basis of `rocm-smi` reporting "No AMD GPUs
specified". That was my probe, not the node. `srun` without `--gres=gpu:N`
hides the GPUs entirely; re-probing with `--gres=gpu:8` shows MI300X (gfx942,
`0x74a1`) present and healthy. The idle nodes were usable the whole time.

`ctr-smc-mi300x-cx67-5`: 128 cores, 8× MI300X, passwordless sudo, Docker
Compose v5.3.1. `/mnt` contained only `hf_cache` — no array, which is the
difference from cx68-25. Nine NVMe, of which `wipefs -n` (read-only, writes
nothing) reports seven carrying no signature at all:

```
/dev/nvme0n1: gpt gpt PMBR            <- leave alone
/dev/nvme1n1:
/dev/nvme2n1:
/dev/nvme3n1:
/dev/nvme4n1:
/dev/nvme5n1: gpt gpt PMBR  mounted:/boot/efi[SWAP]/   <- OS disk
/dev/nvme6n1:
/dev/nvme7n1:
/dev/nvme8n1:
```

Held the node with job `67912919` (`--gres=gpu:8 --time=10:00:00`) so the rest
of the night runs against a stable allocation.

### 2026-09-08 20:30 — the array is heterogeneous, which constrains what can be claimed

This is the important caveat on every number in this report. cx68-25's array is
16 matched drives. cx67-5's seven blank drives are four different models:

| mount | device | model | size |
|---|---|---|---|
| `/mnt/nixl-nvme-0` | `nvme7n1` | SOLIDIGM SSDPF2KX038T1 | 3.5T |
| `/mnt/nixl-nvme-1` | `nvme8n1` | SOLIDIGM SSDPF2KX038T1 | 3.5T |
| `/mnt/nixl-nvme-2` | `nvme1n1` | SAMSUNG MZQL23T8HCLS-00A07 | 3.5T |
| `/mnt/nixl-nvme-3` | `nvme6n1` | SAMSUNG MZQL23T8HCLS-00A07 | 3.5T |
| `/mnt/nixl-nvme-4` | `nvme2n1` | WUS5EA138ESP7E3 | 3.5T |
| `/mnt/nixl-nvme-5` | `nvme3n1` | Micron 7450 MTFDKCC3T2TFS | 2.9T |
| `/mnt/nixl-nvme-6` | `nvme4n1` | Micron 7450 MTFDKCC3T2TFS | 2.9T |

They are ordered in matched model pairs deliberately, so a 1→2→4 drive-count
sweep grows by adding a like-for-like pair rather than by changing which vendor
is in the array. It is still mixed at four drives and the drives differ in
capacity, so **the drive-count scaling curve from this node is weaker evidence
than session 2's**, and a shortfall against 56 GB/s here is not by itself a
regression. What is unaffected is any comparison taken at a fixed drive count —
AIS vs AIS_MT vs POSIX see exactly the same hardware — and that is the
comparison this session actually cares about.

Formatted ext4 with `-m 0` and `lazy_itable_init=0,lazy_journal_init=0`: doing
the inode-table and journal zeroing up front, in one pass, rather than letting
the kernel do it in the background during the first benchmark and charge it to
the result. Mounted `noatime,nodiratime`.

### 2026-09-08 20:35 — hsa-snoop as a sidecar, and why not the in-image one

The runtime image already carries an hsa-snoop built from `main`, started by
`HSA_SNOOP=1` inside `run-nixlbench`. That is the right shape for one paired
run and the wrong shape for a sweep: `storage-sweep.sh` is forty-odd separate
short-lived nixlbench invocations, so an in-container collector is started and
killed forty-odd times and every counter resets underneath the measurement.
A sidecar with a lifetime longer than any single sweep point is the only way to
get one continuous trace.

Three things had to be built ([docker/hsa-snoop.Dockerfile](docker/hsa-snoop.Dockerfile),
[docker/compose/bench-stack.yml](docker/compose/bench-stack.yml),
[docker/scripts/snoop-sample.sh](docker/scripts/snoop-sample.sh), driven by
[.slurm/snoop-sweep.sh](.slurm/snoop-sweep.sh)):

- **The image.** hsa-snoop publishes no container image, only
  `hsa-snoop_1.1.0_amd64.deb`, so the sidecar is `ubuntu:24.04` + `bpftrace` +
  the `.deb`. Pinned to the release tag rather than `main` for the same reason
  every other component here is pinned. The package `Depends: systemd`, whose
  postinst calls `systemctl` and fails in a container; `--force-depends` plus a
  `test -x` on the installed binary tolerates exactly that failure and nothing
  else. Two traps found while building it, both of the "passes for the wrong
  reason" kind: an `|| true` scoped across the whole install chain would equally
  have hidden a truncated download, and `hsa-snoop --version` does not exist in
  1.1.0 — the version probe was silently failing and being swallowed. The real
  liveness gate is a build-time assertion that `--help` mentions `--ais-snoop`,
  since a collector without it would start, serve `/metrics`, and publish
  nothing at all about the storage path.
- **The stack.** The collector runs `privileged`, `pid: host`,
  `network_mode: host`, with `/sys/kernel/{debug,tracing}`, `/sys/fs/bpf` and
  `/lib/modules` mounted. It works by kprobe against the host kernel rather than
  by talking to the benchmark process, so it needs no namespace sharing with the
  sweep container and the two are only loosely coupled. `HSA_SNOOP` is
  deliberately *not* set on the sweep service: a second collector would attach
  duplicate kprobes to the same functions and double-count every event.
- **The scrape.** No Prometheus server on these nodes, and standing one up for
  one night is more moving parts than the night is worth, so a 2-second `curl`
  loop flattens `/metrics` into a timestamped TSV. To make that joinable,
  `storage-sweep.sh` grew `t_start,t_end` columns — appended, not prepended, so
  anything already parsing the CSV by index keeps working. The sidecar samples
  the node on its own clock and has no idea which sweep point is running, so
  wallclock is the only key the two files share.

Checked before trusting any of it that `kfd_ioctl_ais` — the symbol
`--ais-snoop` puts its kprobe on — is actually present and traceable in this
host's `amdgpu` (6.8.0-31-generic), with BTF available. It is.

### 2026-09-08 20:38 — the loaded image is the right one

```
libplugin_AIS.so  libplugin_AIS_MT.so  libplugin_MORI_IO.so
libplugin_POSIX.so  libplugin_UCX.so
AIS    -> /opt/hipfile/lib/libhipfile.so.0
AIS_MT -> /opt/hipfile/lib/libhipfile.so.0
```

All five plugins present, and both AIS plugins resolve the source-built hipFile
under `/opt/hipfile` rather than the ROCm-packaged one under `/opt/rocm`. That
`ldd` check is what actually proves the pin took effect; the build succeeding
does not.

### 2026-09-08 20:39 — the AIS plugin moves real data

The check that mattered most, and the reason it was worth doing before any
sweep: hipFile's *batch* API is a stub that records operations and issues no
I/O, so "submits cleanly, reports success, transfers nothing" is a failure mode
this stack can genuinely produce. The AIS plugin is built on the *async* path
instead, but that had never been exercised on hardware.

One file, one stream, VRAM → `/mnt/nixl-nvme-0`, WRITE:

| block | B/W (GB/s) | avg lat (us) | avg tx (us) |
|---|---|---|---|
| 1 MiB | 2.09 | 501.8 | 478.6 |
| 2 MiB | 1.63 | 1286.0 | 1264.5 |
| 4 MiB | 3.15 | 1333.5 | 1299.0 |

Data moves. hsa-snoop confirms it independently and from the other side of the
stack — it attributes the traffic to a specific PCIe endpoint, correctly
identified:

```
ais_pcie_device_info{device_type="nvme", pcie_id="0000:47:00.0",
                     vendor="Solidigm", device_id="0b60"} 1
ais_tx_ops_total{comm="nixlbench", pcie_id="0000:47:00.0", gpu_id="28851"} 10508
ais_tx_bytes_total{pcie_id="0000:47:00.0", ...} 46370652160
```

`0000:47:00.0` is `nvme7n1`, which is `/mnt/nixl-nvme-0`, which is the Solidigm
the benchmark was pointed at. So this is not just "nixlbench reported a
number" — the kernel-side kprobe agrees that GPU-to-NVMe direct I/O happened,
to the right device, from the right process.

### 2026-09-08 20:39 — sweeps running

Launched `quick rw threads drives readscale direct wide` under the sidecar.
First results, single drive, single thread, WRITE:

| backend | seg | peak GB/s | at block |
|---|---|---|---|
| AIS | VRAM | 3.99 | 16 MiB |
| AIS_MT | VRAM | 3.96 | 16 MiB |
| POSIX/AIO | DRAM | 3.80 | 64 MiB |

Three paths within 5% of each other, which is the correct and uninteresting
answer at this configuration: one PCIe Gen4 x4 NVMe saturates long before any
submission path does. Note also that the POSIX column is the optimistic one —
it is host I/O from DRAM, and a real VRAM-resident KV-cache offload would owe a
device-to-host copy that these numbers do not include.

The comparison this session exists to make — does the async stream path beat
the serial-taskflow submit path when the drives stop being the limit — needs
the `drives` and `threads` sets.

### 2026-09-08 20:50 — all seven sets done, and AIS does not scale

1,155 result rows, zero failed points across `quick rw threads drives readscale
direct wide`. Peak GB/s per configuration:

**Drive-count scaling (WRITE, VRAM for AIS/AIS_MT, DRAM for POSIX):**

| files | AIS | AIS_MT | POSIX/AIO | POSIX/URING |
|---|---|---|---|---|
| 1 | 3.73 | 3.82 | 3.98 | 3.78 |
| 2 | 5.67 | 7.76 | 7.31 | 7.78 |
| 4 | 4.64 | 15.49 | 15.16 | 14.99 |
| 8 | 5.24 | 15.65 | 15.32 | 15.14 |
| 16 | 5.56 | **20.15** | 19.59 | 19.98 |

**Read scaling, where the gap is widest:**

| files | AIS | AIS_MT | POSIX/AIO |
|---|---|---|---|
| 1 | 6.08 | 6.72 | 6.70 |
| 4 | 5.80 | 25.83 | 25.14 |
| 8 | 5.92 | 27.31 | 26.85 |
| 16 | 5.77 | **34.66** | 32.12 |

AIS flatlines from two drives onward. AIS_MT and POSIX both scale to roughly
what seven mixed NVMe should give. Two secondary observations, both consistent
with the same cause: thread count does nothing for any backend at one file
(3.7–3.9 GB/s from 1 to 16 threads — the drive is the limit there, as expected),
and O_DIRECT on/off is within noise for all three (AIS 4.78/4.74, AIS_MT
15.96/15.30, POSIX 15.26/15.41), so page cache is not what separates them.

### 2026-09-08 21:05 — the cap is inside hipFile, not the plugin

Three experiments, run because the obvious first suspicion was my own code.
The plugin acquires one HIP stream per `batch_limit` requests per device
(`DEFAULT_OPS_PER_STREAM = 128`), which on a 16-descriptor transfer crosses the
boundary exactly once — so the leading hypothesis was that every request was
landing on a single stream and serializing there.

| knob | values | result |
|---|---|---|
| `--gds_batch_pool_size` (streams held) | 16 / 32 / 128 | 5.30 / 5.60 / 5.54 GB/s |
| `--gds_batch_limit` (ops chained per stream) | 1 / 2 / 4 / 8 / 128 | 5.51 / 5.48 / 5.50 / 5.38 / 5.59 GB/s |

At `batch_limit=1` every single request gets its own stream, and the number does
not move. That kills the hypothesis: the plugin's stream fan-out is real and
irrelevant. (Pool sizes 1 and 4 fail to produce results at all — fewer streams
than devices — which is a separate rough edge worth fixing.)

The decisive test was concurrency at the process level rather than the stream
level. Two nixlbench processes, eight files each, on **disjoint** drives:

| configuration | GB/s |
|---|---|
| one process, 8 files | 4.91 |
| two processes, 8 files each | 4.71 + 5.02 = **9.73** |

A ceiling that doubles when you add a second process is not the drives (AIS_MT
gets 20 GB/s from them single-process), not PCIe, and not the GPU. It is
per-hipFile-context serialization: the async path at `bd0bc233` behaves as
though one dispatcher drains all submissions regardless of how many streams
feed it.

That also resolves the awkward fact that this stack pins hipFile to `develop`
specifically to get the async API. The API is present and functional — the
capability probe and the hsa-snoop kprobe trace both confirm data moves — but
functional is not the same as fast. The batch API is a stub; the async API is a
bottleneck. Both of hipFile's concurrency stories are, at this commit, worse
than driving its plain synchronous entry points from a thread pool, which is
exactly what `AIS_MT` does.

### What the sidecar was worth

Two things the benchmark numbers alone could not establish:

1. **Attribution.** `ais_tx_bytes_total` is labelled per PCIe endpoint and per
   GPU, so "the transfer went GPU-direct to the Solidigm at `0000:47:00.0`" is
   an observation from the kernel side, not an inference from a throughput
   figure. On a path whose known failure mode is silently transferring nothing,
   that is the difference between a measurement and a hope.
2. **A negative control that came out right.** Per-window counter deltas show
   **zero** AIS bytes during every POSIX window and non-zero across all seven
   drives during every AIS and AIS_MT window. POSIX is host I/O and should
   generate no GPU-direct traffic; it generates none. A collector that reported
   AIS traffic during a POSIX run would have invalidated everything else it
   said.

It also ruled out the first explanation anyone would reach for: AIS fans out
across all seven drives, same as AIS_MT. The plateau was never a fan-out
problem, and the trace said so before the process-concurrency experiment
confirmed it.

### 2026-09-08 22:30 — how far the ceiling goes, and what the drives can actually do

Follow-up session. Four experiments.

**1. Process scaling.** Each process drives all seven drives with its own files,
WRITE, 4 MiB blocks, 8 threads:

| processes | aggregate GB/s | per-process spread |
|---|---|---|
| 1 | 5.27 | — |
| 2 | 10.23 | 5.21 / 5.02 |
| 3 | 10.87 | 0.70 – 5.24 |
| 4 | 11.72 | 0.19 – 5.27 |
| 6 | 16.25 | 0.38 – 5.10 |
| 8 | 14.97 | 0.15 – 5.37 |

Clean 2× at two processes, then saturation between 11 and 16 GB/s — still below
AIS_MT's 20 GB/s from a *single* process. The second column is the more
interesting one: past two processes the bandwidth stops being shared evenly and
some processes are nearly starved (0.15 GB/s while a sibling gets 5.37).

Two caveats added after the fact, both from measurements below. The starvation
is **not** AIS-specific — AIS_MT is worse under identical contention — and the
totals at four processes and above are **inflated by drive write caches**, since
each process only writes 4 GiB. What survives both caveats is the part this
experiment was for: the step from one process to two, 5.27 → 10.23 GB/s, which
is a small enough working set difference to be unaffected by either and is the
result the ceiling argument rests on.

**2. Same GPU vs different GPUs.** Two processes, 8 files each:

| placement | GB/s |
|---|---|
| both on GPU 0 | 4.83 + 5.03 = 9.86 |
| GPU 0 and GPU 1 | 5.07 + 5.46 = 10.53 |

Within noise of each other. **The ~5 GB/s ceiling is per hipFile context, not
per GPU** — two contexts on one GPU scale exactly as well as two contexts on two
GPUs. That rules out the GPU's own PCIe link, which is what `d0ad82c` found
limiting AIS_MT at 56 GB/s on the other node, and it rules out any per-device
driver queue. It is a software limit in one hipFile context.

**3. What the array can actually do.** No fio on the node, so parallel `dd` with
O_DIRECT, 8 GiB per drive, all seven at once:

| | GB/s |
|---|---|
| dd WRITE (queue depth 1 per drive) | 17.81 |
| dd READ (queue depth 1 per drive) | 21.23 |
| AIS_MT WRITE (16 files) | 20.15 |
| AIS_MT READ (16 files) | 34.66 |
| **AIS, any configuration** | **~5.5** |

This is the number that puts the rest in proportion. `dd` at queue depth 1 is a
floor, not a ceiling, and AIS_MT beats it in both directions — so AIS_MT is
extracting roughly what this array has to give. AIS reaches about 31% of the
write floor and 26% of the read floor. The gap is not AIS_MT being unusually
good; it is AIS being far below what seven NVMe deliver to a single `dd` per
drive.

**4. The deficit is proportional, at every block size.** From the `drives` set,
16 files, WRITE:

| block | AIS | AIS_MT | ratio |
|---|---|---|---|
| 4 KiB | 0.12 | 0.42 | 3.5× |
| 64 KiB | 1.46 | 7.70 | 5.3× |
| 1 MiB | 4.36 | 9.00 | 2.1× |
| 4 MiB | 5.56 | 19.12 | 3.4× |
| 64 MiB | 4.41 | 19.54 | 4.4× |

AIS is slower by a roughly constant factor across four orders of magnitude of
transfer size. That matters for diagnosis: a fixed per-operation overhead would
hurt small blocks and vanish at 64 MiB, and a bandwidth cap would do the
opposite. A constant ratio is the signature of a **concurrency** deficit — AIS
behaves as though it has a fraction of the outstanding operations AIS_MT does,
whatever the size of each one.

### 2026-09-08 22:35 — correction: the pool-size failure is not silent

I recorded earlier that `--gds_batch_pool_size` below the device count "fails
silently". That is wrong, and the correction matters because it changes what
the bug is.

It fails loudly and precisely:

```
E ais_backend.cpp:333] AIS: no stream available for device 0
E nixl_agent.cpp:1234] postXferReq: backend 'AIS' failed to post the
                       transfer request with status NIXL_ERR_BACKEND
```

What was silent was my own harness, which grepped for result rows and reported
their absence as a zero. The real defect is also not about device count, which
was a guess: pool 1, 2 and 4 all fail with **8 nixlbench worker threads**, and
pool 8 succeeds. The condition is `stream_pool_size < num_threads` — each
worker thread needs to hold at least one stream concurrently, and the pool is
sized per device, so threads beyond the pool size find it empty and the
transfer is rejected outright rather than waiting for a stream to be returned.

The right fix is for `getStreamFromPool` to block until a stream is free rather
than fail, or failing that for the engine to reject the configuration at init
with a message naming `num_threads`. That is a plugin bug, unrelated to the
hipFile ceiling, and it is not fixed here.

### 2026-09-08 22:50 — the fairness collapse is not AIS's fault

The one loose end from the process-scaling table. Same conditions, both
backends, per-process GB/s sorted ascending:

| config | per-process | max/min |
|---|---|---|
| AIS_MT ×4 | 1.93, 16.64, 17.72, 18.70 | 9.7× |
| AIS_MT ×8 | 0.14, 0.14, 0.33, 0.37, 0.44, 0.46, 12.77, 12.93 | 95× |
| AIS ×4 | 0.30, 0.84, 4.99, 5.10 | 17× |
| AIS ×8 | 0.15, 0.43, 0.48, 0.65, 0.99, 4.83, 4.98, 5.10 | 33× |

**AIS_MT is the less fair of the two**, by a wide margin at eight processes.
So the starvation seen earlier is contention between processes sharing seven
drives, not anything the AIS plugin does. Open item closed, and the AIS
scaling story is unchanged: AIS is slow but no more unfair than the backend
that beats it.

This measurement also invalidates its own totals, which is worth recording
because it nearly went unnoticed. AIS_MT ×4 sums to 55 GB/s — three times the
17.81 GB/s that `dd` sustains on these drives. Each process writes only 4 GiB
(128 iterations × 4 MiB × 8 files), comfortably inside the drives' write
caches, so short concurrent runs measure cache absorption rather than media
bandwidth. The `dd` ground truth used 8 GiB per drive and does not have this
problem, and neither do the single-process sweep numbers, where AIS_MT's
20.15 GB/s sits just above the `dd` floor exactly as it should. Multi-process
totals from this session should not be quoted.

### 2026-09-09 01:40 — what NIXL actually exposes, and what it does not

The ask was to capture NIXL's own metrics. The good news first: the exporter is
already in this tree's image and nobody had noticed. `libtelemetry_exporter_prometheus.so`
is built and installed, and its prometheus-cpp dependencies (`libcore.so.1.3`,
`libpull.so.1.3`) resolve out of `/opt/nixl/lib/x86_64-linux-gnu`. Three
environment variables turn it on:

```
NIXL_TELEMETRY_ENABLE=y
NIXL_TELEMETRY_EXPORTER=prometheus
NIXL_TELEMETRY_PROMETHEUS_PORT=19090
```

and nixlbench then serves 34 series across 19 metric families for the lifetime
of the process. Other knobs, none of which needed changing: `NIXL_TELEMETRY_ENABLED_METRICS`
(a glob allowlist, all-true when unset), `NIXL_TELEMETRY_BUFFER_SIZE` (4096),
`NIXL_TELEMETRY_RUN_INTERVAL` (100 ms), and `NIXL_TELEMETRY_DIR` /
`NIXL_TELEMETRY_CSV_FILE` for the CSV exporter.

The bad news is what is in those series. For the storage backends, **only the
registration counters populate**. Measured on four files, four threads, 4 MiB,
v1.4.1:

| backend | seg | `agent_tx_bytes_total` | `agent_tx_requests_num_total` | `agent_xfer_time_total` | `agent_memory_registered_total` | bw GB/s |
|---|---|---|---|---|---|---|
| POSIX | DRAM | 0 | 0 | 0 | 15032385536 | 9.76 |
| AIS_MT | VRAM | 0 | 0 | 0 | 8589934592 | 9.51 |
| AIS | VRAM | 0 | 0 | 0 | 6442450944 | 4.25 |

Every one of those runs moved gigabytes. The registration counter proves the
telemetry object exists and is being written to; the transfer counters are
simply never reached.

Three explanations were ruled out rather than assumed:

- **Not buffer pressure.** `agent_telemetry_events_dropped_total` is also 0. If
  the staging queue were overflowing, that is where it would show.
- **Not the allowlist.** `NIXL_TELEMETRY_ENABLED_METRICS` is unset, and unset
  means all-true, not none-true.
- **Not a null telemetry pointer.** The debug line `nixl_agent.cpp:1241 DescList
  of mem type 1` appears during the runs, which is inside `postXferReq`'s
  telemetry block — the code path is entered.

What it actually is remains open. Two candidates, both from reading v1.4.1's
`nixl_agent.cpp` (fetched from GitHub — note the local `Projects/nixl` checkout
is `v1.3.1-151-gdf663171`, a different tree, and reasoning from it wasted twenty
minutes before `git describe` caught it): the `remoteSections_.count(remoteAgent) == 0`
early return in `getXferStatus`, which a storage backend always trips because
there is no remote peer, and `telemetry.totalBytes` never being set on the
`makeXferReq` path. Neither is confirmed, so neither is claimed.

The cheap experiment that would halve the search space, not yet run: use
`NIXL_TELEMETRY_EXPORTER=csv` with `NIXL_TELEMETRY_DIR`. If the CSV also has no
transfer events the fault is on the producer side; if it has them, the
prometheus exporter is dropping them.

The practical consequence is that NIXL's exporter is scraped and dashboarded,
but for storage work hsa-snoop remains the source of truth. Rather than leave
that as folklore, the gap is written into the scrape config comment and into
the Grafana panel description, so a flat zero line reads as a known defect and
not as a broken target.

**Corrected at 03:55 — see below. The counters do populate; the table above is
a sampling artefact of reading the endpoint two or three times per run.**

### 2026-09-09 02:10 — the monitoring stack, borrowed from rocm-aic

`Projects/rocm-aic/monitoring/` has the pattern already worked out: host
networking throughout, Grafana with anonymous Viewer and the login form off,
file-provisioned datasource and dashboards, and everything behind compose
profiles so the expensive parts stay off by default. Its `prometheus.yml`
already carries a `nixl` job pointed at `:19090` with a comment marking the
exporter as beta — so the NIXL-telemetry question above had been asked there
first, and the answer this session found is the concrete version of that
warning.

Adopted with two deliberate departures:

- **A named docker volume for the TSDB, not a bind mount into the checkout.**
  The checkout is on NFS. A Prometheus TSDB on NFS is single-writer at best and
  corrupt at worst, and the thing anyone wants out of a run is a CSV, not a
  TSDB directory.
- **A 2 s scrape interval instead of 15 s.** A sweep point lasts tens of
  seconds. At 15 s a point is one or two samples, which is not a measurement.

One compose behaviour is worth recording because it wasted a build: **compose
interpolates the entire file before it filters by profile**, so a `${VAR:?}` in
a profiled service aborts runs that never asked for that service. Every
variable in `bench-stack.yml` therefore uses `:-` — except `IMAGE_REF`, which
keeps its `:?` as a considered trade. Silently benchmarking whichever image
happened to be loaded is the most expensive mistake available here, and the
price of catching it is that `--profile monitoring up` also wants `IMAGE_REF`
set for a service it is not starting. `IMAGE_REF=$(make -s print-tag) docker compose ...`
satisfies it.

What landed: `--profile monitoring` (Prometheus 3.13.2 + Grafana 11.5.2),
`--profile exporters` (node-exporter with diskstats/nvme/infiniband, and the
AMD device-metrics-exporter), a seven-panel `nixl-storage` dashboard, and
`make monitor-up` / `make monitor-down`.

The dashboard is built around one question, because that is the question these
sweeps keep asking: **is a plateau one drive or all of them?** So AIS
throughput is broken out per PCIe endpoint rather than summed, and NVMe
block-layer throughput from node-exporter sits underneath it as the independent
witness — a GPU-direct path that has quietly fallen back to buffered I/O keeps
showing bytes in the second panel and stops showing them in the first.

And `make snoop-dump`, which is the part that matters for an unattended run:
the TSDB is scratch on a node Slurm will take back, so `docker/scripts/prom-dump.sh`
range-queries fourteen series into one CSV each under `logs/`, in long format
that joins against `storage-sweep.csv` on wallclock. It also saves the raw
`/metrics` text of every target, because metric names drift between releases
and in six months that file is the only record of what the names meant.

### 2026-09-09 03:20 — fio with the hipFile engine, and why it is the important addition

Everything this session has concluded about AIS is a NIXL measurement, and NIXL
is a thick stack: agent, backend engine, descriptor lists, stream pool, then
hipFile. The ~5.5 GB/s cap was attributed to hipFile by elimination — pool
depth, ops-per-stream, and the two-process experiment each failed to move it —
but elimination inside one tool is weaker than a second tool that skips the
tool entirely. That is what fio's `libhipfile` engine is for.

The engine to use is **not** upstream `axboe/fio`. Upstream master has
`libhipfile` (landed `67256d4e`, 2026-05-08, no release tag carries it) but it
is synchronous only. The ROCm fork's `zbyrne/async_hipfile_engine` branch, at
`c3226175` (2026-09-02), adds a `hipfile_mode` option whose three values map
one-for-one onto what this tree already has:

| `hipfile_mode` | hipFile call | NIXL equivalent |
|---|---|---|
| `sync` | `hipFileRead`/`hipFileWrite` | `AIS_MT` |
| `stream` | `hipFileReadAsync`/`hipFileWriteAsync` | `AIS` |
| `batch` | `hipFileBatchIOSubmit`/`GetStatus` | none — the plugin was never written |

So `stream` drives the exact API the AIS plugin drives, with nothing above it.
If `stream` also caps near 5.5 GB/s, hipFile is convicted. If it does not, the
plugin is.

`batch` is expected to fail — hipFile's batch backend accepts submissions,
performs no I/O, and returns "Not Implemented" from `GetStatus`. It is in the
sweep anyway as a regression detector: the day it starts passing is the day the
batch plugin from the original plan becomes worth writing, and nobody is going
to notice that by re-reading a CHANGELOG.

The build had one real problem, and it is the kind that fails silently. **There
are two different `libhipfile.so` on this system with the same SONAME**: ROCm
ships `0.3.0` in `/opt/rocm/lib`, and this tree's `hipfile` stage installs
`0.2.0` into `/opt/hipfile` — the source build is the *newer* library despite
the lower version, and it is the one exporting `hipFileReadAsync`,
`hipFileWriteAsync` and `hipFileBatchIOSubmit`. fio must link that one, or the
comparison is against a different library than the plugins use and is worthless.

fio's `configure` makes that awkward. Its hipFile probe derives both the
include path and the library path from `ROCM_PATH`, then *prepends* its cflags
and *appends* its libs, so whether `--extra-cflags`/`--extra-ldflags` win
depends on where the generated Makefile happens to place each variable. Setting
`ROCM_PATH=/opt/hipfile` instead breaks `-lamdhip64`. Both approaches are coin
flips with a silent failure mode.

The fix is a merged ROCm-shaped prefix at `/opt/fio-hipfile`: symlink ROCm's
`include/hip` and `libamdhip64.so*` into it, then symlink `/opt/hipfile`'s
header and library in **last**, so hipFile wins any name it shares. Point
`ROCM_PATH` at that. And then — because the whole point is not to trust it —
assert it, at build time, as a hard failure:

```
[fio] libhipfile: /opt/fio-hipfile/lib/libhipfile.so.0 -> /opt/hipfile/lib/libhipfile.so.0.2.0
[fio] fio-3.42, async=yes
```

`ldd` must find `libhipfile` at all, and `readlink -f` of it must land under
`/opt/hipfile`. The image build now prints `fio OK: fio-3.42, hipfile_mode=yes`
alongside the existing `nixl plugin OK` lines, and fails if either is untrue.

Pinned as `FIO_REF` in the `Makefile` next to the other components, folded into
the image tag (`...-fioc322617`), and `FIO_REF=` empty means no fio in the
image — the same escape hatch shape `HIPFILE_REF` has.

### 2026-09-09 03:33 — first fio numbers, and the batch stub confirmed

`make fio-sweep FIO_SET=quick`, one job on one drive:

| mode | bw GB/s | mean lat | p99 |
|---|---|---|---|
| `sync` | 3.77 | 278 µs | — |
| `stream` | 3.72 | 4514 µs | 6128 µs |
| `batch` | — | — | fails, as predicted |

Two things confirmed immediately. `batch` fails exactly the way hipFile's
stub was documented to fail, so the regression detector is armed and working.
And at one drive the two working modes are indistinguishable at 3.7 GB/s —
which is the same result NIXL gives at one drive (AIS 3.99, AIS_MT 3.96, POSIX
3.80) and for the same reason: a single Gen4 x4 NVMe is the bottleneck long
before any submission path is. The interesting comparison needs all seven
drives, which is the `modes` set now running.

One bug in the harness, caught by the numbers being absurd rather than by the
code being wrong: the first version read p99 out of `clat_ns` unconditionally
and reported a 0.3 µs p99 against a 278 µs mean for `sync`. For a synchronous
engine the whole call is submission, so `clat` is ~0 and only `lat_ns` means
anything; for `stream` it is the other way round. The parser now picks
whichever latency object actually has time in it.

### 2026-09-09 03:34 — hipFile convicted: the cap reproduces with no NIXL in the picture

`FIO_SET=modes`, eight jobs spread across all seven drives, 1 MiB blocks, same
library the AIS plugins link, **no NIXL anywhere in the stack**:

| mode | op | bw GB/s | mean lat |
|---|---|---|---|
| `sync` | write | **28.59** | 293 µs |
| `stream` | write | **5.05** | 26.6 ms |
| `sync` | read | **18.87** | 444 µs |
| `stream` | read | **2.00** | 67.0 ms |

That settles the question this session has been circling since 21:05. The async
submission path is **5.7× slower than the synchronous one for writes and 9.4×
slower for reads**, on the same drives, in the same process, through the same
`libhipfile.so`, with fio doing the submitting instead of NIXL.

And the number matches. fio's `stream` write gets 5.05 GB/s; the AIS plugin
gets ~5.5 GB/s. Two independently written submitters, one in C inside fio and
one in C++ inside a NIXL backend, land within 10% of each other on a figure
that the synchronous path exceeds by nearly 6×. The AIS plugin is not leaving
performance on the table — it is extracting essentially all of what
`hipFileWriteAsync` will give.

The latency column says what kind of limit it is. 26.6 ms mean write latency at
queue depth 16 is not a device that is busy; a Gen4 x4 NVMe serves a 1 MiB
write in a few hundred microseconds, which is exactly what the `sync` row
shows. Requests are sitting in a queue. Combined with the earlier finding that
a *second process* gets its own ~5 GB/s, the picture is a single per-context
dispatcher that serializes async submissions regardless of how many streams,
files, threads or drives are aimed at it.

This is the evidence to put in the upstream report, and it is much stronger
than what was available at 21:05. The previous case was elimination — pool
depth did not matter, ops-per-stream did not matter, a second process helped —
all of which is consistent with a hipFile bottleneck but also, less plausibly,
with three separate plugin bugs. A second, unrelated tool reproducing the same
ceiling to within 10%, and a first-party ROCm tool at that, removes the plugin
from suspicion entirely.

Two secondary observations worth recording:

- **Reads are worse than writes in async mode**, 2.00 vs 5.05 GB/s, which
  inverts the normal NVMe relationship — the `sync` rows show reads at
  18.87 GB/s and these are drives where reads should beat writes. Whatever
  serializes async submission hurts the read path roughly twice as badly.
- **`sync` at 28.59 GB/s beats the `dd` ground truth of 17.81 GB/s**, so the
  write-cache caveat recorded at 22:50 applies here too: 8 jobs × 4 GiB over a
  20 s window is partly cache absorption. The `sync`-vs-`stream` *ratio* is
  unaffected, since both ran the identical shape back to back, and the ratio is
  the finding. The absolute `sync` figure should not be quoted as sustained.

The p99 column for `sync` rows is still not trustworthy and is left out of the
table above. fio classes almost all of a synchronous call as submission
latency, so `clat` p99 (0.3 µs) and `lat` mean (293 µs) describe different
things, and the parser's fallback pairs them anyway. Fixing it properly means
reporting mean and p99 from the same object or reporting neither; deferred so
the `depth`, `drives` and `bs` sets now running keep one consistent schema.

### 2026-09-09 03:40 — the async path serves one operation at a time

The `depth` and `drives` sets turn "hipFile is slow" into a specific mechanism.

**Queue depth buys nothing but waiting.** `hipfile_mode=stream`, eight jobs,
seven drives, 1 MiB, sweeping `iodepth`:

| iodepth | bw GB/s | mean lat |
|---|---|---|
| 1 | 5.086 | 1.65 ms |
| 2 | 5.072 | 3.31 ms |
| 4 | 5.050 | 6.6 ms |
| 8 | 5.050 | 13.3 ms |
| 16 | 5.064 | 26.5 ms |
| 32 | 5.029 | 53.3 ms |
| 64 | 4.980 | 107.6 ms |

Throughput is flat to within 2% across a 64× change in queue depth, while
latency scales *exactly* linearly with it — 1.65 ms × 64 = 105 ms against a
measured 107.6 ms. That is Little's law with the service rate pinned: every
request added to the queue waits behind all the others and none of them go any
faster. A device or a link that was genuinely saturated would show throughput
rising with depth until it flattened; this never rises at all.

The arithmetic identifies the server. 5.05 GB/s at 1 MiB is 4815 ops/s, or
**208 µs per operation** — and a single 1 MiB write to one of these drives
takes 278 µs (the `sync` single-drive figure from the quick set). The async
path is completing operations at roughly the rate one drive completes them,
one after another, no matter how many are in flight. It behaves as though
there is exactly one operation in service at any instant.

**Adding drives confirms it.** Same shape, sweeping the number of drives:

| drives | `sync` GB/s | `stream` GB/s |
|---|---|---|
| 1 | 3.73 | 3.76 |
| 2 | 7.46 | 5.45 |
| 4 | 15.67 | 4.66 |
| 7 | 28.61 | 5.05 |

`sync` scales almost perfectly linearly — 3.73 → 28.61 over 7 drives is 7.7×,
the extra coming from write-cache absorption. `stream` matches it exactly at
one drive, where the drive is the bottleneck and the submission path is not,
then saturates by two drives and stays there. Between four and seven drives it
does not move at all.

So the ceiling is not per-drive, not per-queue and not per-request: it is one
fixed-rate serializer per hipFile context, sitting between the caller and the
hardware. That is consistent with every earlier observation — pool depth not
mattering, ops-per-stream not mattering, and a second *process* getting its own
full ~5 GB/s because it gets its own context.

For the upstream report this is the pair of tables to lead with. The flat
throughput against linearly rising latency is the diagnostic; the drive-scaling
table shows the same thing from the other direction and rules out the hardware.
Both come from ROCm's own fio, so neither involves a line of this project's
code.

### 2026-09-09 03:44 — the block-size curve gives the serializer two numbers

The `bs` set finishes the characterisation. Same eight jobs on seven drives,
`stream` at `iodepth=16` against `sync` at 1:

| bs | `sync` GB/s | `stream` GB/s | `stream` ops/s | µs per op |
|---|---|---|---|---|
| 4 KiB | 1.59 | 0.165 | 40316 | 24.8 |
| 64 KiB | 16.88 | 1.755 | 26782 | 37.3 |
| 256 KiB | 25.08 | 3.635 | 13868 | 72.1 |
| 1 MiB | 28.48 | 5.008 | 4776 | 209.4 |
| 4 MiB | 28.97 | 5.550 | 1323 | 755.9 |
| 16 MiB | 28.55 | 4.473 | 267 | 3745 |

Treating the last column as the service time of a single server and fitting a
straight line to it gives an almost embarrassingly good fit:

```
service time ≈ 24 µs + size / 5.7 GB/s
```

The marginal rate between consecutive rows is 5.65, 5.73 and 5.76 GB/s for the
64 KiB → 4 MiB steps — three independent estimates agreeing to 2%. Predicted
throughput at 1 MiB is 5.04 GB/s against 5.008 measured; at 4 MiB, 5.52 against
5.550.

So the serializer has exactly two parameters: **a fixed ~24 µs per operation,
and a streaming rate of ~5.7 GB/s**. That is what a single dispatch queue with
a per-request setup cost looks like, and it explains every result of the night
at once. It explains why 4 KiB is catastrophic (24 µs of overhead to move 4 KiB
is 0.17 GB/s and nothing can be done about it). It explains the plateau (large
blocks amortise the 24 µs away and expose the 5.7 GB/s rate). It explains why
queue depth is irrelevant (one server). It explains why a second process
doubles aggregate throughput (a second context is a second server).

And it predicts the AIS plugin. The sweeps run at 4 MiB, where the model says
5.52 GB/s; the plugin measures ~5.5 GB/s. A model fitted entirely to fio data,
with no NIXL involved, reproduces the NIXL backend's ceiling to two significant
figures. There is nothing left to fix in the plugin.

One anomaly not explained: 16 MiB drops back to 4.47 GB/s and off the fitted
line, with a 477 ms mean latency. Something additional degrades at that size —
possibly a chunking limit inside hipFile, possibly the 128 outstanding 16 MiB
requests simply exceeding a buffer. Not chased; it is well outside the sizes
this project uses and it does not affect the conclusion.

For completeness, `sync` peaks at 28.97 GB/s at 4 MiB and holds flat from 1 MiB
up, which is the expected shape for a path that is limited by the drives rather
than by submission.

### 2026-09-09 03:55 — correction: NIXL's transfer counters are not zero, they reset

Bringing the monitoring stack up end to end contradicted the 01:40 finding, and
the monitoring stack is right.

The verification run was meant to be routine: start Prometheus and Grafana,
run `SWEEP_SET=quick` through the compose stack, dump the window with
`make snoop-dump`, confirm files appear. They did — eleven series, three
correctly empty because the quick set is write-only. But `nixl_tx_bps` was
**not** empty, and not zero: it carried values up to 886 MB/s. That should have
been impossible given the 01:40 table.

Querying the raw counter settles it:

```
max_over_time(agent_tx_bytes_total[2h])  =  10170593280
```

Ten gigabytes. The counter is populated. Stepping through the range query shows
why it looked dead:

```
23:47:00  0
23:47:08  7173529600
23:47:12  0
23:47:16  1776558080
23:47:20  0
23:48:44  5917466624
23:48:48  0
```

It is not a cumulative counter. It rises to a large value and **falls back to
zero**, repeatedly, within a single run. A metric named `_total` that returns to
zero is not a counter in the Prometheus sense at all.

So the 01:40 measurement was a sampling artefact, and a predictable one in
hindsight: it read the endpoint two or three times per nixlbench invocation, and
most reads land on a zero. Three backends × a couple of samples each is six
coin flips, and getting six zeros while concluding "the counters never populate"
is exactly the kind of error that a continuous 2 s scrape catches and a manual
`curl` does not. That is an argument for the monitoring stack that had not
occurred to me when building it.

What is *not* established is the mechanism. A follow-up probe polling at 0.5 s
during a longer AIS_MT run read zero for its whole sampling window and then lost
the endpoint entirely part-way through the run, so "resets when read" and
"resets on the 100 ms telemetry interval" are both still live, and the exporter's
own availability during a run is less reliable than assumed. The magnitudes are
at least self-consistent with a per-interval delta: 7.17 GB against a 2 s scrape
is 3.6 GB/s, which is the right order for what these runs actually move.

Two consequences, one corrected here and one already applied:

- **The 01:40 table should not be quoted.** `agent_tx_bytes_total`,
  `agent_tx_requests_num_total` and `agent_xfer_time_total` do carry data for the
  storage backends. `agent_memory_registered_total` behaves normally.
- **`rate()` is the wrong function for these series** and the dashboard and
  `prom-dump.sh` were both written using it. `rate()` over a value that returns
  to zero treats every drop as a counter reset and silently discards it, which
  is how a 10 GB counter renders as a near-flat line. Both now record the raw
  value instead, so the sawtooth is visible as a sawtooth and nobody has to
  re-derive this.

The practical recommendation is unchanged — hsa-snoop remains the source of
truth for storage throughput, because a metric with these semantics cannot be
aggregated safely — but for the opposite reason from the one recorded at 01:40.

### 2026-09-09 06:15 — the batch API, implemented, and the ceiling is gone

The stub was the whole problem. `hipfile_mode=batch` was in the sweep as a
regression detector on the assumption that hipFile would fix it eventually;
it was cheaper to fix it here.

`patches/hipfile/01-hipfile-batch-worker-pool.patch` (1000 lines) implements the
three entry points that threw `"Not Implemented"` and gives
`submit_operations` somewhere to send the work: a lazily-created pool of worker
threads, one per `BatchContextMap`, each running the *synchronous* `hipFileIo`
path. A batch submission promises no ordering between its operations, so unlike
the async path it does not need `hipLaunchHostFunc` — which is the entire
reason it is not capped.

Same fio configuration as 03:33, 8 jobs × iodepth 16 × 1 MiB over the seven
NVMe, one MI300X node:

| mode | write GB/s | read GB/s |
|---|---|---|
| sync | 28.30 | 19.10 |
| stream | 4.94 | 2.13 |
| **batch** | **29.19** | **47.24** |

Batch matches the synchronous path on writes and is **2.5× it on reads**,
against 5.9× and 22× the stream path. The read number is the surprise: sync
reads at queue depth 1 leave more than half the array idle, and batch is the
first submission mode in this tree that has actually filled it.

Worker count matters, and 16 (the first default) was leaving most of that on
the floor. Scan at the same shape, `HIPFILE_BATCH_THREADS`:

| threads | 8 | 16 | 32 | 64 | 128 |
|---|---|---|---|---|---|
| write | 16.48 | 20.18 | 26.56 | 29.32 | 29.13 |
| read | 14.01 | 25.25 | 37.14 | 47.23 | 47.75 |

The knee is at 64 and the patch now defaults there, deliberately *not* clamped
to `hardware_concurrency`: these threads are blocked in `pread`/`pwrite`, not
competing for CPU, so core count is the wrong thing to size against.

Correctness is not taken on faith. `docker/scripts/hipfile-batch-smoke.hip.cpp`
is 29 checks — byte-exact write-then-read-back round trip, cookie accounting,
partial reap, `min_nr=0`, expiring timeouts, cancel, capacity overflow, and 20
rounds of destroy-under-load — and passes 29/29 in the image on the MI300X
against `/mnt/nixl-nvme-0`. `build-hipfile.sh` additionally refuses to produce a
library without `BatchWorkerPool` in it, so the patch silently failing to apply
becomes a build failure rather than a runtime one.

One trap worth recording: the first version of that gate used `nm -C` and it
failed inside the image while passing on the dev box. The ROCm image puts
`llvm-nm` ahead of binutils on `PATH` and the two disagree about demangling
local symbols. The gate now matches the mangled identifier and does not depend
on demangling at all.

This unblocks the original plan's Part B — the `AIS` NIXL plugin as a port of
upstream's `cuda_gds`, which is a batch-API plugin and was shelved when the
batch API turned out to be a stub. It is now worth writing, and the fio numbers
above are the target it should be measured against.

### 2026-09-09 13:20 — the AIS plugin gets a batch mode, and the stream ceiling reproduces inside NIXL

The plugin port is done. `AIS` now has two submission paths behind one runtime
parameter, `ais_mode=batch|stream` (default `batch`, also readable from the
`AIS_MODE` environment variable because nixlbench has no flag for a
backend-specific parameter). The batch path is the structural port of `cuda_gds`
the original plan asked for; the stream path is kept, not replaced, because it
is the only way to reproduce the host-function ceiling from inside NIXL against
a batch path running on the same descriptors.

`storage-sweep.sh` carries the mode in the `api` column, the same column that
already distinguishes `POSIX/AIO` from `POSIX/URING` — it has always meant "which
submit path of this backend", which is exactly what this is.

Seven NVMe on `ctr-smc-mi300x-cx67-5`, peak GB/s over the block-size curve:

    WRITE            files:    1      2      4      8     16
      AIS/batch             4.05   7.66  15.19  14.41  19.96
      AIS/stream            4.04   5.86   4.65   5.18   5.31
      AIS_MT                4.00   7.60  14.72  14.56  19.58
      POSIX/AIO             3.95   7.82  15.06  15.27  20.29

    READ             files:    1      4      8     16
      AIS/batch             6.60  24.50  26.48  35.17
      AIS/stream            6.09   5.80   5.74   5.76
      AIS_MT                6.67  24.84  26.80  35.23
      POSIX/AIO             6.63  24.13  25.41  29.57

Two results, and only one of them is the one that was expected.

**The stream ceiling is confirmed from inside NIXL.** `AIS/stream` flatlines at
~5 GB/s from two files onward and never moves again — 5.31 writing, 5.76 reading
at sixteen files, against fio's independent prediction of 4.9 and 2.1 on these
same drives. The single HIP host-function thread is now demonstrated three ways:
in a bare HIP probe, in fio with no NIXL in the picture, and now in the NIXL
plugin next to a batch path that scales past it on identical descriptors. That
part is closed.

**Batch's fio advantage does not reproduce through NIXL, and that is the new
open question.** In fio, batch beat sync 47.2 to 19.1 reading. Here `AIS/batch`
and `AIS_MT` are indistinguishable — 35.17 against 35.23 reading, 19.96 against
19.58 writing — and both sit short of fio's batch figure of 47.2/29.2 on the
same seven drives. So the batch path did what it was built to do (it removed the
5 GB/s cap and tracks the best existing NIXL path) but it did not reach hipFile's
demonstrated ceiling, and neither did anything else. Three unrelated submit paths
converging on one number, with a NIXL-free control sitting above it, points at
something common to the NIXL path rather than at any one backend. Not chased yet.

Worth noting the write direction was the least discriminating one to have run
first: in fio, sync and batch are nearly tied writing (28.3 vs 29.2) and only
separate reading. The write table above therefore says less than it looks like it
says.

**`hipFileBatchIOSetUp` caps a batch context at 128 operations.**
`BatchContext::MAX_SIZE` is 128, matching cuFile's `CUFILE_MAX_BATCH_IO_SIZE`,
and it is not exported in `hipfile.h`. `--gds_batch_limit 256` therefore fails at
agent creation with a bare `hipFileInvalidValue` (5022), nowhere near the flag
that caused it. `DEFAULT_OPS_PER_HANDLE` is 128, which sits exactly on the cap by
luck rather than design; the plugin now names the limit and the flag when SetUp
rejects the size.

**`--group-add` was passing group *names* to docker**, which resolves them
against the *container's* `/etc/group`. On `ctr-smc-mi300x-cx68-25` the host's
`render` group is 109 and the image's is 994, so the flag granted a group that
owns nothing, `/dev/kfd` stayed unopenable, and every ROCm tool reported "no
ROCm-capable device is detected" on a node with eight MI300X in it. Now numeric.
Separately: `srun --overlap` steps get no GPUs unless the step itself passes
`--gres`, which produces the identical symptom from a different cause.

**There is no free storage node.** The `storage` partition has four GPU nodes:
`cx67-15` and `cx67-25` are drained ("Kill task failed"), `cx68-25` has its NVMe
array unmounted (all sixteen `/mnt/nixl-nvme-*` resolve to the boot drive), and
`cx67-5` is the seven-drive node everything above was measured on. The two idle
nodes, `ctr-smc-strg-cx68-[3,5]`, have `GRES=(null)` — no GPUs, so AIS cannot run
on them at all. A sixteen-drive number to compare against session 2's 56 GB/s is
therefore not available without an admin resuming a drained node, or `mkfs` on
`cx68-25`'s ten blank drives.

## Open items

- **Batch and thread-pool converge ~30% below fio on the same drives** (see
  13:20). `AIS/batch`, `AIS_MT` and `POSIX/AIO` all land within a few percent of
  each other at 35 GB/s read and 20 GB/s write, where fio's batch mode does
  47.2/29.2. Three unrelated submit paths hitting one number with a NIXL-free
  control above it is the shape of a limit in the NIXL path, not in any backend.
  Candidates not yet separated: nixlbench's per-thread submit-and-wait loop, the
  16 MiB `max_request_size` chunking, and the batch pool depth. The cheap first
  experiment is `max_request_size`, since it is one parameter and a 64 MiB block
  is currently split four ways.

- **NIXL's transfer metrics reset to zero mid-run** (see 03:55). Mechanism not
  pinned — "resets when read" and "resets on the 100 ms telemetry interval" are
  both consistent with what was measured, and a 0.5 s poll lost the endpoint
  part-way through a run, so the exporter's own availability is also suspect.
  The cheap next experiment is still `NIXL_TELEMETRY_EXPORTER=csv` with
  `NIXL_TELEMETRY_DIR`: if the CSV shows the same sawtooth the producer is at
  fault, if it shows a monotonic total the prometheus exporter is. Worth an
  upstream report once the mechanism is known, because a `_total` metric that
  is not cumulative is a defect regardless of which side it is on.
- **`hipfile_mode=stream` at 16 MiB drops off the fitted line** (4.47 GB/s
  against a predicted ~5.7, with 477 ms mean latency). Not chased; outside the
  sizes this project uses.
- **File two upstream observations against ROCm/hipFile.** First, the batch API
  at `bd0bc233` accepts submissions and performs no I/O while returning
  `hipFileSuccess` from SetUp and Submit — a silent data-loss shape, and the
  worse of the two defects. `patches/hipfile/` carries a working implementation
  and is offerable as-is. Second, async submission does
  not scale past ~5 GB/s per context at `bd0bc233`. The headline evidence is now
  the fio result — ROCm's own fio fork, `hipfile_mode=sync` 28.59 GB/s against
  `hipfile_mode=stream` 5.05 GB/s on identical drives in one process, with 26 ms
  mean latency at queue depth 16 — because it involves none of this project's
  code. Supporting evidence: the
  same-GPU-vs-different-GPU pair (9.86 vs 10.53 GB/s, so it is not the device),
  the constant ~3.4× deficit across all block sizes (so it is concurrency, not
  per-op overhead or bandwidth), and `dd` at queue depth 1 beating it by 3×
  (17.81 GB/s) on the same drives. Not filed yet.
- **`stream_pool_size < num_threads` rejects transfers outright.** Reproduced:
  pool 1/2/4 fail with 8 worker threads, pool 8 succeeds. `getStreamFromPool`
  should block until a stream is returned rather than error, or the engine
  should reject the configuration at init naming `num_threads`. Plugin bug,
  independent of the hipFile ceiling, not fixed.
- **Multi-process totals are inflated by drive write caches** and should not be
  quoted as sustained figures — see the fairness measurement below, where four
  AIS_MT processes "total" 55 GB/s against a 17.81 GB/s `dd` floor. Each process
  writes only 4 GiB, small enough to be absorbed. The single-process sweep
  numbers are not affected (AIS_MT's 20.15 GB/s sits just above the `dd` floor,
  which is the expected relationship), but any future multi-process work needs a
  working set large enough to defeat the cache.
- The **`HIPFILE_REF=` empty fallback is still untested**, so that escape hatch
  remains theoretical.
- Job `67912908` is still queued on `ctr-smc-mi300x-cx68-25` and was kept
  deliberately: it is the only place the hipFile bump can be checked against
  session 2's 56 GB/s AIS_MT baseline on identical 16-drive hardware. Nothing
  in this session can confirm or deny a regression there, because a different
  and smaller array cannot.
- The seven ext4 filesystems on `ctr-smc-mi300x-cx67-5` are left mounted at
  `/mnt/nixl-nvme-0..6`. They were created on drives that carried no signature;
  nothing was overwritten.
