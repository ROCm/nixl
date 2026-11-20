#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Record what the machine it runs on offers a nixlbench run, as one JSON object.

    amd_bench_probe_guest.py [--only NAME[,NAME...]] [--json-out FILE]
                             [--summary-out FILE]

Run by .github/workflows/amd-bench-probe.yml in the rocjitsu guest, and with
"--only io_uring" in a job container. The probes are: system, limits, disk,
modules, tools, packages, libraries, rocm, nvme, block, rdma, network, apt and
io_uring.

The only thing that changes the machine is the apt probe, which times
"sudo -n apt-get update". Every probe is best effort: one that cannot answer
records why under "errors", and the exit status is 0 unless the arguments are
wrong or an output file cannot be written.

Standard library only.
"""

from __future__ import annotations

import argparse
import ctypes
import datetime
import errno
import glob
import json
import os
import re
import resource
import shutil
import subprocess
import sys
import time
from typing import Any, Callable

SCHEMA = 1

MODULES = ("nvme-fabrics", "nvme-rdma", "nvme-tcp", "ionic", "ionic_rdma")

# name -> the command that prints its version, or None when it has none.
TOOLS: dict[str, list[str] | None] = {
    "ais-stats": None,
    "nvme": ["nvme", "version"],
    "ibv_devinfo": None,
    "ibv_rc_pingpong": None,
    "rping": None,
    "ib_write_bw": ["ib_write_bw", "--version"],
    "rdma": ["rdma", "-V"],
    "etcd": ["etcd", "--version"],
    "etcdctl": ["etcdctl", "version"],
    "redis-server": ["redis-server", "--version"],
    "curl": ["curl", "--version"],
    "aws": ["aws", "--version"],
    "cc": ["cc", "--version"],
    "fio": ["fio", "--version"],
}

PACKAGES = (
    "nvme-cli",
    "rdma-core",
    "ibverbs-utils",
    "rdmacm-utils",
    "libibverbs-dev",
    "librdmacm-dev",
    "perftest",
    "etcd-server",
    "etcd-client",
    "redis-server",
    "libgflags2.2",
    "libhwloc15",
    "libcurl4t64",
    "liburing2",
    "liburing-dev",
    "libfabric1",
    "libaio1t64",
    "build-essential",
)

LIBRARIES = (
    "libgflags",
    "libhwloc",
    "libcurl",
    "liburing",
    "libfabric",
    "libibverbs",
    "librdmacm",
    "libaio",
    "libnuma",
)

ROCM_VERSION_FILES = (
    "/opt/rocm/.info/version",
    "/opt/rocm/core/.info/version",
    "/opt/rocm/core-*/.info/version",
)

# io_uring_setup has this number on every architecture.
NR_IO_URING_SETUP = 425
# sizeof(struct io_uring_params), with its features word at this offset.
IO_URING_PARAMS_SIZE = 120
IO_URING_FEATURES_OFFSET = 20

Facts = dict[str, Any]


def read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except OSError:
        return None


def run(cmd: list[str], timeout: float = 30) -> Facts:
    """Run cmd; "rc" is None when it could not be run or did not finish."""
    try:
        done = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError:
        return {"rc": None, "out": "", "err": f"{cmd[0]}: not found"}
    except subprocess.TimeoutExpired:
        return {"rc": None, "out": "", "err": f"timed out after {timeout:g} s"}
    except OSError as e:
        return {"rc": None, "out": "", "err": e.strerror or str(e)}
    return {
        "rc": done.returncode,
        "out": done.stdout.decode("utf-8", "replace").strip(),
        "err": done.stderr.decode("utf-8", "replace").strip(),
    }


def first_line(text: str) -> str:
    return text.splitlines()[0].strip() if text.strip() else ""


def probe_system() -> Facts:
    uname = os.uname()
    meminfo: dict[str, int] = {}
    for line in (read("/proc/meminfo") or "").splitlines():
        match = re.match(r"(\w+):\s+(\d+)", line)
        if match:
            meminfo[match.group(1)] = int(match.group(2))
    model = ""
    for line in (read("/proc/cpuinfo") or "").splitlines():
        if line.startswith("model name"):
            model = line.split(":", 1)[1].strip()
            break
    os_release = ""
    for line in (read("/etc/os-release") or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            os_release = line.split("=", 1)[1].strip().strip('"')
    return {
        "hostname": uname.nodename,
        "kernel_release": uname.release,
        "kernel_version": uname.version,
        "machine": uname.machine,
        "os_release": os_release,
        "vcpus": os.cpu_count(),
        "vcpus_usable": len(os.sched_getaffinity(0)),
        "cpu_model": model,
        "mem_total_kb": meminfo.get("MemTotal"),
        "mem_available_kb": meminfo.get("MemAvailable"),
        "swap_total_kb": meminfo.get("SwapTotal"),
        "hugepages_total": meminfo.get("HugePages_Total"),
        "loadavg": read("/proc/loadavg"),
        "user": {"uid": os.getuid(), "groups": sorted(os.getgroups())},
    }


def _rlimit(value: int, unit: int) -> int | str:
    return "unlimited" if value == resource.RLIM_INFINITY else value // unit


def probe_limits() -> Facts:
    soft, hard = resource.getrlimit(resource.RLIMIT_MEMLOCK)
    nofile = resource.getrlimit(resource.RLIMIT_NOFILE)
    facts: Facts = {
        # In KiB, which is what "ulimit -l" prints.
        "memlock_soft_kb": _rlimit(soft, 1024),
        "memlock_hard_kb": _rlimit(hard, 1024),
        "nofile_soft": _rlimit(nofile[0], 1),
        "nofile_hard": _rlimit(nofile[1], 1),
    }
    # What a process started through sudo gets, which is how a run would raise
    # the limit without a limits.d entry.
    sudo = run(["sudo", "-n", "bash", "-c", "ulimit -S -l; ulimit -H -l"])
    if sudo["rc"] == 0 and len(sudo["out"].split()) == 2:
        facts["sudo_memlock_soft_kb"], facts["sudo_memlock_hard_kb"] = sudo[
            "out"
        ].split()
    else:
        facts["sudo_error"] = sudo["err"] or f"exit status {sudo['rc']}"
    return facts


def _statvfs(path: str) -> Facts:
    st = os.statvfs(path)
    return {
        "bytes_total": st.f_blocks * st.f_frsize,
        "bytes_free": st.f_bfree * st.f_frsize,
        "bytes_avail": st.f_bavail * st.f_frsize,
    }


def probe_disk() -> Facts:
    mounts: list[Facts] = []
    seen: set[str] = set()
    for line in (read("/proc/mounts") or "").splitlines():
        fields = line.split()
        if len(fields) < 3:
            continue
        source, target, fstype = fields[0], fields[1], fields[2]
        if target != "/" and not source.startswith("/dev/"):
            continue
        if target in seen:
            continue
        seen.add(target)
        entry: Facts = {
            "source": source,
            # /proc/mounts writes a space in a path as \040.
            "target": target.replace("\\040", " "),
            "fstype": fstype,
            "nvme": os.path.basename(os.path.realpath(source)).startswith("nvme"),
        }
        try:
            entry.update(_statvfs(entry["target"]))
        except OSError as e:
            entry["error"] = e.strerror or str(e)
        mounts.append(entry)
    return {"mounts": mounts}


def probe_modules() -> Facts:
    modules: Facts = {}
    for name in MODULES:
        info = run(["modinfo", "-n", name])
        present = info["rc"] == 0 and bool(info["out"])
        entry: Facts = {
            "present": present,
            "loaded": os.path.isdir("/sys/module/" + name.replace("-", "_")),
        }
        if present:
            # A path, or "(builtin)".
            entry["path"] = first_line(info["out"])
        else:
            entry["error"] = first_line(info["err"]) or f"exit status {info['rc']}"
        modules[name] = entry
    return modules


def probe_tools() -> Facts:
    tools: Facts = {}
    for name, version_cmd in TOOLS.items():
        path = shutil.which(name)
        entry: Facts = {"present": path is not None, "path": path}
        if path is not None and version_cmd is not None:
            version = run(version_cmd)
            entry["version"] = first_line(version["out"]) or first_line(version["err"])
        tools[name] = entry
    # ais-stats ships with hipFile, which need not be on PATH.
    if not tools["ais-stats"]["present"]:
        found = sorted(glob.glob("/opt/rocm*/**/bin/ais-stats", recursive=True))
        if found:
            tools["ais-stats"] = {"present": True, "path": found[0]}
    return tools


def probe_packages() -> Facts:
    query = run(
        ["dpkg-query", "-W", "-f", "${Package}\\t${Version}\\t${db:Status-Abbrev}\\n"]
        + list(PACKAGES)
    )
    packages: Facts = {name: {"installed": False} for name in PACKAGES}
    if query["rc"] is None:
        return {"error": query["err"]}
    for line in query["out"].splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[0] in packages:
            packages[fields[0]] = {
                "installed": fields[2].strip().startswith("ii"),
                "version": fields[1],
            }
    return packages


def probe_libraries() -> Facts:
    cache = run(["ldconfig", "-p"])
    if cache["rc"] != 0:
        cache = run(["/sbin/ldconfig", "-p"])
    if cache["rc"] != 0:
        return {"error": cache["err"] or f"exit status {cache['rc']}"}
    libraries: Facts = {name: [] for name in LIBRARIES}
    for line in cache["out"].splitlines():
        match = re.match(r"\s*(\S+)\s+\(([^)]*)\)\s+=>\s+(\S+)", line)
        if not match:
            continue
        soname = match.group(1)
        for name in LIBRARIES:
            if soname.startswith(name + ".so") or soname.startswith(name + "-"):
                libraries[name].append(
                    {"soname": soname, "abi": match.group(2), "path": match.group(3)}
                )
    return libraries


def probe_rocm() -> Facts:
    versions: Facts = {}
    for pattern in ROCM_VERSION_FILES:
        for path in sorted(glob.glob(pattern)):
            text = read(path)
            if text:
                versions[path] = first_line(text)
    guest: Any = None
    text = read("/etc/rocjitsu-guest.json")
    if text is not None:
        try:
            guest = json.loads(text)
        except ValueError:
            guest = text
    return {
        "version": next(iter(versions.values()), None),
        "version_files": versions,
        "rocm_path": os.environ.get("ROCM_PATH"),
        "opt": sorted(glob.glob("/opt/rocm*")),
        "rocjitsu_guest": guest,
    }


def _nvme_namespace(ctrl: str, entry: str) -> Facts:
    # With native multipath the controller holds nvmeXcYnZ and the block
    # device is nvmeXnZ; without it they are the same name.
    block = re.sub(r"c\d+n", "n", entry)
    sysfs = f"/sys/block/{block}"
    sectors = read(f"{sysfs}/size")
    generic = "/dev/ng" + block[len("nvme") :]
    return {
        "name": entry,
        "nsid": read(f"{ctrl}/{entry}/nsid"),
        "block": block if os.path.isdir(sysfs) else None,
        "block_node": os.path.exists(f"/dev/{block}"),
        # 1 for a namespace Linux attaches with no block device, such as a
        # Key Value one.
        "hidden": read(f"{sysfs}/hidden"),
        "logical_block_size": read(f"{sysfs}/queue/logical_block_size"),
        "physical_block_size": read(f"{sysfs}/queue/physical_block_size"),
        "bytes": int(sectors) * 512 if sectors and sectors.isdigit() else None,
        "generic_node": generic if os.path.exists(generic) else None,
    }


def probe_nvme() -> Facts:
    controllers: list[Facts] = []
    for ctrl in sorted(glob.glob("/sys/class/nvme/nvme*")):
        name = os.path.basename(ctrl)
        namespaces = [
            _nvme_namespace(ctrl, entry)
            for entry in sorted(os.listdir(ctrl))
            if re.fullmatch(r"nvme\d+(c\d+)?n\d+", entry)
        ]
        controllers.append(
            {
                "name": name,
                "transport": read(f"{ctrl}/transport"),
                "model": read(f"{ctrl}/model"),
                "serial": read(f"{ctrl}/serial"),
                "firmware_rev": read(f"{ctrl}/firmware_rev"),
                "subsysnqn": read(f"{ctrl}/subsysnqn"),
                "address": read(f"{ctrl}/address"),
                "state": read(f"{ctrl}/state"),
                "namespaces": namespaces,
            }
        )
    return {
        "controllers": controllers,
        "multipath": read("/sys/module/nvme_core/parameters/multipath"),
        "generic_nodes": sorted(glob.glob("/dev/ng*n*")),
    }


def probe_block() -> Facts:
    columns = "NAME,TYPE,SIZE,FSTYPE,MOUNTPOINTS,TRAN,MODEL,SERIAL,LOG-SEC,PHY-SEC"
    lsblk = run(["lsblk", "-J", "-b", "-o", columns])
    if lsblk["rc"] != 0:
        return {"error": lsblk["err"] or f"exit status {lsblk['rc']}"}
    try:
        return {"lsblk": json.loads(lsblk["out"]).get("blockdevices", [])}
    except ValueError:
        return {"error": "lsblk did not print JSON", "text": lsblk["out"]}


def probe_rdma() -> Facts:
    devices: list[Facts] = []
    for dev in sorted(glob.glob("/sys/class/infiniband/*")):
        ports: Facts = {}
        for port in sorted(glob.glob(f"{dev}/ports/*")):
            gids = []
            for path in sorted(glob.glob(f"{port}/gids/*"), key=_trailing_number):
                gid = read(path)
                if gid and gid.strip("0:"):
                    index = os.path.basename(path)
                    gids.append(
                        {
                            "index": int(index),
                            "gid": gid,
                            "type": read(f"{port}/gid_attrs/types/{index}"),
                        }
                    )
            ports[os.path.basename(port)] = {
                "state": read(f"{port}/state"),
                "link_layer": read(f"{port}/link_layer"),
                "rate": read(f"{port}/rate"),
                "gids": gids,
            }
        netdevs: list[str] = []
        if os.path.isdir(f"{dev}/device/net"):
            netdevs = sorted(os.listdir(f"{dev}/device/net"))
        devices.append(
            {
                "name": os.path.basename(dev),
                "node_type": read(f"{dev}/node_type"),
                "fw_ver": read(f"{dev}/fw_ver"),
                "driver": _link_name(f"{dev}/device/driver"),
                "netdevs": netdevs,
                "ports": ports,
            }
        )
    facts: Facts = {"devices": devices}
    devinfo = run(["ibv_devinfo"])
    if devinfo["rc"] is not None:
        facts["ibv_devinfo"] = devinfo["out"] or devinfo["err"]
    return facts


def _link_name(path: str) -> str | None:
    return os.path.basename(os.path.realpath(path)) if os.path.exists(path) else None


def _trailing_number(path: str) -> int:
    base = os.path.basename(path)
    return int(base) if base.isdigit() else -1


def probe_network() -> Facts:
    addr = run(["ip", "-j", "addr"])
    if addr["rc"] != 0:
        return {"error": addr["err"] or f"exit status {addr['rc']}"}
    interfaces: list[Facts] = []
    for link in json.loads(addr["out"]):
        interfaces.append(
            {
                "name": link.get("ifname"),
                "mac": link.get("address"),
                "state": link.get("operstate"),
                "mtu": link.get("mtu"),
                "addresses": [
                    f"{a.get('local')}/{a.get('prefixlen')}"
                    for a in link.get("addr_info", [])
                ],
            }
        )
    return {"interfaces": interfaces}


def _tree_bytes(root: str) -> int:
    total = 0
    for directory, _, files in os.walk(root):
        for name in files:
            try:
                total += os.lstat(os.path.join(directory, name)).st_size
            except OSError:
                pass
    return total


def probe_apt() -> Facts:
    lists = "/var/lib/apt/lists"
    facts: Facts = {"lists_bytes_before": _tree_bytes(lists)}
    start = time.monotonic()
    update = run(
        ["sudo", "-n", "env", "DEBIAN_FRONTEND=noninteractive", "apt-get", "update"],
        timeout=600,
    )
    facts["seconds"] = round(time.monotonic() - start, 1)
    facts["rc"] = update["rc"]
    facts["ok"] = update["rc"] == 0
    facts["lists_bytes_after"] = _tree_bytes(lists)
    tail = (update["out"] + "\n" + update["err"]).strip().splitlines()[-12:]
    facts["output_tail"] = tail
    return facts


def probe_io_uring() -> Facts:
    facts: Facts = {
        # 0 allows it, 1 restricts it to a group, 2 disables it.
        "sysctl_io_uring_disabled": read("/proc/sys/kernel/io_uring_disabled"),
    }
    for line in (read("/proc/self/status") or "").splitlines():
        if line.startswith(("Seccomp:", "NoNewPrivs:")):
            key, value = line.split(":", 1)
            facts[key.lower()] = value.strip()
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    params = ctypes.create_string_buffer(IO_URING_PARAMS_SIZE)
    fd = libc.syscall(
        ctypes.c_long(NR_IO_URING_SETUP), ctypes.c_uint(4), ctypes.byref(params)
    )
    if fd >= 0:
        os.close(fd)
        features = int.from_bytes(
            params.raw[IO_URING_FEATURES_OFFSET : IO_URING_FEATURES_OFFSET + 4],
            sys.byteorder,
        )
        facts.update({"works": True, "features": f"{features:#x}"})
    else:
        code = ctypes.get_errno()
        facts.update(
            {
                "works": False,
                "errno": errno.errorcode.get(code, str(code)),
                "error": os.strerror(code),
            }
        )
    return facts


PROBES: dict[str, Callable[[], Facts]] = {
    "system": probe_system,
    "limits": probe_limits,
    "disk": probe_disk,
    "modules": probe_modules,
    "tools": probe_tools,
    "packages": probe_packages,
    "libraries": probe_libraries,
    "rocm": probe_rocm,
    "nvme": probe_nvme,
    "block": probe_block,
    "rdma": probe_rdma,
    "network": probe_network,
    "apt": probe_apt,
    "io_uring": probe_io_uring,
}


def _gib(value: Any) -> str:
    return f"{value / 2**30:.1f} GiB" if isinstance(value, int) else "unknown"


def _yes_no(value: Any) -> str:
    return "yes" if value else "no"


def summary_rows(facts: Facts) -> list[tuple[str, str]]:
    """The headline answers, in the order the probes ran."""
    rows: list[tuple[str, str]] = []
    system = facts.get("system")
    if system:
        rows.append(("Kernel", str(system["kernel_release"])))
        rows.append(("vCPUs", str(system["vcpus"])))
        mem = system["mem_total_kb"]
        rows.append(("MemTotal", _gib(mem * 1024 if isinstance(mem, int) else None)))
    limits = facts.get("limits")
    if limits:
        rows.append(
            (
                "ulimit -l (soft / hard, KiB)",
                f"{limits['memlock_soft_kb']} / {limits['memlock_hard_kb']}",
            )
        )
    for mount in facts.get("disk", {}).get("mounts", []):
        if mount["target"] == "/" or mount["nvme"]:
            rows.append(
                (
                    f"Free on {mount['target']} ({mount['source']})",
                    f"{_gib(mount.get('bytes_avail'))} of {_gib(mount.get('bytes_total'))}",
                )
            )
    modules = facts.get("modules")
    if modules:
        rows.append(
            (
                "Kernel modules",
                ", ".join(
                    f"{name}: {'present' if entry['present'] else 'absent'}"
                    for name, entry in modules.items()
                ),
            )
        )
    tools = facts.get("tools")
    if tools:
        rows.append(("ais-stats", _yes_no(tools["ais-stats"]["present"])))
        rows.append(
            (
                "Tools found",
                ", ".join(n for n, e in tools.items() if e["present"]) or "none",
            )
        )
        rows.append(
            (
                "Tools missing",
                ", ".join(n for n, e in tools.items() if not e["present"]) or "none",
            )
        )
    libraries = facts.get("libraries")
    if libraries and "error" not in libraries:
        rows.append(
            (
                "Shared libraries",
                ", ".join(
                    f"{name}: {found[0]['soname'] if found else 'absent'}"
                    for name, found in libraries.items()
                ),
            )
        )
    rocm = facts.get("rocm")
    if rocm:
        rows.append(("ROCm", str(rocm["version"] or "not found")))
    for ctrl in facts.get("nvme", {}).get("controllers", []):
        namespaces = "; ".join(
            f"nsid {ns['nsid']} {ns['block'] or ns['name']}"
            f" lba {ns['logical_block_size']} {_gib(ns['bytes'])}"
            for ns in ctrl["namespaces"]
        )
        rows.append(
            (
                f"NVMe {ctrl['name']}",
                f"{ctrl['transport']}, model {ctrl['model']}, serial {ctrl['serial']}"
                f"; {namespaces or 'no namespace'}",
            )
        )
    rdma = facts.get("rdma")
    if rdma:
        rows.append(
            (
                "RDMA devices",
                ", ".join(
                    f"{dev['name']} ({dev['driver']}, {'/'.join(dev['netdevs'])})"
                    for dev in rdma["devices"]
                )
                or "none",
            )
        )
    apt = facts.get("apt")
    if apt:
        rows.append(
            (
                "sudo apt-get update",
                f"{'ok' if apt['ok'] else 'failed'} (exit status {apt['rc']})"
                f" in {apt['seconds']} s",
            )
        )
    uring = facts.get("io_uring")
    if uring:
        answer = "works" if uring["works"] else f"fails with {uring['errno']}"
        rows.append(("io_uring_setup", answer))
    return rows


def summary_markdown(result: Facts) -> str:
    lines = ["| Question | Answer |", "| --- | --- |"]
    for question, answer in summary_rows(result["facts"]):
        lines.append(f"| {question} | {answer.replace('|', '/')} |")
    for name, error in result["errors"].items():
        lines.append(f"| Probe {name} | failed: {error.replace('|', '/')} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "--only",
        default="",
        help="comma-separated probes to run; all of them when empty",
    )
    parser.add_argument("--json-out", help="write the JSON object here, not to stdout")
    parser.add_argument("--summary-out", help="write a Markdown table of the answers")
    args = parser.parse_args(argv)

    names = [name for name in re.split(r"[,\s]+", args.only) if name] or list(PROBES)
    unknown = [name for name in names if name not in PROBES]
    if unknown:
        parser.error(
            f"unknown probe {', '.join(unknown)}; expected one of {', '.join(PROBES)}"
        )

    facts: Facts = {}
    errors: dict[str, str] = {}
    for name in names:
        try:
            facts[name] = PROBES[name]()
        except Exception as e:  # one failed probe must not stop the rest
            errors[name] = f"{type(e).__name__}: {e}"
    result: Facts = {
        "schema": SCHEMA,
        "probe": "machine-facts",
        "time_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        ),
        "probes": names,
        "facts": facts,
        "errors": errors,
    }

    text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as f:
            f.write(text)
    else:
        sys.stdout.write(text)
    if args.summary_out:
        try:
            table = summary_markdown(result)
        except Exception as e:  # the JSON object above is the result
            table = f"The summary could not be written: {type(e).__name__}: {e}\n"
        with open(args.summary_out, "w", encoding="utf-8") as f:
            f.write(table)
    for name, error in errors.items():
        sys.stderr.write(f"probe {name} failed: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
