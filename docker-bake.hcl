// buildx bake definition for nixl-mori-test.
//
// The Makefile still drives the ordinary build; this exists because there are
// now two images from one Dockerfile that share every build stage, and bake
// builds them in one graph so ucx, libfabric, mori and nixl are each compiled
// once rather than once per target.
//
//   docker buildx bake                 # both images
//   docker buildx bake slim            # just the slim runtime
//   docker buildx bake --print         # resolved config, builds nothing
//
// The pins come from the Makefile; override on the command line or in the
// environment to match a `make build` that used different ones, e.g.
//   NIXL_REF=v1.5.0 docker buildx bake slim

variable "ROCM_VERSION"   { default = "7.14.0" }
variable "ROCM_BASE_IMAGE" { default = "" }
variable "NIXL_REF"       { default = "v1.5.2" }
variable "MORI_REF"       { default = "v1.2.4" }
variable "UCX_REF"        { default = "v1.22.0" }
variable "LIBFABRIC_REF"  { default = "v2.7.0" }
variable "HIPFILE_REF"    { default = "bd0bc2330a1573223209b9899055d1d6188b9113" }
variable "FIO_REF"        { default = "c32261752c88b3f7aadfde4d011a028ae954f869" }
// A branch head on sbates130272/perftest: the ROCm data-validation work is
// not upstream. A SHA so a push to the fork does not change this image.
variable "PERFTEST_REF"   { default = "18f7a8b0ad19107ce7d235916c8efa179990e0dd" }
variable "VERSION"        { default = "" }
variable "IMAGE_NAME"     { default = "nixl-mori-test" }
variable "IMAGE_TAG"      { default = "latest" }

// Base for the slim runtime. See the runtime-slim stage: override both this
// and ROCM_BASE_IMAGE with explicit registry hosts when a site mirror proxies
// docker.io, since the mirror fetches upstream under its own quota.
variable "RUNTIME_BASE_IMAGE" { default = "ubuntu:24.04" }

// INSTALL_TORCH is 1 in the full image because nixl._api imports torch at
// module scope. The slim runtime sets it to 0: it is a multi-GB download that
// only nixl._api and MORI's Python benchmarks use, and neither is the point of
// a runtime image.
variable "INSTALL_TORCH"  { default = "1" }

group "default" {
  targets = ["full", "slim"]
}

target "_common" {
  context    = "."
  dockerfile = "docker/Dockerfile"
  args = {
    ROCM_VERSION     = ROCM_VERSION
    // Empty would be passed through as an empty build-arg and override the
    // Dockerfile's own default, leaving `FROM :`. The Makefile omits the arg
    // instead; bake has no way to omit one, so resolve it here.
    ROCM_BASE_IMAGE  = ROCM_BASE_IMAGE != "" ? ROCM_BASE_IMAGE : "rocm/dev-ubuntu-24.04:${ROCM_VERSION}-full"
    NIXL_REF         = NIXL_REF
    MORI_REF         = MORI_REF
    UCX_REF          = UCX_REF
    LIBFABRIC_REF    = LIBFABRIC_REF
    HIPFILE_REF      = HIPFILE_REF
    FIO_REF          = FIO_REF
    PERFTEST_REF     = PERFTEST_REF
    VERSION          = VERSION
    RUNTIME_BASE_IMAGE = RUNTIME_BASE_IMAGE
  }
}

target "full" {
  inherits = ["_common"]
  target   = "runtime"
  tags     = ["${IMAGE_NAME}:${IMAGE_TAG}"]
  args = {
    INSTALL_TORCH = INSTALL_TORCH
  }
}

// Same artifacts, on a base that was never a build environment. See the
// runtime-slim stage in docker/Dockerfile for what this gives up -- chiefly
// MORI's hipcc JIT, since the compiler is exactly what is left behind.
target "slim" {
  inherits = ["_common"]
  target   = "runtime-slim"
  tags     = ["${IMAGE_NAME}:${IMAGE_TAG}-slim"]
  args = {
    INSTALL_TORCH = "0"
  }
}

// The MORI wheel on its own, for `--output type=local,dest=dist`.
target "wheels" {
  inherits = ["_common"]
  target   = "wheels"
  output   = ["type=local,dest=dist"]
}

// ---- rocm-ernic -------------------------------------------------------------
// Not part of `default`: this builds a device server for the test cluster, not
// a NIXL image, and `docker buildx bake` should not drag it in. See
// docker/rocm-ernic/Dockerfile for why it exists.
//
// ERNIC_BASE_IMAGE must track ERNIC_IMAGE in cluster/env/nixl-mori: the whole
// point is to inherit that image's libvfio-user, so the server still pairs
// with the QEMU the stack runs.
// One line, like every other variable here: the Makefile and pins-check both
// read this file with a single-line regex, and a multi-line declaration reads
// as absent rather than as an error.
variable "ERNIC_BASE_IMAGE" { default = "docker.io/sbates130272/batesste-ci-images-ubuntu-rocm-ernic:20261021.g742e8e2-ernic.31d3114-vfu.8039244" }

// A SHA, not a branch. Contains daa7d94 (ROCm/rocm-ernic#315), which is what
// makes guest-to-guest rdma_cm work and therefore what makes NIXL's LIBFABRIC
// backend work at all on this cluster.
variable "ERNIC_REF" { default = "051ff6d540e095021553fe59913e0c4039ee0972" }

variable "ERNIC_IMAGE_NAME" { default = "nixl-mori-ernic" }

target "ernic" {
  context    = "docker/rocm-ernic"
  dockerfile = "Dockerfile"
  tags       = ["${ERNIC_IMAGE_NAME}:${substr(ERNIC_REF, 0, 7)}"]
  args = {
    ERNIC_BASE_IMAGE = ERNIC_BASE_IMAGE
    ERNIC_REF        = ERNIC_REF
  }
}
