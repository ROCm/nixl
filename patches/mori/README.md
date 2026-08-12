# MORI patchset

Applied to `ROCm/mori` at `MORI_REF` (see the `Makefile`), in lexical filename
order. See [../README.md](../README.md) for the rules.

Currently empty — the pinned tag is built pristine.

## Notes specific to MORI

- Patches are applied **after** submodules are initialised
  (`3rdparty/msgpack-c`, `3rdparty/spdlog`), so a patch may touch files that
  reference them. The `3rdparty/spdk` submodule is deliberately not checked out
  (it is huge, and only needed for `BUILD_UMBP_SPDK=ON`, which drives its own
  selective checkout via `tools/setup_spdk.sh`).
- MORI versions itself with `setuptools_scm`. Applying a patch leaves the tree
  dirty, which would otherwise stamp the wheel `1.2.2+d<date>`; the build pins
  `SETUPTOOLS_SCM_PRETEND_VERSION` to the tag instead. If a patch is supposed
  to change the reported version, set `MORI_VERSION` explicitly.
- Upstream keeps its own patch in-tree at `docker/rocr-mapped-handle-fix.patch`
  for one of its dev images. It is *not* applied here — copy it in as
  `01-rocr-mapped-handle-fix.patch` if the target ROCm build needs it.
