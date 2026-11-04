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
`ld.so.conf.d` entry. Three system libraries are not in the tarball because
they come from apt rather than `/opt` -- `libgflags2.2`, `libhwloc15` and
`libcurl4t64`, the last being the AWS SDK's HTTP client and so the OBJ
plugin's -- and `cluster-stack` installs them.

## S3 over RDMA, in one VM

`rocm-ernic`'s `--backend s3` runs an object store, an HTTP endpoint and an
RDMA data mover inside the server process, so one guest and one rail are a
complete object fabric: no second VM, no storage backend, and no `--tap`,
because the control plane is terminated in band on the emulated wire. That is
what NIXL's OBJ backend talks to here, through the `hipobj` engine
(`patches/nixl/08`), which is the ROCm counterpart of NIXL's CUDA-only Dell
one.

```
make cluster-s3                             # vm4 rail 2 becomes the S3 server
make cluster-gpu ANSIBLE_ARGS='--limit vm4' # the VM was restarted
```

Then, in the guest:

```
nixlbench --backend OBJ \
    --obj_accelerated_enable=true --obj_accelerated_type=hipobj \
    --obj_hipobj_nic=rocep3s0 \
    --obj_endpoint_override=192.168.80.200:9000 --obj_scheme=http \
    --obj_bucket_name=ernic --obj_access_key=e --obj_secret_key=e \
    --obj_use_virtual_addressing=false \
    --op_type WRITE --initiator_seg_type VRAM \
    --start_block_size 4096 --max_block_size 4194304
```

`make cluster-s3-restore` puts the rail back in the TCP mesh.

**`--obj_hipobj_nic` is not optional.** hipObject picks its RDMA device by
proximity to the GPU, and the server reads the client buffer by rkey,
resolving it in its own MR table -- one emulator owns every registration one
guest holds. An rkey minted on the other rail's device means nothing to it,
and the symptom is a 25-second stall and then HTTP 500, "The RDMA read from
the client buffer did not complete."

**The endpoint needs a host route**, which `make cluster-s3` adds. Every rail
sits in the same `ERNIC_GUEST_SUBNET` /20, so the endpoint address is on-link
for all of them, the kernel picks the first, and the request leaves on rail 1
to a wire with no S3 server on it: "No route to host".

Measured 2026-11-02, vm4 rail 2, GB/s, with `HIPOBJ_REQUIRE_GPU_DIRECT=1` on
the VRAM runs so a silent fall back to host staging would fail rather than be
measured:

| Block | WRITE DRAM | WRITE VRAM | READ DRAM | READ VRAM |
|---|---|---|---|---|
| 4 KiB | 0.027 | 0.026 | 0.038 | 0.028 |
| 64 KiB | 0.258 | 0.262 | 0.580 | 0.463 |
| 1 MiB | 0.558 | 0.559 | 7.32 | 6.20 |
| 4 MiB | 0.553 | 0.595 | 12.68 | 18.00 |

VRAM is at parity with DRAM, which is the point: the server RDMAs straight
into GPU memory, through dmabuf, with no staging copy.

Read the two columns differently, though. WRITE latency grows linearly with
size -- 150 us at 4 KiB to 7.0 ms at 4 MiB -- so the server's RDMA READ really
is paying per byte. READ latency is flat, 110 to 330 us across three orders of
magnitude, so those numbers are a fixed HTTP round trip over a push that costs
the emulator almost nothing. The large READ figures are not fabric bandwidth.

One wrinkle in the harness: nixlbench seeds an object with the real `aws` CLI
before a READ run, and aws-cli v2's chunk-signed `PutObject` is more HTTP than
this server's parser implements. `cluster-stack` installs
`cluster/scripts/aws-curl-shim.sh` as `~/bin/aws` on each guest, which does
the same three commands with `curl` -- and with multipart above 15 KiB,
because a whole request is capped at 16 KiB. It touches only the seeding; the
measured transfer still goes through NIXL and the `x-amz-rdma-token` path.

## NVMe Key Value, over TCP and over RDMA

`patches/nixl/09` adds an `NVME_KV` backend: the ROCm-reachable counterpart of
NIXL's upstream DOCA_MEMOS plugin (ai-dynamo/nixl#1717), which needs doca-kv
and a BlueField-3. It keeps DOCA_MEMOS's NIXL contract — DRAM_SEG value,
OBJ_SEG key, WRITE is Store, READ is Retrieve, `queryMem()` is Exist — and
issues the commands over io_uring NVMe passthrough instead, so it needs no
vendor library at all.

That works because Linux attaches a KV namespace and hides only its *block*
device: `nvme nvme3: block device for nsid 1 not supported (csi 1)`, and
`/dev/ng3n1` appears and takes passthrough.

### Serving one: rocm-ernic, one VM and one rail

