# rdma-core: the ionic provider lives in rocm-ernic, not here

This directory used to build and install a patched ionic provider onto the
cluster guests. It no longer does, because the premise it was built on is
false. It is kept as a pointer, since the false premise is an easy one to
arrive at twice.

## The provider the guests actually load

`rocm-ernic`, role `ernic_srq_provider`, series
`ansible/files/patches/rdma-core/`. It builds upstream rdma-core pinned at
`bd3282a1cf1025c7869a8664c2d54266a4dded44` — a master SHA declaring version
66.0, not a release — plus 20 patches, and installs the whole tree to
**`/usr/local`**:

| Patches | What |
| --- | --- |
| 0001–0006 | ionic SRQ support (upstream [#1799][srqpr] + two local fixes) |
| 0007–0020 | ionic direct verbs / GDA (upstream [#1771][dvpr], rebased) |

To rebuild and reinstall it on the guests:

```sh
cd ~/Projects/rocm-ernic/ansible
ansible-playbook -i ../../nixl-mori-test/ansible/inventory.py \
    playbooks/srq-dkms-build.yml --tags srq-provider -e srq_dkms_hosts=cluster
```

`ansible/guest-setup.yml` here only asserts the result; it does not build it.

## What was wrong before

**The guests do not load the distro rdma-core.** They run 66.0 from
`/usr/local`, which wins the `ld.so` search. This directory patched
`/usr/lib/x86_64-linux-gnu/libionic.so.1.0.61.0`, in a 61.0 stack that nothing
ever `dlopen`s — so it looked installed and did nothing. Everything the old
README said about ABI version `rdmav59` and "why v61.0 and not upstream"
followed from that mistake.

Two further traps, both of which cost a rebuild here:

- **The SONAME link is not the one that matters.** libibverbs `dlopen`s
  `lib/libibverbs/libionic-rdmav<ABI>.so` by private ABI version. Repointing
  `libionic.so.1` alone changes nothing that loads.
- **`ldconfig` re-elects `libionic.so.1`** from *any* file in a scanned
  directory carrying that SONAME, so a `.orig` backup left beside the
  installed provider is not inert — it gets chosen, silently undoing the
  install on the next `ldconfig` run.

## Do not build the provider on a different base

A provider carrying the DV series but not the SRQ series loads, brings both
ports up, exports all 20 DV symbols, benchmarks LIBFABRIC at full speed, and
takes UCX out fleet-wide with `ibv_create_srq() failed: Operation not
supported` — while `ibv_devinfo -v` reports `max_srq: 256`. LIBFABRIC does not
use SRQ, so it keeps working and hides it. The rocm-ernic README documents this
at length; the short version is that both halves ship together or neither does.

## `sq_sig_all` (the old `01-` patch) is obsolete

The v61.0 ionic provider accepted `sq_sig_all`, stored it nowhere and consulted
it never, so libfabric — which never sets `IBV_SEND_SIGNALED` and relies
entirely on that flag — lost every completion over 255 bytes to
`ionic_poll_send`. The backport is deleted because the fix is **already in the
base** the guests now run: `v65.0` onward honours it natively, and applying the
old patch there fails with `duplicate member 'sig_all'`.

[srqpr]: https://github.com/linux-rdma/rdma-core/pull/1799
[dvpr]: https://github.com/linux-rdma/rdma-core/pull/1771
