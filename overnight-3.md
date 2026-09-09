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

## Result, up front

**The new AIS plugin works, and it does not scale.** It is capped at
**~5.5 GB/s** no matter how many files, threads, streams or drives it is given,
while the existing thread-pool `AIS_MT` reaches **20 GB/s writing and 34.7 GB/s
reading** on the same seven drives, the same buffers and the same library. On
16-file reads that is a **6× gap in AIS_MT's favour**.

**The cap is in hipFile's async submission path, not in the plugin and not in
the hardware.** Three experiments, each of which could have exonerated it:

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

## Open items

- **File an upstream observation against ROCm/hipFile**: async submission does
  not scale past ~5 GB/s per context at `bd0bc233`. The evidence to quote is the
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