`--backend nvmeof:kv=1` turns the NVMe-oF target's namespace into a Key
Value one. One guest and one rail are then a complete KV fabric -- no SPDK,
no second VM -- exactly as the S3 backend is for objects:

```
rocm-ernic -s /run/vfu/ernic-4-2.sock -m <mac> \
    -b nvmeof:kv=1,size=256M,keys=4096,maxval=1M,ip=192.168.80.201
```

In the guest, the neighbour entry is the unobvious step: the controller is
not a host on the segment and has no MAC, so address resolution has nothing
to resolve to and the connect fails with `rdma connection establishment
failed (-104)` and nothing at all in the target's log. Any link-layer
address will do -- the emulator routes by queue pair, not by MAC.

```
sudo ip neigh replace 192.168.80.201 lladdr 02:00:00:00:c0:01 \
    dev enp3s0np0 nud permanent
sudo ip route replace 192.168.80.201/32 dev enp3s0np0 src 192.168.80.18
sudo nvme connect -t rdma -a 192.168.80.201 -s 4420 -n nvmet-test
```

Measured 2026-11-03, vm4, 128 KiB values, GB/s:

| | Store | Retrieve |
|---|---|---|
| DRAM | 6.991 (1.28x) | 7.551 (1.91x) |
| VRAM | 0.330 (1.03x) | 5.660 (1.46x) |

Median of eight consecutive runs, with the min-to-max spread of those eight
in brackets. Taken with `cluster/scripts/bench.sh NVME_KV`, on ernic
128bad7. Against the 461dbbd numbers this table used to carry -- Store DRAM
6.141, Retrieve DRAM 5.072, Store VRAM 0.062, Retrieve VRAM 2.126 -- every
figure is up, Store VRAM by a factor of five.

**Read the spreads before the medians.** Retrieve varies by 1.9x across
eight back-to-back runs on a quiet-looking host, so a single Retrieve number
from this cluster means very little; an earlier three-run sample of this
same table reported 9.290 and 7.049 for the two Retrieve cases, both near
the top of the range rather than the middle.

Store VRAM is the exception at 1.03x, and the reason is worth noting: it is
almost entirely a device-to-host copy, which is CPU work of a fixed size and
does not care what the fabric or the neighbours are doing. The paths that
vary are the ones that actually cross the emulator.

Many times the SPDK path below, with latency flat in size -- 13.2, 12.3,
13.8, 14.0, 14.1 and 13.9 us from 4 KiB to 128 KiB, so bandwidth is just the
value size over a fixed cost -- rather than climbing, because the data never
leaves the host: `dp_sge_to_host()` is a copy within the emulator's own
address space.

**The VRAM Store number is still the staging copy, and it is still the
argument for finishing the job.** 7.0 GB/s against 0.330 is a factor of
twenty, and essentially all of it is the device-to-host copy: `post` is
375.6 us of the 376.9. The large BAR and p2pdma registration landing did not
move it, which is consistent -- this client does not take that path.
The target can already RDMA straight into GPU memory -- it reaches guest
memory by rkey, and that is the same path the hipObject S3 engine uses
GPU-direct. What stages is the *client*, because it goes through the kernel
NVMe stack. A userspace initiator would close it.

### Serving one: SPDK

SPDK's `kvdev_mem` is the other target. The published
`ubuntu-spdk-libvfio-user` image is built `CONFIG_RDMA=n`, so it can serve KV
over TCP but not over RDMA; `--with-rdma` and `libibverbs-dev librdmacm-dev`
are what the RDMA build needs.

Over TCP, from a container on `nixl-mori_fleet`, which the guests reach
through QEMU's NAT:

```
docker run -d --name nixl-mori-spdk-kv --network nixl-mori_fleet --privileged \
    -v /tmp/spdkkv:/cfg -e SPDK_JSON_CONFIG=/cfg/empty.json \
    sbates130272/batesste-ci-images-ubuntu-spdk-libvfio-user:latest
# /cfg/empty.json is {"subsystems":[]}, which skips the image's VFIOUSER setup
rpc.py nvmf_create_transport -t TCP
rpc.py kvdev_mem_create kv0 -v 4194304 -k 65536
rpc.py nvmf_create_subsystem nqn.2026-11.io.nixl:kv0 -a -s NIXLKV001
rpc.py nvmf_subsystem_add_kv_ns nqn.2026-11.io.nixl:kv0 kv0 -n 1
rpc.py nvmf_subsystem_add_listener nqn.2026-11.io.nixl:kv0 -t TCP -a <ip> -s 4420
```

Over RDMA the target has to be *inside* a guest, because the fabric is the
emulated ernic and only a guest has one. Unpack an RDMA-enabled SPDK into a
VM and run it under systemd — not `nohup ... &` over ssh, which dies with the
session — then listen on that VM's rail address:

