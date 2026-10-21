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
* **LIBFABRIC** does not. libfabric itself is healthy on the emulated port --
  `fi_pingpong -e rdm` completes a full sweep -- but NIXL's plugin stalls in
  rendezvous, its rail manager being written around EFA.
* **AIS and AIS_MT** do not, because `amdgpu` never attaches to the emulated
  GPU: its BAR0 is 16 MB and 32-bit, and the memory controller needs a
  multi-gigabyte 64-bit prefetchable aperture, so the probe fails `-EINVAL`
  and no KFD device appears. Everything above hipFile fails downstream of
  that. Fixing it means giving the device model a large resizable BAR.

`FI_UNIVERSE_SIZE` must be bounded for anything libfabric to initialise at
all; the image sets it to 16. Without it `ofi_rxm` asks the verbs provider for
a completion queue larger than the port's `max_cqe` and `fi_enable()` fails
before any transfer.

## Restarting pieces by hand

Three traps, all of which cost time to find:

* `tapsetup-N` joins **ernic-N's network namespace**, creates `tap0`, exits.
  Restarting `ernic-N` gives it a fresh namespace with no `tap0`, and it dies
  after two minutes. Start `tapsetup-N` again immediately after.
* Restarting `ernic-N` recreates its vfio-user socket, so the guest has lost
  its device: restart `qemu-N` as well.
* `rocjitsu-N` does not unlink its socket on exit and cannot rebind to the
  stale one. Remove `/run/vfu/rocjitsu-N.sock` before starting it again.
