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
make cluster-nvme      # ext4 + mount the local NVMe on every guest, once
make cluster-status    # containers, guest reachability, RDMA device per guest
make cluster-down      # tear it down, volumes included
```

Guests are at `VM_SSH_PORT_BASE + n - 1` on localhost: VM 1 on 2400, VM 2 on
2401. Their RDMA-side addresses are `ERNIC_GUEST_SUBNET.1n`, so
`192.168.220.11` and `192.168.220.12`.

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

Measured on a two-guest instance; see `overnight-2026-10-20.md` for numbers.

* **UCX between guests** works. Bandwidth is tens of MB/s: the ERNIC data path
  is a TCP mesh between QEMU processes, so this measures the path, not a NIC.
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
* **LIBFABRIC** does not. libfabric itself is healthy on the emulated port --
  `fi_pingpong -e rdm` completes a full sweep -- and the plugin creates its
  backend and both processes reach "All processes are ready to proceed", but
  no transfer ever completes and the run dies on `ASIO Receive timeout`. Not
  a large-message problem: capping `--max_block_size` to 65536, below the
  rail manager's own 131072-byte striping threshold, fails identically, and
  no result row is printed at any size. The rail manager is written around
  EFA and says so in its own logs (`Could not find EFA devices connected to
  any PCIe switch`).
* **AIS and AIS_MT** reach the GPU but not past hipFile. With
  `VM_VFIO_USER_ROOT_PORT=true` -- which this overlay sets, and which is
  load-bearing, see `env/nixl-mori` -- `amdgpu` binds, `/dev/kfd` appears and
  HIP sees a device. Registration needs `HIPFILE_ALLOW_COMPAT_MODE=true`, and
  I/O then still fails (`hipFileWrite failed: Success`,
  `hipFileBatchIOSubmit failed: err=5030`).

GPU *memory* is not reachable over the emulated ERNIC at all: `ibv_reg_mr` on
ROCm memory fails `-EFAULT` at every size tried, from 512 MB down to 64 MB, so
this is not a BAR-window limit. Use `DRAM` segment types here; `VRAM` ones
cannot register.

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