```
sudo systemd-run --unit=spdk-nvmf --collect \
    /opt/spdk/build/bin/nvmf_tgt -r /var/tmp/spdk.sock -m 0x3 --no-huge -s 1024
rpc.py nvmf_create_transport -t RDMA -q 128 -u 8192 --no-srq
rpc.py nvmf_subsystem_add_listener ... -t RDMA -a 192.168.80.15 -s 4420
```

`--no-srq` is not optional: the ernic reports `max_srq: 0`.

### Three things that will bite

**The listener address must match exactly.** A listener on `0.0.0.0` refuses a
guest that connects to the concrete address — "does not allow host ... to
connect at this address" — and adding a second listener on the same port
fails with "Invalid parameters". Remove the wildcard one and add the real
address.

**Route the target to a mesh rail.** Every rail shares one
`ERNIC_GUEST_SUBNET` /20, so the kernel picks whichever it likes. On a VM
whose rail 2 is the S3 server, that rail is not a mesh member and the
connection resets with `rdma connection establishment failed (-104)` and
nothing at all in the target's log. `ip route replace <target>/32 dev
enp1s0np0` fixes it.

**MDTS caps a value at 128 KiB here.** SPDK reports MDTS 5, so one value is at
most 2^5 x 4 KiB whatever the kvdev's `--max-value-len` says. The plugin reads
MDTS at startup and says so; without that the kernel rejects the command
before the device sees it and the only symptom is a bare EINVAL.

### Running it

```
nixlbench --backend NVME_KV --nvme_kv_device=/dev/ng3n1 \
    --op_type WRITE --initiator_seg_type DRAM \
    --start_block_size 4096 --max_block_size 131072
```

Passthrough is root-only, and sudo drops the environment, so pass
`NIXL_PLUGIN_DIR` and `LD_LIBRARY_PATH` through explicitly rather than
relying on `sudo -E`.

**The device node does not tell you the namespace id, and the plugin needs
both.** `--nvme_kv_device` names a character device; which namespace that is
comes from `NVME_IOCTL_ID`, not from the name. Linux names namespace devices
by the head instance it allocated, and a hidden KV namespace still consumes
one, so on a controller serving a block namespace *and* a KV one, `/dev/ng2n1`
is nsid 2 and nsid 1 is `/dev/nvme2n2`. Resolve it rather than assuming:

```
for d in /sys/class/nvme/nvme2/nvme2n*; do
    echo "$(basename "$d") -> nsid $(cat "$d/nsid")"
