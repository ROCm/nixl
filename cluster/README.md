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
* **LIBFABRIC** does not, and still does not after the guest image change and
  after libfabric moved to 2.7.0 built `--with-rocr`. The plugin creates its
  backend, both processes reach "All processes are ready to proceed", and then
  no transfer ever completes: the initiator dies on `ASIO Receive timeout`.
  No result row is printed at any size.

  What is new is that the device is now clearly exonerated. UCX and MORI_IO
  both move multiple GB/s over this same fabric, so whatever LIBFABRIC is
  hitting is in the `verbs;ofi_rxm` path or in the plugin's rail manager, not
  in the emulated port. (The rail manager is written around EFA and says so in
  its own logs: `Could not find EFA devices connected to any PCIe switch`.)
  Note that the earlier claim here that `fi_pingpong -e rdm` completes a full
  sweep has NOT been reproduced on this cluster -- the attempt did not get
  past fi_pingpong's own out-of-band connect, which is its control socket
  rather than the fabric, so it settles nothing either way and wants redoing.
* **AIS and AIS_MT** reach the GPU but not past hipFile. Registration needs
  `HIPFILE_ALLOW_COMPAT_MODE=true`, and I/O then still fails
  (`hipFileWrite failed: Success`, `hipFileBatchIOSubmit failed: err=5030`).
  Not retested since the guest image moved.

The GPU is driven by `make cluster-gpu`; `VM_VFIO_USER_ROOT_PORT=true` in
`env/nixl-mori` is load-bearing for it. Whether GPU *memory* is reachable over
the emulated ERNIC is **open**. It failed `ibv_reg_mr -EFAULT` at every size
on the old image, but that was with a libfabric built without ROCR and on a
guest with no amdgpu at all, so the result does not carry over. Retest VRAM
segments before trusting either answer.

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
