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
* **LIBFABRIC** does not, and the cause is now identified: **RDMA CM does not
  work on this fabric.** `rping`, which is nothing but RDMA CM, hangs on both
  sides with no output in either direction. libfabric's `verbs;ofi_rxm` sets
  its connections up through RDMA CM, so the plugin gets as far as creating
  its backend, both ranks reach "All processes are ready to proceed" -- that
  is the ASIO rendezvous, which is ordinary TCP and unaffected -- and then no
  fabric connection is ever established and the initiator dies on `ASIO
  Receive timeout`. UCX and MORI_IO are unaffected because they do their own
  out-of-band setup over TCP and use raw verbs QPs.

  This matches what `overnight-2026-10-22.md` recorded independently: of the
  three things LIBFABRIC needs, the published rocm-ernic image is not one of
  them. The other two are now in place --- `--enable_pt`, and the rdma-core
  ionic `sq_sig_all` fix, which the stock Ubuntu 61.0 provider lacks and
  without which libfabric gets no send completions at all above the 255-byte
  inject limit (see `patches/rdma-core/`). Neither is sufficient alone.

  Two corrections to what this file used to say. The device is not at fault
  in the way claimed --- UCX and MORI_IO move multiple GB/s over it. And the
  claim that `fi_pingpong -e rdm` completes a full sweep is **not reproduced
  here**; the attempt never got past fi_pingpong's own out-of-band control
  socket, so it settles nothing either way.
* **AIS and AIS_MT** reach the GPU but not past hipFile. Registration needs
  `HIPFILE_ALLOW_COMPAT_MODE=true`, and I/O then still fails
  (`hipFileWrite failed: Success`, `hipFileBatchIOSubmit failed: err=5030`).
  Not retested since the guest image moved.

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

## Restarting pieces by hand

Three traps, all of which cost time to find:

* `tapsetup-N` joins **ernic-N's network namespace**, creates `tap0`, exits.
  Restarting `ernic-N` gives it a fresh namespace with no `tap0`, and it dies
  after two minutes. Start `tapsetup-N` again immediately after.
* Restarting `ernic-N` recreates its vfio-user socket, so the guest has lost
  its device: restart `qemu-N` as well.
* `rocjitsu-N` does not unlink its socket on exit and cannot rebind to the
  stale one. Remove `/run/vfu/rocjitsu-N.sock` before starting it again.