done
```

This is not pedantry. Store is opcode 01h and so is block Write; Delete is 10h
and so is Dataset Management. A command aimed at the wrong namespace of the
same controller is not refused as nonsense, it is executed as something else.

**A READ run needs block size == value size.** nixlbench seeds one object at
the *registration* size and then reads it in *block*-sized pieces, which is
S3's model: a KV value is atomic and there is no ranged Retrieve. Reading 4
KiB of a 128 KiB value trips the plugin's length check, which is correct and
is what DOCA_MEMOS does too. Use
`--start_block_size N --max_block_size N --total_buffer_size N`.

Measured 2026-11-03, vm4, DRAM, GB/s and average latency:

| Value | Store TCP | Store RDMA | Retrieve TCP | Retrieve RDMA |
|---|---|---|---|---|
| 4 KiB | 0.248 / 17 us | 0.156 / 26 us | 0.317 / 13 us | 0.098 / 42 us |
| 16 KiB | 0.262 / 62 us | 0.352 / 47 us | 0.178 / 92 us | 0.487 / 34 us |
| 64 KiB | 0.300 / 218 us | 0.727 / 90 us | 0.420 / 156 us | 1.361 / 48 us |
| 128 KiB | 0.357 / 368 us | 1.164 / 113 us | 0.439 / 299 us | 1.812 / 72 us |

RDMA is 3-4x TCP at 128 KiB and its latency grows far more slowly with size,
which is the whole point. TCP wins at 4 KiB, where per-command overhead
dominates and the RDMA path has more of it.

### VRAM works, and is staged rather than GPU-direct

`--initiator_seg_type VRAM` works, and a round trip is byte-verified on both
transports: a pattern written to device memory, Stored through NIXL, the
device buffer wiped, Retrieved back and read off the device again, 65536
bytes identical. `cluster/scripts/kv-vram-roundtrip.cpp` does it, with the
build line in its header; it is a standalone check rather than nixlbench's
own consistency flag.

It is a staged path, not GPU-direct, and the reason is structural rather
than a missing feature. NVMe passthrough maps its data buffer with
`blk_rq_map_user_io()` or `io_uring_cmd_import_fixed()`, both of which pin
*user pages*; a device pointer has none, and there is no dmabuf or p2pdma
path in `drivers/nvme/host/ioctl.c` to take one instead. Nor would p2pdma
help over nvme-rdma, whose `nvme_rdma_map_data()` builds an sg_table from the
request's bio pages (`blk_rq_map_sg`, then `ib_dma_map_sg`) and has no dmabuf
path at all. So a VRAM registration gets a pinned host staging buffer and the
plugin copies into it before a Store and out of it after a Retrieve --
exactly what hipObject does when a device pointer cannot be registered with
the NIC. DOCA_MEMOS sidesteps the question by taking DRAM only.

### Large BAR, p2pdma, and where it stops

The emulated GPU can be made to publish PCI P2PDMA, and it takes three
things together:

* `ROCJITSU_IMAGE` at `rocjitsu.73c3cf3` or later. Earlier images predate
  batesste-ci-images `668a48f`, which moved `rocjitsu_vram_aperture` from
  `keep` to `max`; their `vram-aperture` label is empty and the BAR is
  256 MiB.
* `VM_CPU_PHYS_BITS=41` and `VM_PCI_HOLE64_SIZE=512G`. qemu-tool otherwise
  defaults to `-cpu EPYC`, 40 physical address bits and a 256 GiB window,
  and the guest then fails to assign **both** BAR0 and BAR2 and ends up
  with no VRAM window at all -- worse than the small BAR, and silent.
* A qcow2 built from qemu-minimal `ae551b3` or later, which is where those
  two run-vm options came from.

With all three, the guest reports what it should:

```
amdgpu: [drm] Detected VRAM RAM=442368M, BAR=262144M
Memory at 4000000000 (64-bit, prefetchable) [size=256G]
/sys/bus/pci/devices/0000:02:00.0/p2pmem
```

256 GiB of BAR against 256 MiB before, and **both GPUs now publish a
p2pmem provider**, which they did not at all previously.

**And the kernel path can use it -- my earlier claim that it could not was
wrong.** The out-of-tree amdgpu carries AIS-specific P2PDMA support:

```
AIS: Failed to add PCI P2PDMA resource for VRAM %d
AIS: NVMeoF %s -> RDMA NIC %s
AIS: P2P DMA not accessible
pci_p2pdma_add_resource    pci_p2pdma_distance_many
```

hipFile does not hand the kernel a VRAM user pointer and hope `get_user_pages`
works -- it cannot, and that is what misled me. It calls into amdgpu, which
returns **p2pdma-backed pages**, and builds the bvec and sgtable from those.
They are real `struct page`s, so `ib_dma_map_sg()` maps them through
`pci_p2pdma_map_sg()` to bus addresses and the ordinary nvme-rdma path works.
amdgpu even resolves an NVMe-oF device to the RDMA NIC behind it and validates
P2P to that NIC, which is exactly the fabrics case. With the large BAR in
place the GPU publishes 1 GiB:

```
/sys/bus/pci/devices/0000:02:00.0/p2pmem/{allocate,available,published}
size = 1073741824
```

So GPU-direct NVMe KV does not need a userspace initiator. It needs the
buffer described by that ioctl rather than by a user virtual address.

**It no longer hangs, and the fastpath now works -- and is five times
slower than staging.** This file used to say that
`HIPFILE_ALLOW_COMPAT_MODE=false` wedged the guest. It does not: more than a
dozen AIS and AIS_MT VRAM runs completed normally, with
`dmesg | grep -c "blocked for more than"` at zero afterwards.

**Use FORCE, not ALLOW, to tell the two paths apart.**
`HIPFILE_ALLOW_COMPAT_MODE=true` *permits* the staging fallback, it does not
select it, so with a working fastpath both settings of ALLOW take the
fastpath and read identically -- which is not evidence of anything.
`HIPFILE_FORCE_COMPAT_MODE=true` is the control. 1 MiB blocks, three runs:

| | GB/s | latency |
|---|---|---|
| `HIPFILE_ALLOW_COMPAT_MODE=false` (p2p fastpath) | 0.082, 0.087, 0.086 | 14738 us |
| `HIPFILE_FORCE_COMPAT_MODE=true` (staging) | 0.422, 0.429, 0.459 | 2476 us |

**This also explains the AIS numbers above.** The 0.400 GB/s recorded for
1 MiB AIS_MT is staging -- it matches the FORCE column, not the fastpath --
because at the time it was measured the fastpath did not work and
`ALLOW=true` fell back. Now that the fastpath engages, the same command
reads ~0.085. That is not a regression in the fabric; it is the fastpath
arriving and being slower than the thing it replaces.

So the original position in this file was right about where to look and
wrong only about the hang: **the thing to debug is the emulated NVMe
controller servicing a DMA into the GPU's BAR**, which is a vfio-user
region belonging to rocjitsu. 14.7 ms to move 1 MiB is about 14 us per
4 KiB page, which is round-trip shaped rather than copy shaped.

**The cost is QEMU's bounce buffer, and it is a four-line fix away.**
Chased down 2026-11-03. The DMA works and the data is correct; what is slow
is how QEMU moves it.

QEMU *does* map the GPU BAR -- two 256 GiB shared mappings of
`/memfd:rocjitsu-vram` are in its address space, so the vfio-user region fd
is being shared and mapped as designed. But the emulated NVMe does not get
a direct pointer to it for DMA. It falls back to QEMU's bounce-buffer path,
and that path is capped:

```
$ qemu-system-x86_64 -device nvme,help | grep bounce
  x-max-bounce-buffer-size=<size> - Maximum buffer size allocated for bounce
    buffers used for mapped access to indirect DMA memory (default: 4096)
