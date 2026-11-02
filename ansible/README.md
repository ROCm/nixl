# Guest and image modifications

Everything this repo changes outside its own image, in one place: what was
modified, why, and whether it is automated.

```
make cluster-ansible                       # all of it
make cluster-ansible ANSIBLE_TAGS=gpu      # just the GPU, needed once per boot
make cluster-ansible ANSIBLE_ARGS=--limit=vm1
```

`make cluster-prep` and `make cluster-gpu` are shorthands for the `prep` and
`gpu` tag sets. The playbook is idempotent; a second run reports no changes.

The inventory is generated from `cluster/env/nixl-mori` by `inventory.py`, so
`VM_COUNT`, `VM_SSH_PORT_BASE` and `ERNIC_GUEST_SUBNET` are read from the same
file the compose stack is rendered from rather than duplicated.

## Guest modifications — automated here

| What | Where | Tag | Without it |
|---|---|---|---|
| `memlock` unlimited | `/etc/security/limits.d/90-rdma-memlock.conf` | `prep` | Every `ibv_reg_mr` fails; NIXL reports "registration failed for the specified or all potential backends", which reads like a missing plugin |
| Fabric address | `/etc/netplan/60-ernic.yaml` | `prep` | Nothing assigns it; set by hand it lasts until the next reboot |
| ionic queue count | `ethtool -L combined 1` + driver rebind | `prep` | Only when the link is down. `ip link set up` answers "No space left on device" after `qcq alloc of rx0 -28` |
| Patched ionic provider | `/usr/lib/x86_64-linux-gnu/libionic.so.1.0.61.0` | `rdma` | libfabric gets no send completions above the 255-byte inject limit and stalls with nothing logged. See `patches/rdma-core/` |
| `ip_discovery.bin` | `/lib/firmware/amdgpu/` | `gpu` | `amdgpu` probes `-2`, no KFD node, no GPU |
| `amdgpu-probe` | runtime, once per boot | `gpu` | No GPU, so MORI_IO calls `exit(-1)` before any transport starts |

`optional: true` in the netplan config is load-bearing on its own: without it
`systemd-networkd-wait-online` blocks the boot while the link is down, which is
indefinite if the NIC cannot allocate queues — so a link fault presents as a
guest you cannot log in to in order to diagnose it.

## Guest modifications — elsewhere, deliberately

`cluster.sh stack` installs the NIXL stack: the `/opt` tarball exported from
the slim image, `libgflags2.2` and `libhwloc15` from apt, an `ld.so.conf.d`
entry, and `FI_UNIVERSE_SIZE=16` / `UCX_ROCM_COPY_DMABUF=try` written to
`/etc/environment`. It stays a shell script because it is driven by `docker
export` on the controller, not by configuring a guest.

Note which file those two variables go in. `profile.d` is sourced only by a
login shell and `environment.d` only by a systemd user session, and every
benchmark here runs as `ssh guest 'nixlbench ...'`, which is neither.
`/etc/environment` is read by `pam_env` on any PAM session. All three are
written; only the last one actually reaches a run.

`cluster.sh nvme` formats and mounts the local NVMe, unchanged.

## Not a guest change: a hand edit that was reverted

`CONFIG_DMABUF_MOVE_NOTIFY=y` was appended to `/boot/config-$(uname -r)` to
prove that UCX's dmabuf probe was the only thing standing between us and VRAM
over the fabric. It was, and the edit has been removed from every guest:
`patches/ucx/03-rocm-dmabuf-kallsyms-fallback.patch` fixes the probe properly,
and VRAM measures the same with the patch and without the edit (0.643 vs
0.645 GB/s at 256 KiB). Nothing should put that line back.

## Container image and compose changes

These are in `cluster/env/nixl-mori`, with the full reasoning on each, and
`make cluster-images` turns the `VM_BACKING_IMAGE` pin back into guest disks
and the fleet key. Checked 2026-11-02: nothing newer than `20261030.gadc94af`
is published for any of the four, so the guest image is current and the three
held at `20261021` are held for the reason given, not for lack of a newer
build.

| Image | Moved to | Why |
|---|---|---|
| `VM_BACKING_IMAGE` | `20261030` `ernic-rocjitsu` | The `ionic` flavour has `"dkms": []` and no ROCm, so its in-tree `amdgpu` cannot bind the emulated gfx1250 |
| `QEMU_IMAGE` | sbates fork, `qemu.aa4344e` | Stock QEMU cannot do vfio-user P2P DMA, so an RDMA write into VRAM has no BAR window to target |
| `ERNIC_IMAGE`, `ROCJITSU_IMAGE` | held at `20261021` | The `20261030` ernic binds its mesh port on tcp6 only and `gen-compose`'s healthcheck greps `/proc/net/tcp` for a v4 LISTEN, so nothing starts |
| `IMAGE_ALLOW_GUEST_LAG` | `true` | Required to run a guest newer than the device servers |
| `ERNIC_GUEST_SUBNET` | `192.168.80` | Must fall inside the subnet docker assigned the stack's `l2` network, which changed |

Two further operational points that are not settings:

* **Restarting `qemu-N` requires restarting `rocjitsu-N` first**, with its
  socket removed. rocjitsu stops serving when its client disconnects ("this
  device shares memory by descriptor, which cannot be reclaimed, so serving
  ends here") and cannot rebind a stale socket. The symptom is QEMU exiting
  with `vfio-user ... timed out waiting for reply`.
* **Root-owned NVMe scratch images block boot.** Left over from when the
  containers ran as root; QEMU opens every drive read-write and refuses. Delete
  them, they are recreated.

## Host-side, outside this repo

`~/Projects/qemu-minimal` was fast-forwarded 67 commits to `origin/main`; it
was behind `ae551b3`, which is the commit the `20261030` guest image was built
at. `qemu-tool` was reinstalled from that tree into `/tmp/qt`.
