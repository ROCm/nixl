# rdma-core: the ionic provider the guests need

Unlike `patches/nixl`, `patches/ucx` and `patches/hipfile`, nothing here is
applied during the image build. rdma-core lives in the **guest**, not in the
image: the NIXL stack bundles no rdma-core of its own, so its libfabric and UCX
resolve providers through the guest's `/usr/lib/x86_64-linux-gnu/libibverbs/`.
This directory is therefore built and installed separately, onto the cluster
guests.

## What it fixes

`01-ionic-honour-sq-sig-all.patch` makes the ionic provider honour
`sq_sig_all`. libfabric's verbs provider never sets `IBV_SEND_SIGNALED` and
relies entirely on queue pairs created with `sq_sig_all = 1` for its
completions. v61.0's ionic provider accepts that flag, stores it nowhere, and
consults it never, so the device completes the send and the provider discards
the completion in `ionic_poll_send`.

The visible effect is a hard cutoff at the inject limit rather than an error:
payloads of 255 bytes and under go through `fi_inject`, need no completion, and
work forever; at 256 bytes `rxm_emulate_inject` takes a tx-pool buffer it can
only return on a completion that never arrives, and every send after that is
`-FI_EAGAIN` with nothing logging it.

This is not specific to the emulated device -- any libfabric-over-ionic
deployment has it. It is fixed upstream after v61.0 (present at
`bd3282a1cf1025c7869a8664c2d54266a4dded44`); this is that change backported.

## Why v61.0 and not upstream

libibverbs loads providers by a private ABI version. The guests run rdma-core
61.0, whose libibverbs looks for `libionic-rdmav59.so`. Building upstream
master produces `libionic-rdmav65.so`, which it will not load at all -- so the
newer tree is not a shortcut, even though the fix is in it.

## Building and installing

```sh
mkdir -p /tmp/out
docker run --rm -v "$PWD/patches/rdma-core:/patches:ro" -v /tmp/out:/out \
    ubuntu:26.04 bash /patches/build-ionic-provider.sh
```

Then on each guest, keeping the original alongside:

```sh
T=/usr/lib/x86_64-linux-gnu/libionic.so.1.0.61.0
[ -f "$T.orig" ] || sudo cp -a "$T" "$T.orig"
sudo install -m 0644 libionic.so.1.0.61.0 "$T"
```

The stock provider is 55688 bytes and the patched one about 67416, which is the
quickest way to tell which is installed.

## It is necessary, not sufficient

LIBFABRIC still does not work on this cluster with this in place. RDMA CM does
not work on the published rocm-ernic -- `rping` hangs in both directions -- and
libfabric's `verbs;ofi_rxm` establishes its connections through RDMA CM. See
`cluster/README.md`.

## SRQ is a separate matter

qemu-minimal carries a six-patch SRQ series for this same provider
(`qemu/docker/lmcache/rdma-core-patches/`), which would let UCX use `rc_verbs`
instead of `ud_verbs`. It is deliberately not vendored here: it is written
against the post-v61 master tree, so it does not apply to the v61.0 the guests'
ABI requires, and the published ERNIC device reports `max_srq: 0` regardless,
so the provider half alone would change nothing.