```

4096 bytes. A 1 MiB write from the BAR becomes 256 map/copy/unmap cycles,
and the throughput becomes buffer-size over per-cycle cost -- a constant,
independent of how much you ask for. That is exactly the shape measured:
0.072 GB/s at 256 KiB and 0.077 at 1 MiB, flat where staging scales.

Raising it on the NVMe -- the DMA *initiator*, not the vfio-user device --
confirms it. vm4 with `x-max-bounce-buffer-size=16M`:

| Block | bounce 4096 | bounce 16M |
|---|---|---|
| 256 KiB | 0.072 | 0.210 |
| 1 MiB | 0.077 | 0.321 (5 runs, 0.314-0.323) |
| 4 MiB | -- | 0.369 |

4.2x at 1 MiB, and the flat ceiling becomes a curve. `qemu-tool` sets this
property nowhere, so every emulated NVMe in this fleet runs at the 4096
default; `run-vm`'s literal `--nvme` form is how the above was tested.

**Why 4096, and why it bounces at all.** Neither is a vfio-user thing and
neither needs the fork.

QEMU refuses direct access to *any* `ram_device` region -- the class that
covers an mmap'd device BAR, vfio-user or real VFIO passthrough alike --
because of commit `4a2e242bbb`, "memory: Don't use memcpy for ram_device
regions". A variable-length `memcpy` can split one 4-byte guest access into
several byte-sized ones, which breaks real MMIO registers. So
`memory_access_is_direct()` returns false for these regions and
`address_space_map()` falls back to a bounce buffer, for correctness
reasons that have nothing to do with us.

The 4096 is the historical bounce buffer size. QEMU used to have exactly
one per `AddressSpace`, clamped to `TARGET_PAGE_SIZE`. Mattias Nissler's
"softmmu: Support concurrent bounce buffers" (commit `637b0aa1`, 9.1.1 and
9.2) made them dynamic and per-device and set the default budget to 4096
*to match that previous maximum*. It is a budget rather than a per-mapping
size, so one large map can take all of it.

This is a known upstream problem with a fix in flight: the same shape is
reported for a passed-through GPU's BAR used as a DMA target, including the
guest hangs that this file once recorded, and Gavin Shan's series (v5,
reviewed by Peter Maydell) replaces the `memmove` with a `qemu_ram_move()`
that uses qatomics for small aligned accesses and then reverts
`4a2e242bbb`, making ram device regions directly accessible again. If that
lands, the bounce disappears rather than merely getting bigger.

So the answer to "do we need the sbates fork for this" is no. The
vfio-user client is upstream from QEMU 10.1, and `use_region_fds` /
`vfio_device_get_region_fd()` are part of that same upstream series, not
fork additions. `pci-mmio-bridge` is the only fork-only piece here and it
is a tracing device. Stock QEMU 10.1 or newer behaves identically, and the
`x-max-bounce-buffer-size` workaround applies there too.

It is still a copy, so the fastpath remains ~22% under staging (0.321
against 0.412). Closing that needs the BAR to be RAM from the DMA path's
point of view rather than something to bounce through. The fork carries
`use_region_fds` and `vfio_device_get_region_fd` and clearly uses them for
the guest's own access to the BAR; it is the device-DMA path that still
bounces.

`--pci-mmio-bridge` is NOT that mechanism and is worth not confusing with
it: it is an MMIO *tracing* device -- an 8 KiB shadow window polled every
millisecond, with `-trace enable=pci_mmio_*` -- and qemu-tool notes that it
forces `ioeventfd=off,dbcs=off` on the NVMe, which would make this slower
rather than faster.

### Which p2pdma path you are on decides everything

Measured 2026-11-03, vm4, 64 MiB copies for the GPU cases and 1 MiB blocks
for the storage and fabric ones. The emulated GPU can be a DMA peer for
several different things, and they span two orders of magnitude. The
differences are NOT about p2pdma support, which all of them have:

| Path | The copy is done by | GB/s |
|---|---|---|
| GPU <-> emulated NIC (ernic) | the ernic, in its own address space | 1.05 - 1.21 |
| GPU -> host -> GPU, SDMA | rocjitsu SDMA engine | 0.62 |
| GPU -> GPU peer, SDMA | rocjitsu SDMA engine | 0.38 - 0.41 |
| GPU -> QEMU emulated NVMe | QEMU, 16 MiB bounce | 0.321 |
| GPU -> GPU peer, GFX blit | rocjitsu compute path | 0.084 |
| GPU -> QEMU emulated NVMe | QEMU, default 4096 bounce | 0.077 |
| GPU -> host -> GPU, GFX blit | rocjitsu compute path | 0.040 |
| GPU -> GPU same device | rocjitsu compute path, always | 0.043 |

And the guest CPU reaching the BAR directly, for reference:

| Access | GB/s |
|---|---|
| CPU -> BAR write | 1.525 |
| plain RAM memcpy (reference) | 2.957 |
| BAR -> CPU read | 0.154 |

**SDMA versus GFX blit is a 5x lever, and it is on by default.** ROCr picks
SDMA unless told otherwise; `HSA_ENABLE_SDMA=0` or `HSA_ENABLE_PEER_SDMA=0`
forces the blit kernel path and costs 4.6x on a peer copy (0.38 to 0.084)
and 15x on a host round trip (0.62 to 0.040). The emulated device really
does have the engines -- `num_sdma_engines 2`, `sdma_v7_1_0`, rings
`sdma0.0` and `sdma0.1` -- so this is not a fallback being papered over.

**Same-device `hipMemcpyDeviceToDevice` never gets SDMA.** It measures
0.043 GB/s with SDMA on, off, and at the default, which is blit-path speed.
It is the slowest way to move bytes on this cluster, slower than going out
to host memory and back, and no environment variable changed it.

**Peer is still slower than staging, even on SDMA.** 0.38 - 0.41 against
0.62 for the same 64 MiB through host memory. An earlier revision of this
file blamed the compute path for that; it is wrong, both are on SDMA. On
this emulator a peer copy is simply dearer than two host copies. That says
nothing about real hardware, and it does mean a benchmark assuming peer
copies are free will mislead here.

**The NIC path is fastest because the ernic holds the GPU's memory.**
`rocm-ernic` maps BOTH `/memfd:rocjitsu-vram` descriptors, 256 GiB each,
exactly as QEMU does. An RDMA transfer to or from VRAM is therefore a
memcpy inside the ernic process, with QEMU not in the data path at all --
which is why the VRAM transports have always measured the same as the DRAM
ones rather than at the rate the bounce path imposes.

**The NVMe path is slowest because the NVMe is inside QEMU.** A QEMU device
model reaching VRAM goes through `address_space_map()`, hits the
`ram_device` rule, and bounces. Nothing about the GPU, the BAR or p2pdma
differs between this and the NIC case; only the identity of the copier
does. A vfio-user peer gets the region fd; an in-QEMU device model gets a
4 KiB bounce buffer.

**What the memfd sharing actually buys: mapping, not zero copy.** The
descriptor gives every holder the same physical pages, so nobody has to
copy data in order to reach it -- that part is pointer passing. The guest
CPU numbers above show it is a real mapping rather than trapped access: at
1.5 GB/s, writes cannot be going out to the server per access. The
read/write asymmetry is PCI caching semantics on a prefetchable BAR, not
message round trips.

What it does NOT buy is the elimination of copies in the data path. The
ernic still memcpys out of those pages to put bytes on the wire; it just
does it once, from the real VRAM, with no staging buffer and no QEMU.
QEMU's NVMe copies twice, into and out of a bounce buffer, which is the
entire difference between the two rows.

### A GFX kernel cannot ring another device's doorbell, and fails silently

This is the GPUDirect Async / DOCA GPUNetIO shape: a kernel on the GPU
writes the NIC's doorbell to post work without waking the CPU. Tested
2026-11-03 on vm4. It does not work here, and nothing tells you so.

Every API call succeeds:

```
mmap ernic BAR2            ok
hipHostRegister(IoMemory)  ok          <- 0x4, hipHostRegisterIoMemory
hipHostGetDevicePointer    ok
kernel -> ernic doorbell   no error    <- launch and synchronize both clean
```

And the data is gone:

```
BAR2[4] seeded by CPU        : 0x11110000
kernel 5000 stores           : no error
BAR2[4] after kernel stores  : 0x11110000   LOST
```

The ernic's own counters agree: `mmio_writes_total` moves by 2 across a run
with ZERO kernel stores and by 4 across a run with 5000 of them. The stores
never reached the device.

The control in the same binary rules out the measurement: a kernel store to
the *peer GPU's* VRAM lands every time and reads back correctly, and a CPU
store to the very same doorbell address lands immediately. So kernels can
write to another vfio-user device -- just not this one.

**Why.** `rocm-ernic` registers both BARs with no mmap areas and no fd:

```c
vfu_setup_region(vfu_ctx, VFU_PCI_DEV_BAR2_REGION_IDX, IONIC_BAR2_DB_SIZE,
                 bar2_access, VFU_REGION_FLAG_RW | VFU_REGION_FLAG_MEM,
                 NULL, 0, -1, 0);
