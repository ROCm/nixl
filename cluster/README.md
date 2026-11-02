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
* a VM image to boot, in `VM_IMAGES_DIR`, named `<VM_NAME_PREFIX>-<n>.qcow2`

## Use

```
make cluster-up        # render the stack and bring it up
make cluster-prep      # memlock, ionic queue count, fabric address per guest
make cluster-gpu       # ip_discovery.bin + amdgpu-probe, once per boot
make cluster-nvme      # ext4 + mount the local NVMe on every guest, once
make cluster-status    # containers, guest reachability, RDMA device per guest
make cluster-down      # tear it down, volumes included
```

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

Measured 2026-11-02 on the four-guest cluster, after it moved to the
`ernic-rocjitsu` guest image. DRAM segments, vm1 <-> vm2, single runs, GB/s:

| Block | UCX | MORI_IO |
|---|---|---|
| 64 KiB | 0.195 | 0.078 |
| 256 KiB | 0.756 | 0.305 |
| 1 MiB | **2.139** | 1.078 |
| 4 MiB | **4.427** | 2.964 |
| 8 MiB | 3.882 | 3.387 |
| 16 MiB | fails | fails |

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
