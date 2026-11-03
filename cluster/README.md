# A test cluster for NIXL and MORI

Spins up N VMs, each with an emulated RDMA NIC (rocm-ernic), an emulated GPU
(rocjitsu) and a local NVMe, joined in a mesh. It exists so the NIXL backends
can be exercised without real RDMA hardware.

It is driven by [`qemu-tool`](https://github.com/ROCm/qemu-minimal), which
generates the compose stack from env files; `cluster/env/nixl-mori` is the
overlay that describes this cluster, layered on qemu-minimal's own `scale-out`
profile. Nothing here duplicates that tooling.

## Requirements

* a checkout of `qemu-minimal`, pointed at by `QEMU_MINIMAL` (default
  `~/Projects/qemu-minimal`)
* `qemu-tool` **from that source tree**, not the packaged one -- the released
  version has no `gen-compose`:
  ```
  python3 -m venv /tmp/qt && /tmp/qt/bin/pip install $QEMU_MINIMAL/qemu
  ```
* `oras`, `zstd`, `qemu-img`, `docker` and `ansible-playbook` on the host
* the guest disks, which `make cluster-images` fetches -- see below

## From nothing to a running cluster

Every pin needed is in `env/nixl-mori`, so this is the whole sequence:

```
make ernic-image       # build the rocm-ernic whose RDMA CM works
make cluster-images    # fetch VM_BACKING_IMAGE -> backing qcow2, overlays, fleet key
make cluster-up        # render the compose stack and boot the guests
make cluster-ansible   # memlock, fabric addresses, ionic provider, GPU
docker buildx bake slim
make cluster-stack     # install /opt and its environment on every guest
```

`ernic-image` is first because `ERNIC_IMAGE` points at it. The published
ernic at this build stamp predates ROCm/rocm-ernic#315, so guest-to-guest
`rdma_cm` does not work on it and **LIBFABRIC hangs rather than transfers**.
Every published build that does carry the fix is at a libvfio-user revision
the forked QEMU is not, and the vfio-user wire protocol is not compatible
across revisions -- so no published pair gives both a working CM and a working
VRAM path. `docker/rocm-ernic/Dockerfile` resolves that by building a newer
server against the published image's own libvfio-user.

One consequence to know about: that server binds its mesh-manager port on
tcp6 only -- a dual-stack socket, which Linux reports in `/proc/net/tcp6` and
not `/proc/net/tcp`. `gen-compose`'s manager healthcheck greps the latter, so
it never passes and `compose up` refuses to start anything gated on it. The
mesh itself is fine; v4 clients connect through the dual-stack socket. It
cannot be fixed from the image, so `cluster-up` ignores the compose failure
and `cluster-boot` starts the containers in dependency order instead. The
real fix belongs in `gen-compose`, which should check both files.

`cluster-images` is the step that used to be missing. The image tags
described the cluster exactly and still could not be turned back into one,
because fetching the qcow2, extracting the fleet key and creating the per-VM
overlays were all done by hand. It takes two tags of the same build: the
`-qcow2` one is an OCI artifact holding the disk and needs `oras`, and the
conventional one carries `/output/id_rsa` -- the key the guests trust, which is
baked into the image rather than injected, since a published backing image gets
no cloud-init pass.

It is idempotent, keyed on `.artifact-tag` in the artifacts directory, so it
re-fetches exactly when `VM_BACKING_IMAGE` moves. `FORCE=1 make cluster-images`
refetches regardless and recreates the overlays, which discards all guest
state.

Note that `qemu-tool run-vm --backing-shared` does **not** create the overlays;
that flag only turns off file locking so the guests can share one backing file.
A missing overlay is a guest that will not boot.

## Use

```
make cluster-up        # render the stack and bring it up
make cluster-ansible   # everything the guests need that the image lacks
make cluster-prep      # just the memlock / queue / fabric-address tags
make cluster-gpu       # just ip_discovery.bin + amdgpu-probe, once per boot
make cluster-nvme      # ext4 + mount the local NVMe on every guest, once
make cluster-status    # containers, guest reachability, RDMA device per guest
make cluster-down      # tear it down, volumes included
```

The guest configuration lives in `ansible/guest-setup.yml`, and
[ansible/README.md](../ansible/README.md) is the single list of everything this
repo changes outside its own image -- guests, container images and compose
settings alike, with what each failure looks like when the change is missing.
`cluster-prep` and `cluster-gpu` are shorthands for two of that playbook's tag
sets.

Guests are at `VM_SSH_PORT_BASE + n - 1` on localhost: VM 1 on 2400, VM 2 on
2401. Their RDMA-side addresses are `ERNIC_GUEST_SUBNET.1n`, so
`192.168.220.11` and `192.168.220.12`.

**`cluster-prep` is not optional, and it is needed again after anything that
reboots the guests** -- which `cluster-up` does whenever the render changes a
`qemu-N` service. It sets three things, each of which fails in a way that
points somewhere other than itself:

* `memlock` unlimited. nixlbench registers its whole buffer, so the 8 MB
  default is nowhere near enough. UCX says so plainly and NIXL then buries it
  under "registration failed for the specified or all potential backends",
  which reads like a missing plugin.
* one combined queue on the ernic NIC. ionic sizes its interrupt pool from the
  vCPU count and does not reliably fit four queues in it; the link then stays
  down with `ip link set up` answering "No space left on device". It is not
  deterministic -- one bring-up here left vm1 at four queues and stranded the
  other three -- so the rule pins it rather than leaving it to chance.
* the fabric address, as netplan config. Nothing in the compose stack assigns
  it, so before this it was set by hand and lasted until the next reboot. The
  `optional: true` in that config matters on its own: without it
  `systemd-networkd-wait-online` blocks the boot while the link is down, which
  is indefinitely if the queue count is wrong, and the guest never reaches
  sshd.

## Getting NIXL onto the guests

The guests have no Docker, and the full image is far too large to host in one.
Use the slim runtime, which exports as a tarball:

```
docker buildx bake slim
make cluster-stack     # export /opt from the slim image, install on each guest
```

That puts the artifacts under `/opt/nixl-stack/opt` and adds an
`ld.so.conf.d` entry. Two system libraries are not in the tarball because they
come from apt rather than `/opt` -- `libgflags2.2` and `libhwloc15` -- and
`cluster-stack` installs them.

## What works here, and what does not

All three transports, measured 2026-11-02 on the four-guest cluster with two
rails and two GPUs per guest, the `ernic-rocjitsu` guest image and the
rocm-ernic from `docker/rocm-ernic/`. DRAM segments, vm1 <-> vm2, 16 MiB
buffer, single runs, GB/s:

| Block | LIBFABRIC | UCX | MORI_IO |
|---|---|---|---|
| 64 KiB | 0.196 | 0.154 | 0.075 |
| 128 KiB | 0.338 | 0.291 | 0.146 |
| 256 KiB | 0.639 | 0.674 | 0.289 |
| 512 KiB | 1.119 | 1.270 | 0.489 |
| 1 MiB | 1.778 | 1.624 | 0.805 |

**LIBFABRIC and UCX are the same speed; MORI_IO is half.** Three runs at
1 MiB, because a single run cannot separate the first two:

| Backend | runs | mean | spread |
|---|---|---|---|
| UCX | 1.624, 1.888, 1.843 | 1.785 | ±7.4% |
| LIBFABRIC | 1.778, 1.676, 1.835 | 1.763 | ±4.5% |
| MORI_IO | 0.805, 0.818, 0.814 | 0.812 | ±0.8% |

The two leaders' ranges overlap, so **do not read an ordering between them
from these numbers**; each is well inside the other's run-to-run variance on
a host whose 32 cores are 2x oversubscribed before the emulators are counted.
MORI_IO is 2.2x behind both, which is larger than any variance here, and it
is also by far the steadiest of the three.

This supersedes the ranking in `overnight-2026-10-22.md`, which had LIBFABRIC
ahead of UCX by 11.8x. That was measured with UCX crippled by the 8 MB
`memlock` default; with that fixed the two are level.

Large blocks, same cluster, 256 MiB buffer:

| Block | UCX | LIBFABRIC | MORI_IO |
|---|---|---|---|
| 2 MiB | 2.620 | 1.857 | 1.382 |
| 4 MiB | 3.620 | 2.179 | 2.028 |
| 8 MiB | **4.466** | 2.416 | 1.420 |
| 16 MiB | 2.549 | 2.344 | **fails** |

The ordering changes above 1 MiB: **UCX pulls clearly ahead of LIBFABRIC**,
by 1.8x at 8 MiB, where at 1 MiB and below the two are level. UCX peaks at
8 MiB and falls off at 16; LIBFABRIC is flatter.

**The 16 MiB ceiling is gone for UCX and LIBFABRIC, and remains for
MORI_IO.** It used to take all three down with `NIXL_ERR_REMOTE_DISCONNECT`.
The rocm-ernic in `docker/rocm-ernic/` is what changed: ROCm/rocm-ernic#315
also makes a QP entering the Error state flush its queues, where before
outstanding work requests were never completed. Checked against the obvious
alternative explanation -- that the old failures were just a buffer equal to
the block size -- by running 16 MiB blocks at 16, 32 and 256 MiB buffers:
2.547, 2.537 and 2.670, so buffer size is not the variable. MORI_IO still
fails at 16 MiB, with `NIXL_ERR_BACKEND`.

## Batching is still the largest lever

1 MiB blocks, varying only the batch size. Nothing else tried moves
throughput this much:

| Batch | UCX | LIBFABRIC | MORI_IO |
|---|---|---|---|
| 1 | 2.157 | 2.091 | 0.866 |
| 4 | **5.612** | 4.625 | **2.815** |
| 8 | 5.184 | **5.467** | 1.404 |

2.6x for UCX and LIBFABRIC. The two differ in where they want to sit: UCX
peaks at batch 4 and gives a little back at 8, LIBFABRIC is still climbing at
8. **MORI_IO is the one to be careful with** -- 3.2x at batch 4 and then half
of that at 8, a collapse rather than a plateau, so a batch size tuned for the
other two is actively bad for it.

## MORI_IO worker threads

`--progress_threads`, which patch 04 maps onto the plugin's
`mori_worker_threads`. 1 MiB, batch 4:

| Threads | GB/s |
|---|---|
| 1 | 2.232 |
| 2 | 2.725 |
| 4 | 2.744 |
| 8 | 2.725 |

The knee is at 2 and everything past it is flat, so MORI_IO is not
thread-bound here; one worker is the only setting that costs anything.

## The mesh scales across pairs

Two pairs at once, vm1 <-> vm2 alongside vm3 <-> vm4, 1 MiB at batch 4:

| Backend | pair A | pair B | aggregate |
|---|---|---|---|
| LIBFABRIC | 4.162 | 3.949 | **8.112** |
| UCX | 3.846 | 3.767 | 7.613 |
| MORI_IO | 1.897 | 1.626 | 3.523 |

8.1 GB/s is the highest figure measured on this cluster. Each pair runs at
roughly 75% of what it does alone, so the fabric is sharing rather than
serialising, and LIBFABRIC edges UCX here where the single-pair batched
numbers had them the other way round -- another reminder not to rank those
two on one run.

## Two rails aggregate

Both rails between the same pair of guests, driven as two independent jobs,
1 MiB blocks:

| | GB/s |
|---|---|
| rail 1 alone | 2.114 |
| rail 1, both running | 1.936 |
| rail 2, both running | 1.946 |
| **aggregate** | **3.881** |

1.84x for the second rail, each giving up about 8%. So the emulated fabric is
not serialising across rails, which is the property worth having: a second
rail buys close to a second rail's worth of bandwidth.

A method note, because it cost a wrong answer here and in
`overnight-2026-10-22.md` before that. Two concurrent runs writing to one log
path produce two *identical* numbers -- the second reader gets the first
run's rows -- and the result looks plausible rather than broken. The first
attempt at the table above reported both rails at 1.928965 GB/s, to six
decimal places. Give each run its own log, and treat identical concurrent
figures as a harness fault until proven otherwise.

**These are roughly 10x what the same backends measured on the previous
guest image**, where UCX reached 0.284 GB/s at 1 MiB and MORI_IO 0.278. Two
things account for it and they are not separable from these numbers alone: the
old guests ran with `memlock` at the 8 MB default, which throttled every
registration, and they drove the ernic through the in-tree `ionic` rather than
the `ionic-ernic` DKMS module the ernic-rocjitsu image ships. Anything in
`overnight-2026-10-2*.md` comparing backend against backend was measured under
the first of those and should be re-read with that in mind -- in particular
MORI_IO was ahead of UCX there by 8x, and here UCX leads at every size.

Both fail at 16 MiB, each in its own way: UCX with
`NIXL_ERR_REMOTE_DISCONNECT`, MORI_IO with a CQ flush and
`remote access error`. The MORI_IO ceiling is the one
`overnight-2026-10-22.md` already recorded, so it has now survived a guest
image change.

* **POSIX against the local NVMe** works.
* **MORI_IO between guests** works, but only with `--mori_host` set per
  process to that guest's fabric address:

  ```
  # on 192.168.210.11
  nixlbench --backend MORI_IO --mori_host 192.168.210.11 ...
  # on 192.168.210.12
  nixlbench --backend MORI_IO --mori_host 192.168.210.12 ...
  ```

  MORI copies that address into the engine descriptor the peer connects back
  to, so it has to be one the peer can reach. Left unset, the backend
  auto-detects by asking the kernel which source address it would use to leave
  the box -- which here is the user-mode NAT NIC, `10.0.2.15`, *the same
  address on every guest*. The run then gets all the way through
  registration and rendezvous before failing with
  `connect(10.0.2.15:<port>): Connection refused`. Any multi-homed host has
  this problem; these guests just make it certain.
* **LIBFABRIC works on DRAM** and is one of the two fastest backends here;
  see the tables above. It needed three things, none of them default:
  `--enable_pt`, the rdma-core ionic `sq_sig_all` fix (see
  `patches/rdma-core/`), and a rocm-ernic whose guest-to-guest `rdma_cm`
  works, which is what `docker/rocm-ernic/` builds. `rping` is the quickest
  check that the third one is in place: it is nothing but RDMA CM, and it
  completes only on an ernic carrying ROCm/rocm-ernic#315.
* **LIBFABRIC on VRAM works**, and is the fastest VRAM path here. 1 MiB
  blocks: LIBFABRIC 2.247 GB/s, UCX 2.333, MORI_IO 0.816.

  It took `patches/libfabric/01`, and the cause was the same bug as
  `patches/ucx/03` in a second library. `rocr_is_dmabuf_supported()` decides
  whether ROCr dmabuf is usable by grepping `/boot/config-$(uname -r)` for
  `CONFIG_DMABUF_MOVE_NOTIFY=y` and `CONFIG_PCI_P2PDMA=y`. The 7.2.6 kernel
  has the second, never mentions the first, and exports
  `dma_buf_move_notify` -- so the capability is there and the probe cannot
  see it.

  What made it expensive is that the failure is silent. The export is
  skipped, and `vrb_reg_hmem_dmabuf()` in the verbs provider does:

  ```c
  err = ofi_hmem_get_dmabuf_fd(iface, buf, len, &fd, &offset);
  if (err) goto failover;          /* saved_errno stays 0 */
  ...
  failover:
      mr = ibv_reg_mr(pd, (void *)buf, len, vrb_access);
  ```

  `ibv_reg_mr` on a device pointer returns EFAULT, and the "Failover failed"
  message is only printed when `saved_errno` is non-zero -- which it is not
  on this path. So the whole thing surfaces as

  ```
  fi_mr_reg failed on rail 0: Bad address
  ```

  with nothing logged at any `FI_LOG_LEVEL`, even in an `--enable-debug`
  build, and it points at the NIC rather than at a probe that read the wrong
  file.

  **Two things were ruled out before finding it**, both recorded so nobody
  repeats them. The device name is not the cause: `vrb_hmem_supported()`
  takes the peer-memory path when the name contains `mlx`, `bnxt_re` or
  `roce`, and ours is `rocep1s0` -- but renaming both devices to
  `rocm_ernic0`/`rocm_ernic1` left `fi_mr_reg` failing identically. And NIXL
  was only half the story: `patches/nixl/07` is needed to get as far as
  attempting the registration at all, but on its own it still fails here.

* **AIS and AIS_MT work**, against VRAM, through hipFile. They did not on the
  previous guest image, where I/O failed with `hipFileWrite failed: Success`
  and `hipFileBatchIOSubmit failed: err=5030`. Registration still needs
  `HIPFILE_ALLOW_COMPAT_MODE=true`. Two-rail cluster, consistency-checked,
  `--filepath` on a local NVMe, GB/s:

  | Block | AIS_MT | AIS | POSIX (DRAM) |
  |---|---|---|---|
  | 256 KiB | 0.140 | 0.137 | 0.732 |
  | 512 KiB | 0.199 | 0.196 | 1.031 |
  | 1 MiB | 0.400 | 0.406 | 1.361 |

  AIS and AIS_MT are indistinguishable from each other at every size, so
  hipFile's threading is buying nothing here. POSIX is on DRAM and is NOT a
  like-for-like comparison: it cannot register VRAM at all, failing
  `registerMem`, which is expected for a host-memory path.

  Measure storage with `--check_consistency true`. Without it AIS_MT reads
  0.226 GB/s at 1 MiB against 0.138 checked -- the inflation
  `overnight-2026-10-22.md` warned about is real.

The GPU is driven by `make cluster-gpu`; `VM_VFIO_USER_ROOT_PORT=true` in
`env/nixl-mori` is load-bearing for it.

## VRAM over the fabric works

It did not before, and the old note here -- "`ibv_reg_mr` fails `-EFAULT` at
every size, so the limitation is in the device" -- was wrong twice over. The
device was never the constraint, and the EFAULT was a symptom of asking the
wrong question of it. Measured vm1 <-> vm2, 16 MiB buffer, GB/s:

| Block | UCX VRAM | UCX DRAM | MORI_IO VRAM | MORI_IO DRAM |
|---|---|---|---|---|
| 64 KiB | 0.176 | 0.170 | 0.072 | 0.070 |
| 128 KiB | 0.345 | 0.325 | 0.139 | 0.138 |
| 256 KiB | **0.643** | 0.589 | 0.274 | 0.269 |

All three transports move VRAM now, at 1 MiB: LIBFABRIC 2.247, UCX 2.333,
MORI_IO 0.816 GB/s. LIBFABRIC needed `patches/libfabric/01` as well as
`patches/nixl/07`; see the transport list below for why.

All four columns are one 16 MiB buffer so they compare directly; the DRAM
figures elsewhere in this file use a 1 GiB buffer and are not interchangeable
with these.

**VRAM costs nothing against DRAM here** -- the two are within run-to-run
variance of each other for both backends, and VRAM is nominally ahead in this
pair of runs. Once the registration path is right the GPU memory is not the
expensive part; the fabric is. That also means the ordering between backends
is the same on VRAM as on DRAM, with UCX roughly 2.3x MORI_IO.

Three things had to be true at once, and each failed silently:

1. **UCX has to ask for a dma-buf fd.** It decides whether dmabuf is
   supported by grepping `/boot/config-$(uname -r)` for
   `CONFIG_DMABUF_MOVE_NOTIFY=y` and `CONFIG_PCI_P2PDMA=y`. The 7.2.6 kernel
   has the second and never mentions the first, while exporting
   `dma_buf_move_notify` in `/proc/kallsyms` -- so the capability is there and
   the probe cannot see it. `patches/ucx/03-rocm-dmabuf-kallsyms-fallback.patch`
   adds the symbol fallback upstream ROCm/ucx already has. Without it UCX calls
   plain `ibv_reg_mr` on a device pointer and gets EFAULT, which reads like a
   NIC that cannot take GPU memory.
2. **`UCX_ROCM_COPY_DMABUF=y` must be set.** The default is `n`, so even a UCX
   that knows dmabuf works will not export. This is a runtime setting, not
   something the patch covers.
3. **QEMU must do vfio-user P2P DMA.** An RDMA write into VRAM is one
   vfio-user device DMAing into another's BAR. Stock QEMU never advertises the
   BAR window and does not say so; `QEMU_IMAGE` therefore points at the
   sbates fork. See `env/nixl-mori`.

With 1 and 2 but not 3, registration succeeds and every transfer fails
`NIXL_ERR_REMOTE_DISCONNECT` -- which is the same error DRAM gives at the
16 MiB mesh ceiling, so it is easy to misread as a size limit.

`FI_UNIVERSE_SIZE` must be bounded for anything libfabric to initialise at
all; the image sets it to 16, and `cluster.sh stack` writes it into
`/etc/profile.d` on each guest because an `ENV` cannot travel inside a tarball
of `/opt`. Without it `ofi_rxm` asks the verbs provider for a completion queue
larger than the port's `max_cqe` and `fi_enable()` fails before any transfer,
reporting only `Invalid argument`.

## First thing to check when device memory will not register

**If a VRAM registration fails, suspect a kernel-config dmabuf probe before
you suspect the NIC.** This has now cost a day twice, in two different
libraries, with two different symptoms and the same root cause.

UCX and libfabric both decide whether ROCr dmabuf is usable by reading
`/boot/config-$(uname -r)` and requiring BOTH:

```
CONFIG_DMABUF_MOVE_NOTIFY=y
CONFIG_PCI_P2PDMA=y
```

The 7.2.6 guest kernel has the second, never mentions the first, and exports
`dma_buf_move_notify` in `/proc/kallsyms` -- which it would not if the code
were compiled out. So the capability is present and both probes conclude it
is absent. `patches/ucx/03` and `patches/libfabric/01` add the symbol
fallback; they are near-identical for that reason.

The two symptoms look nothing alike, which is what makes this worth writing
down:

| Library | What you see |
|---|---|
| UCX | `ibv_reg_mr(...) failed: Bad address`, then NIXL's "registration failed for the specified or all potential backends" |
| libfabric | `fi_mr_reg failed on rail 0: Bad address`, and **nothing else at any `FI_LOG_LEVEL`**, including an `--enable-debug` build |

libfabric's silence is structural rather than bad luck: a failed export takes
`vrb_reg_hmem_dmabuf()` to its `failover:` label with `saved_errno` still 0,
and the one log line on that path is conditional on `saved_errno` being
non-zero. The fallback then registers the device pointer with plain
`ibv_reg_mr`, which is what returns EFAULT.

Three checks, in order, before touching anything else:

```sh
grep -cE 'CONFIG_DMABUF_MOVE_NOTIFY=y|CONFIG_PCI_P2PDMA=y' /boot/config-$(uname -r)
grep -c dma_buf_move_notify /proc/kallsyms      # capability really present?
UCX_ROCM_COPY_DMABUF=try                        # and it must be set; default is n
```

A count of 1 from the first with a non-zero second is exactly this bug. Any
other library that probes the same way will fail the same silent way.

## Restarting pieces by hand

Three traps, all of which cost time to find:

* `tapsetup-N` joins **ernic-N's network namespace**, creates `tap0`, exits.
  Restarting `ernic-N` gives it a fresh namespace with no `tap0`, and it dies
  after two minutes. Start `tapsetup-N` again immediately after.
* Restarting `ernic-N` recreates its vfio-user socket, so the guest has lost
  its device: restart `qemu-N` as well.
* `rocjitsu-N` does not unlink its socket on exit and cannot rebind to the
  stale one. Remove `/run/vfu/rocjitsu-N.sock` before starting it again.