```

so the doorbell page is trap-only: every access must become a vfio-user
message to the ernic process. The guest CPU can do that, because its
accesses trap through KVM to QEMU, which owns the vfio-user connection.
rocjitsu cannot: its shared mappings are the two `rocjitsu-vram` memfds and
guest RAM, and nothing else. It has no mapping of the ernic's BAR and no
channel to the ernic, so a store that resolves there in the emulated GPU's
address space is simply dropped.

That is also why the peer-VRAM case works and this one does not. VRAM is
shared by descriptor and really is the same pages in both processes; a
trap-only BAR is not shared at all.

**What it would take.** The ernic would have to export its doorbell region
with a real fd -- `vfu_setup_region` with an mmap area backed by a memfd --
so that QEMU could hand that fd to rocjitsu the way it already does for
VRAM. That is a change in `src/rocm_ernic_server.c`, not in QEMU or in HIP,
and it would trade the ernic's ability to see every doorbell write (which
is how its stats and its queue scheduling work) for the ability to let a
kernel write them. Worth knowing before anyone plans GPU-initiated
networking on this cluster.

**What is NOT evidence, recorded so it is not re-derived.** `published`
reads 0 and `p2pmem/available` never drops during a transfer, and neither
means the fastpath is unused. `pci_p2pmem_publish()` only sets the flag
`pci_has_p2pmem()` reports, which exists so OTHER drivers can discover a
provider through `pci_p2pmem_find()`; a driver consuming its own pages has
no reason to call it. Likewise the `available` counter tracks the genalloc
pool that `pci_alloc_p2pmem()` draws from, so a driver handing out its own
pages by another route leaves it untouched. amdgpu referencing only
`pci_p2pdma_add_resource` and `pci_p2pdma_distance_many` is consistent with
exactly that -- it registers the resource, checks reachability, and consumes
the pages internally; the DMA mapping of those pages happens in the
consumer, not in amdgpu.

**The limit is the kernel client, not the fabric.** rocm-ernic's NVMe-oF
target reaches guest memory with `dp_nvmeof_from_host()`, which is
`dp_sge_to_host()` -- the same rkey path the S3 backend uses, and the one
that already RDMAs straight into GPU memory for the hipObject engine. So
GPU-direct KV is reachable on exactly this emulated hardware; what cannot get
there is a client that goes through the kernel's NVMe stack, because that
stack can only describe host pages. Closing the gap needs a userspace
initiator that registers VRAM with `ibv_reg_dmabuf_mr` and puts that rkey in
the command's keyed SGL itself -- which is what `tests/s3_rdma_client.c` in
rocm-ernic already does for S3.

**On this cluster the staging copy dominates everything else**, and that is
the emulated GPU rather than the design. Store, RDMA:

| Value | Latency | of which Post (the D2H copy) | Tx |
|---|---|---|---|
| 4 KiB | 589 us | 577 us | 17 us |
| 32 KiB | 2114 us | 2089 us | 27 us |
| 128 KiB | 2144 us | 2118 us | 30 us |

A standalone `hipMemcpy` on this GPU measures 497 us at 4 KiB and 2113 us at
32 KiB and above — so the plugin's Post time *is* the copy, with nothing
measurable on top. The numbers are flat in size because the cost is per-call,
not per-byte.

The two directions behave differently, which is worth knowing before reading
the Retrieve numbers as a contradiction. Cold and single-shot, both cost
about 2 ms (a lone Retrieve into VRAM measures 1941 us). Across a benchmark
loop the host-to-device copy amortises to ~57 us while device-to-host stays
at ~2.1 ms, so VRAM Retrieve looks close to DRAM and VRAM Store does not:

| Value | Store DRAM | Store VRAM | Retrieve DRAM | Retrieve VRAM |
|---|---|---|---|---|
| 4 KiB | 0.156 / 26 us | 0.007 / 589 us | 0.098 / 42 us | 0.083 / 49 us |
| 128 KiB | 1.164 / 113 us | 0.061 / 2144 us | 1.812 / 72 us | 0.989 / 133 us |

The plugin's code path is the same in both directions, so the asymmetry is
the emulator's. On real hardware a 128 KiB copy either way is single-digit
microseconds and none of this would be visible.

Two things in the plugin do matter for the staging cost, and both are worth
keeping if this is ever ported: the copies go on a stream of the engine's own
rather than the null stream, which otherwise waits on unrelated GPU work (that
alone took 4 KiB Post from 1482 us to 565 us), and a request's descriptors are
all issued before anything is waited on, so a batch pays one synchronisation
rather than one per value.

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
  blocks, medians of eight runs: LIBFABRIC 2.174 GB/s, UCX 2.103,
  MORI_IO 0.944. See "VRAM over the fabric works" below for the spread and
  for why a smaller sample on this host is not worth quoting.

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
  and `hipFileBatchIOSubmit failed: err=5030`. Two-rail cluster,
  consistency-checked, `--filepath` on a local NVMe, GB/s:

  | Block | AIS_MT | AIS | POSIX (DRAM) |
  |---|---|---|---|
  | 256 KiB | 0.140 | 0.137 | 0.732 |
  | 512 KiB | 0.199 | 0.196 | 1.031 |
  | 1 MiB | 0.400 | 0.406 | 1.361 |

  **These are the STAGING numbers, and they are the ones to want.** At the
  time they were taken the p2p fastpath did not work, so
  `HIPFILE_ALLOW_COMPAT_MODE=true` fell back to staging; forcing staging
  today with `HIPFILE_FORCE_COMPAT_MODE=true` reproduces them (1 MiB AIS_MT
  0.422, 0.429, 0.459). The fastpath works now and reads ~0.085 at 1 MiB,
  five times slower, so leaving the flag alone silently costs you the
  difference. Use `HIPFILE_FORCE_COMPAT_MODE=true` to get this table until
  the DMA into the GPU's BAR is fixed; see "VRAM over the fabric" above.

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

All three transports move VRAM now. LIBFABRIC needed `patches/libfabric/01`
as well as `patches/nixl/07`; see the transport list below for why.

1 MiB blocks, 16 MiB buffer, vm1 <-> vm2. **Eight consecutive runs each,
median reported** -- see the warning below about why two is not enough here.
On the pinned ernic 128bad7:

| Backend | VRAM median | VRAM min-max | on 8e43a0c | previously recorded |
|---|---|---|---|---|
| UCX | 1.826 | 1.522 - 1.872 | 2.103 | 2.333 |
| LIBFABRIC | 1.978 | 1.491 - 2.002 | 2.174 | 2.247 |
| MORI_IO | 0.985 | 0.823 - 1.009 | 0.944 | 0.816 |

UCX DRAM on 8e43a0c over eight runs: median 2.162, range 2.093 - 2.253.

Nothing here supports a regression at any step. The 8e43a0c column was taken
on a host at load 50-177 and the 128bad7 column at load ~30, and the
differences between the two columns are smaller than the swing this file
measured between two bursts of the SAME configuration. Read them as one
band, roughly 1.8-2.2 GB/s for UCX and LIBFABRIC and 0.9-1.0 for MORI_IO,
not as three dated points on a trend.

**THIS HOST IS SHARED, AND THE NUMBERS MOVE WITH ITS LOAD.** It has 32
threads and several hundred logged-in users, and during the runs above its
load average went from 50 to 177 -- other people's jobs, not ours. The
consequence is specific and worth knowing before trusting any figure here:

* Within one burst of eight back-to-back runs the spread is small: 1.03x
  for UCX, 1.13x for LIBFABRIC, 1.36x for MORI_IO.
* BETWEEN bursts minutes apart it is not. The same UCX VRAM configuration
  that medians 2.103 here read 1.591 and 1.796 in a two-run sample taken
  twenty minutes earlier, when the host was busier. That is a 1.5x swing
  from nothing but neighbours.

An earlier revision of this file reported a 15-33% fleet-wide regression
across all three transports, attributed to the pin bump. That was wrong, and
it was wrong because it rested on two runs per configuration. Take eight in
one burst, report the median, and note the load; a two-run sample on this
machine can say almost anything.

`cluster/scripts/bench.sh` does exactly that, so the rule does not have to
be remembered:

```
cluster/scripts/bench.sh UCX VRAM
RUNS=16 BLOCK=262144 cluster/scripts/bench.sh LIBFABRIC DRAM
```

It runs eight by default, prints every run, reports min/median/max and the
load either side, and says so when the spread passes 1.25x. It also carries
the two flags that are mandatory and not default -- `--enable_pt` for
LIBFABRIC and `--mori_host` for MORI_IO -- so a measurement cannot
accidentally be taken without them.

For reference, raw RDMA underneath all of this is 5.73 GB/s at 1 MiB
(`ib_send_bw -s 1048576 -n 2000`, vm3 <-> vm4, rail 1), measured the same
way. It is recorded here so the next pin bump has a fabric baseline to move
against rather than only end-to-end numbers, which is what would separate a
real regression from a loaded afternoon.

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
