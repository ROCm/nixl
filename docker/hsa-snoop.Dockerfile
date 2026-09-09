# Copyright (c) Advanced Micro Devices, Inc. All rights reserved.
#
# SPDX-License-Identifier: MIT
#
# hsa-snoop as a sidecar, from the published release rather than from source.
#
# The main runtime image already carries an hsa-snoop built from `main` by
# build-hsa-snoop.sh, and that one stays: it is what HSA_SNOOP=1 starts inside
# run-nixlbench for a single paired run.  This image is for the other shape of
# measurement -- one long-lived collector watching a whole sweep of short-lived
# benchmark containers.  A sidecar is the only way to do that, because a
# collector started inside each benchmark container sees only its own run and
# dies with it, and the sweep is forty-odd runs.
#
# Pinned to a release tag, not `main`, for the same reason the other components
# are pinned: a number in a report has to be attributable to a specific build.
# v1.1.0 is also the first release with --ais-snoop, which is the whole point
# here -- it reports per-GPU and per-PCIe-device direct-storage throughput,
# IOPS and latency, which is exactly the layer between "nixlbench says 56 GB/s"
# and "the drives did something".
#
# Ubuntu rather than the ROCm base image: hsa-snoop works through kprobes and
# bpftrace against the host kernel, not through the HIP runtime, so it needs
# none of ROCm's 20 GB.  It does need to see the host's kernel -- hence
# privileged, pid=host and the tracing mounts in the compose file, not here.
FROM ubuntu:24.04

ARG HSA_SNOOP_VERSION=1.1.0
ARG HSA_SNOOP_REPO=sbates130272/hsa-snoop

# bpftrace is a hard dependency of the package.  systemd is the other one, and
# is deliberately not installed: the package ships a unit file for running as a
# service on a host, which is irrelevant in a container where the binary is
# PID 1.  --force-depends is what lets the package install without it.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
	--mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
	apt-get update && \
	apt-get install -y --no-install-recommends \
	bpftrace \
	ca-certificates \
	curl \
	linux-tools-common \
	&& rm -rf /var/lib/apt/lists/*

# The postinst calls systemctl to enable the service unit, and there is no
# systemd here, so dpkg exits non-zero after having correctly unpacked the
# binary.  Tolerate that specific outcome -- but only that one: assert the
# binary landed rather than wrapping the whole chain in `|| true`, which would
# equally hide a truncated download or a wrong-architecture package.  Note
# there is no --version to call; the flag does not exist in 1.1.0, and the
# --ais-snoop check below is what actually proves the binary runs.
RUN curl -fsSL -o /tmp/hsa-snoop.deb \
	"https://github.com/${HSA_SNOOP_REPO}/releases/download/v${HSA_SNOOP_VERSION}/hsa-snoop_${HSA_SNOOP_VERSION}_amd64.deb" && \
	{ dpkg --force-depends -i /tmp/hsa-snoop.deb || true; } && \
	rm -f /tmp/hsa-snoop.deb && \
	test -x /usr/local/bin/hsa-snoop

# Fail the build rather than ship a sidecar that cannot do the one thing it is
# here for.  An hsa-snoop without --ais-snoop would start, serve /metrics, and
# publish nothing about the storage path -- a silent hole in the report.
RUN hsa-snoop --help 2>&1 | grep -q -- '--ais-snoop' || \
	{ echo "ERROR: hsa-snoop ${HSA_SNOOP_VERSION} has no --ais-snoop" >&2; exit 1; }

EXPOSE 9488

ENTRYPOINT ["hsa-snoop"]
CMD ["--all", "--ais-snoop", "--prometheus", "--prometheus-port", "9488"]
