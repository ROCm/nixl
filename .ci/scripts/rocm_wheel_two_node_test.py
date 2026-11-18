#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Two-node UCX transfer test for an installed rocm-nixl wheel.

Run once per node. The target registers a buffer and serves it; the initiator
writes a known pattern into it and reads it back. The initiator fetches the
target's metadata from its listener, so neither etcd nor a shared file is
needed. Needs only the standard library, numpy, the installed wheel and
rocm_wheel_smoke_test.py beside this file. Prints one PASS/FAIL/SKIP line per
check and exits 0 only if every requested check passed.

    rocm_wheel_two_node_test.py --role target --listen-port PORT
        [--bind-ip IP] --expect-rocm X.Y [--mem dram|vram] [--size BYTES]
        [--timeout SECONDS] [--require-transport NAME]
    rocm_wheel_two_node_test.py --role initiator --peer-ip IP --peer-port PORT
        --expect-rocm X.Y [--mem dram|vram] [--size BYTES] [--timeout SECONDS]
        [--require-transport NAME]

The transport reported is the one UCX selected for ucp_put and ucp_get on the
endpoint between the two agents (UCX_PROTO_INFO). It is UCX's protocol
selection, not a byte count. --require-transport also restricts UCX to that
family through UCX_TLS, unless the caller set UCX_TLS, so give it to both
roles.
"""

from __future__ import annotations

import argparse
import contextlib
import ctypes
import faulthandler
import fcntl
import glob
import ipaddress
import os
import pathlib
import pickle
import re
import signal
import socket
import struct
import sys
import tempfile
import time
from typing import Any, Callable, Iterator, Optional

import numpy as np

try:
    from rocm_wheel_smoke_test import (
        CheckFailed,
        Hip,
        Reporter,
        check_import,
        expected_backend,
        load_api,
        pattern,
        require_equal,
        site_dir,
    )
except ImportError as exc:
    sys.exit(f"rocm_wheel_smoke_test.py must sit beside this script: {exc}")

TARGET = "two-node-target"
INITIATOR = "two-node-initiator"
READY_LINE = "READY target"
DESCS_TAG = b"DESCS:"
WROTE_TAG = b"WROTE:"
DONE_TAG = b"DONE:"
BYE_TAG = b"BYE:"
ABORT_TAG = b"ABORT:"
DEFAULT_SIZE = 1 << 20
DEFAULT_TIMEOUT_S = 120.0
SIOCGIFADDR = 0x8915
UCP_PARAM_FIELD_FEATURES = 1
UCP_FEATURE_RMA = 1 << 1
UCP_FEATURE_AM = 1 << 6
NETWORK_FAMILIES = ("rc", "ud", "dc", "tcp")
REQUIRED_FAMILIES = {
    "rc": ("rc",),
    "ud": ("ud",),
    "dc": ("dc",),
    "rdma": ("rc", "ud", "dc"),
    "tcp": ("tcp",),
}
# With --mem vram the ROCm memory transports are named too: UCX detects GPU
# memory only through transports UCX_TLS allows.
REQUIRED_TLS = {
    "rc": "rc",
    "ud": "ud",
    "dc": "dc",
    "rdma": "rc,ud,dc",
    "tcp": "tcp",
}


class Stopped(BaseException):
    """A signal or the deadline ended the run; not a failed check."""

    def __init__(self, reason: str, code: int) -> None:
        super().__init__(reason)
        self.code = code


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--role", required=True, choices=("target", "initiator"))
    parser.add_argument(
        "--expect-rocm",
        required=True,
        metavar="X.Y",
        help="ROCm version whose backend 'import nixl' must select",
    )
    parser.add_argument(
        "--listen-port",
        type=int,
        metavar="PORT",
        help="target: port its metadata listener binds, on every address",
    )
    parser.add_argument(
        "--bind-ip",
        metavar="IP",
        help="target: local address the initiator will use; UCX is restricted "
        "to the interface holding it (NIXL's listener cannot be bound to it)",
    )
    parser.add_argument("--peer-ip", metavar="IP", help="initiator: target's address")
    parser.add_argument(
        "--peer-port", type=int, metavar="PORT", help="initiator: target's port"
    )
    parser.add_argument("--mem", choices=("dram", "vram"), default="dram")
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE, metavar="BYTES")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_S,
        metavar="SECONDS",
        help="limit for each wait; the process as a whole gives up after three "
        "times this plus 60 s, even inside a call that never returns",
    )
    parser.add_argument(
        "--require-transport",
        choices=sorted(REQUIRED_FAMILIES),
        metavar="NAME",
        help="restrict UCX to this transport family through UCX_TLS, unless "
        "UCX_TLS is set, and on the initiator fail unless UCX selected it for "
        f"the transfer ({', '.join(sorted(REQUIRED_FAMILIES))}; rdma is any "
        "verbs transport). Give it to both roles",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+", args.expect_rocm):
        parser.error(f"--expect-rocm must be X.Y, got '{args.expect_rocm}'")
    if args.size <= 0 or args.timeout <= 0:
        parser.error("--size and --timeout must be positive")
    if args.role == "target":
        if args.listen_port is None:
            parser.error("--role target requires --listen-port")
        for option in ("peer_ip", "peer_port"):
            if getattr(args, option) is not None:
                name = "--" + option.replace("_", "-")
                parser.error(f"{name} is for --role initiator")
        addresses = [args.bind_ip] if args.bind_ip else []
    else:
        if args.peer_ip is None or args.peer_port is None:
            parser.error("--role initiator requires --peer-ip and --peer-port")
        for option in ("listen_port", "bind_ip"):
            if getattr(args, option) is not None:
                name = "--" + option.replace("_", "-")
                parser.error(f"{name} is for --role target")
        addresses = [args.peer_ip]
    for address in addresses:
        try:
            ipaddress.ip_address(address)
        except ValueError:
            parser.error(f"'{address}' is not a numeric IP address")
    return args


def wait_for(what: str, predicate: Callable[[], Any], timeout: float) -> Any:
    deadline = time.monotonic() + timeout
    while True:
        value = predicate()
        if value:
            return value
        if time.monotonic() > deadline:
            raise CheckFailed(f"{what} not seen in {timeout:.0f}s")
        time.sleep(0.01)


def interface_with(address: str) -> Optional[str]:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        for _, name in socket.if_nameindex():
            request = struct.pack("256s", name.encode()[:15])
            try:
                reply = fcntl.ioctl(sock.fileno(), SIOCGIFADDR, request)
            except OSError:
                continue
            if socket.inet_ntoa(reply[20:24]) == address:
                return name
    return None


def local_address_towards(peer: str) -> str:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.connect((peer, 9))
        return str(sock.getsockname()[0])


def rdma_ports(interface: str) -> list[str]:
    """UCX names of the RDMA ports on the same device as a network interface."""
    ports = glob.glob(f"/sys/class/net/{interface}/device/infiniband/*/ports/*")
    return sorted(
        f"{pathlib.Path(port).parent.parent.name}:{pathlib.Path(port).name}"
        for port in ports
    )


def restrict_transports(args: argparse.Namespace) -> tuple[str, str]:
    preset = os.environ.get("UCX_TLS")
    if preset:
        return "PASS", f"UCX_TLS={preset} was set by the caller and is left alone"
    if not args.require_transport:
        return "SKIP", "no --require-transport, so UCX may use every transport"
    tls = REQUIRED_TLS[args.require_transport]
    if args.mem == "vram":
        tls += ",rocm"
    os.environ["UCX_TLS"] = tls
    return "PASS", f"UCX_TLS={tls} set for --require-transport {args.require_transport}"


def check_ucx_devices(args: argparse.Namespace, backend: str) -> str:
    """Keep UCX on the interface the two nodes share.

    A node can hold an address the peer holds too (QEMU guests on user-mode
    networking all do), which UCX would take for a route to the peer.
    """
    preset = os.environ.get("UCX_NET_DEVICES")
    if preset:
        return f"UCX_NET_DEVICES={preset} was set by the caller and is left alone"
    if args.role == "target":
        address = args.bind_ip
        if not address:
            return "no --bind-ip, so UCX may use every network device"
    else:
        address = args.peer_ip
    if ipaddress.ip_address(address).version != 4:
        return f"{address} is not IPv4, so UCX may use every network device"
    if args.role == "initiator":
        try:
            address = local_address_towards(args.peer_ip)
        except OSError as exc:
            raise CheckFailed(f"no route to {args.peer_ip}: {exc}") from exc
    interface = interface_with(address)
    if interface is None:
        raise CheckFailed(f"no local interface holds {address}")
    devices = [interface, *rdma_ports(interface)]
    os.environ["UCX_NET_DEVICES"] = ",".join(devices)
    where = f"{','.join(devices)} (the interface holding {address})"
    try:
        _, status, text = ucx_context_info(backend)
    except Exception as exc:
        return f"UCX_NET_DEVICES={where}; not checked against UCX: {exc}"
    if status != 0:
        return (
            f"UCX_NET_DEVICES={where}; a UCX context could not be created with "
            f"the current UCX_* settings (status {status}), so the devices "
            "were not checked and the restriction is kept"
        )
    usable = [
        device
        for device in devices
        if re.search(rf"/{re.escape(device)}\s*$", text, re.MULTILINE)
    ]
    if not usable:
        # Not dropped: without it UCX could take an address both nodes hold
        # for a route to the peer.
        raise CheckFailed(
            f"UCX lists no transport on {where} with the current UCX_* settings"
        )
    return f"UCX_NET_DEVICES={where}; UCX can use {', '.join(usable)}"


def describe_rdma_devices() -> tuple[str, str]:
    lines = []
    for device in sorted(glob.glob("/sys/class/infiniband/*")):
        states = []
        for port in sorted(glob.glob(f"{device}/ports/*")):
            try:
                state = pathlib.Path(port, "state").read_text().strip()
            except OSError as exc:
                state = f"unreadable ({exc.strerror})"
            states.append(f"port {pathlib.Path(port).name} {state}")
        lines.append(f"{pathlib.Path(device).name} ({', '.join(states) or 'no port'})")
    if not lines:
        return "SKIP", "no RDMA device in /sys/class/infiniband"
    return "PASS", "; ".join(lines)


def ucx_context_info(backend: str) -> tuple[str, int, str]:
    """UCX version, ucp_init status and the description of a scratch context.

    The context is made from the wheel's own libucp with the current UCX_*
    environment, which is what the agent's backend will see.
    """
    found = str(site_dir(backend) / f"{backend}.libs" / "libucp-*.so*")
    libraries = sorted(glob.glob(found))
    if not libraries:
        raise CheckFailed(f"no bundled libucp at {found}")
    ucp = ctypes.CDLL(libraries[0])
    libc = ctypes.CDLL(None)
    numbers = [ctypes.c_uint() for _ in range(3)]
    ucp.ucp_get_version(*(ctypes.byref(number) for number in numbers))
    major, minor, release = (number.value for number in numbers)
    version = f"UCX {major}.{minor}.{release}"

    ucp.ucp_config_read.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ucp.ucp_config_release.argtypes = [ctypes.c_void_p]
    # ucs_status_t is a one-byte enum.
    ucp.ucp_config_read.restype = ctypes.c_int8
    ucp.ucp_init_version.restype = ctypes.c_int8
    ucp.ucp_init_version.argtypes = [
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    ucp.ucp_context_print_info.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    ucp.ucp_cleanup.argtypes = [ctypes.c_void_p]
    libc.fopen.restype = ctypes.c_void_p
    libc.fopen.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    libc.fclose.argtypes = [ctypes.c_void_p]

    config = ctypes.c_void_p()
    status = ucp.ucp_config_read(None, None, ctypes.byref(config))
    if status != 0:
        return version, status, ""
    # ucp_params_t starts with the field mask and the features; the rest of
    # the block is zero because the mask names nothing else.
    params = (ctypes.c_uint64 * 32)()
    params[0] = UCP_PARAM_FIELD_FEATURES
    params[1] = UCP_FEATURE_RMA | UCP_FEATURE_AM
    context = ctypes.c_void_p()
    status = ucp.ucp_init_version(major, minor, params, config, ctypes.byref(context))
    ucp.ucp_config_release(config)
    if status != 0:
        return version, status, ""
    with tempfile.TemporaryDirectory(prefix="nixl-2node-") as directory:
        path = os.path.join(directory, "context")
        stream = libc.fopen(path.encode(), b"w")
        if not stream:
            ucp.ucp_cleanup(context)
            raise CheckFailed(f"cannot open {path}")
        ucp.ucp_context_print_info(context, stream)
        libc.fclose(stream)
        text = pathlib.Path(path).read_text()
    ucp.ucp_cleanup(context)
    return version, 0, text


def describe_ucx_resources(backend: str) -> tuple[str, str]:
    version, status, text = ucx_context_info(backend)
    if status != 0:
        return (
            "SKIP",
            f"{version}: UCX gave status {status} with the current UCX_* "
            "settings, so nothing can be listed",
        )
    components = re.findall(r"component\s+\d+\s*:\s*(\S+)", text)
    resources = re.findall(r"resource\s+\d+\s*:.*\s(\S+/\S+)\s*$", text, re.MULTILINE)
    verbs = "loaded" if "ib" in components else "NOT loaded"
    return (
        "PASS",
        f"{version}; components {', '.join(components)} (verbs module {verbs}); "
        f"transport/device {', '.join(resources) or 'none'}",
    )


def flush_c_streams() -> None:
    # UCX writes its log through a buffered C stream.
    ctypes.CDLL(None).fflush(None)


def read_ucx_log(path: str) -> list[str]:
    flush_c_streams()
    try:
        text = pathlib.Path(path).read_text(errors="replace")
    except OSError:
        return []
    return [re.sub(r"^\[[^]]*\] \[[^]]*\]\s*", "", line) for line in text.splitlines()]


def ucx_complaints(path: str) -> str:
    lines = [
        line
        for line in read_ucx_log(path)
        if re.search(r"UCX\s+(WARN|ERROR|FATAL)", line)
    ]
    return " | ".join(lines) or "nothing at WARN or above"


def selected_lanes(path: str) -> dict[tuple[str, str], list[str]]:
    """{(endpoint kind, operation): [transport/device, ...]} from UCX_PROTO_INFO."""
    lanes: dict[tuple[str, str], list[str]] = {}
    kind: Optional[str] = None
    operation: Optional[str] = None
    for row in read_ucx_log(path):
        title = re.match(r"\| \S+ (self|intra-node|inter-node) cfg#\d+", row)
        if title:
            kind, operation = title.group(1), None
        elif kind and operation is None and row.startswith("| ") and "ucp_" in row:
            operation = row.strip("| ").strip()
        elif kind and operation and row.startswith("|") and row.count("|") == 4:
            found = lanes.setdefault((kind, operation), [])
            for lane in re.findall(r"[a-z0-9_]+/[A-Za-z0-9_:.-]+", row.split("|")[3]):
                if lane not in found:
                    found.append(lane)
    return lanes


def family(lane: str) -> str:
    transport = lane.split("/", 1)[0]
    for name in NETWORK_FAMILIES:
        if transport == name or transport.startswith(name + "_"):
            return name
    return transport


def check_transport(
    path: str, required: Optional[str], transferred: bool, mem: str
) -> tuple[str, str]:
    lanes = {
        key: value
        for key, value in selected_lanes(path).items()
        if key[0] != "self" and ("ucp_put" in key[1] or "ucp_get" in key[1])
    }
    # UCX prints a table when it configures the endpoint or first needs the
    # protocol, so a table is its selection, not proof the operation ran.
    detail = "UCX's selection for the endpoint to the target: " + (
        "; ".join(
            f"{operation} ({kind}): {', '.join(found)}"
            for (kind, operation), found in sorted(lanes.items())
        )
        or "nothing for ucp_put or ucp_get"
    )
    if mem == "vram":
        gpu = sorted({operation for _, operation in lanes if "rocm" in operation})
        detail += "; of these, for GPU memory: " + (
            ", ".join(f"'{operation}'" for operation in gpu)
            or "none, every table is for host memory"
        )
    if not transferred:
        status = "FAIL" if required else "SKIP"
        return status, f"the transfer did not complete; {detail}"
    if not required:
        return ("PASS" if lanes else "SKIP"), detail
    wanted = REQUIRED_FAMILIES[required]
    problems = [
        f"UCX reported no selection for {call}"
        for call in ("ucp_put", "ucp_get")
        if not any(call in operation for _, operation in lanes)
    ]
    for (_, operation), found in sorted(lanes.items()):
        families = {family(lane) for lane in found}
        others = sorted(families.intersection(NETWORK_FAMILIES).difference(wanted))
        if others:
            problems.append(f"'{operation}' uses {', '.join(others)}")
        elif not families.intersection(wanted):
            problems.append(f"'{operation}' uses no {required} lane")
    if problems:
        return (
            "FAIL",
            f"--require-transport {required}: {'; '.join(problems)}; {detail}",
        )
    return "PASS", f"--require-transport {required} met; {detail}"


class Memory:
    """Buffers of one kind, addressed the way NIXL descriptors address them."""

    def __init__(self, kind: str, size: int) -> None:
        self.nixl_type = "VRAM" if kind == "vram" else "DRAM"
        self.size = size
        self.hip = Hip() if kind == "vram" else None
        self.arrays: dict[int, np.ndarray] = {}
        self.pointers: list[int] = []

    def buffer(self, data: np.ndarray) -> int:
        if self.hip is None:
            array = data.copy()
            self.arrays[array.ctypes.data] = array
            return int(array.ctypes.data)
        pointer = self.hip.malloc(self.size)
        self.pointers.append(pointer)
        self.hip.upload(pointer, data)
        return pointer

    def read(self, address: int) -> np.ndarray:
        if self.hip is None:
            return self.arrays[address]
        return self.hip.download(address, self.size)

    def free(self) -> None:
        if self.hip is not None:
            for pointer in self.pointers:
                self.hip.free(pointer)
        self.pointers.clear()
        self.arrays.clear()


def take_notification(agent: Any, peer: str, tag: bytes) -> Optional[bytes]:
    for message in agent.update_notifs().get(peer, []):
        if message.startswith(ABORT_TAG):
            raise CheckFailed(f"{peer} reported a failure and gave up")
        if message.startswith(tag):
            agent.notifs[peer].remove(message)
            return bytes(message[len(tag) :])
    return None


def run_transfer(
    agent: Any, op: str, local: Any, remote: Any, notif: bytes, timeout: float
) -> None:
    handle = agent.initialize_xfer(op, local, remote, TARGET, notif, backends=["UCX"])
    try:
        state = agent.transfer(handle)
        deadline = time.monotonic() + timeout
        while state == "PROC":
            if time.monotonic() > deadline:
                raise CheckFailed(f"{op} did not complete in {timeout:.0f}s")
            time.sleep(0.001)
            state = agent.check_xfer_state(handle)
        if state != "DONE":
            raise CheckFailed(f"{op} ended in state {state}")
    finally:
        agent.release_xfer_handle(handle)


class Node:
    def __init__(self, args: argparse.Namespace, ucx_log: str) -> None:
        self.args = args
        self.ucx_log = ucx_log
        self.name = TARGET if args.role == "target" else INITIATOR
        self.agent: Any = None
        self.memory: Optional[Memory] = None
        self.registration: Any = None
        self.addresses: list[int] = []
        self.remote: Any = None

    def create_agent(self) -> str:
        api = load_api()
        port = self.args.listen_port if self.args.role == "target" else 0
        # Both roles need the listener thread: it is what reads the reply to a
        # metadata fetch.
        config = api.nixl_agent_config(
            enable_prog_thread=True, enable_listen_thread=True, listen_port=port
        )
        try:
            self.agent = api.nixl_agent(self.name, config)
        except Exception as exc:
            raise CheckFailed(
                f"{type(exc).__name__}: {exc}; UCX log: {ucx_complaints(self.ucx_log)}"
            ) from exc
        # Creating the agent leaves SIGHUP with a handler of UCX's.
        catch_stop_signals()
        if "UCX" not in self.agent.backends:
            raise CheckFailed("agent has no UCX backend")
        return f"agent {self.name} with the UCX backend"

    def contents(self) -> list[np.ndarray]:
        raise NotImplementedError

    def allocate(self) -> str:
        # Before any UCX context, the order the single-node GPU test uses.
        self.memory = Memory(self.args.mem, self.args.size)
        self.addresses = [self.memory.buffer(data) for data in self.contents()]
        return (
            f"{len(self.addresses)} x {self.args.size} bytes of "
            f"{self.memory.nixl_type} allocated"
        )

    def register(self) -> str:
        assert self.memory is not None
        self.registration = self.agent.register_memory(
            [(address, self.args.size, 0, "") for address in self.addresses],
            self.memory.nixl_type,
            backends=["UCX"],
        )
        return f"{len(self.addresses)} buffer(s) registered with UCX"

    def descriptors(self, address: int) -> Any:
        assert self.memory is not None
        return self.agent.get_xfer_descs(
            [(address, self.args.size, 0)], self.memory.nixl_type
        )

    def steps(self) -> list[tuple[str, Callable[[], str]]]:
        raise NotImplementedError

    def close(self) -> None:
        if self.agent is not None and self.registration is not None:
            with contextlib.suppress(Exception):
                self.agent.deregister_memory(self.registration)
        if self.memory is not None:
            with contextlib.suppress(Exception):
                self.memory.free()


class Target(Node):
    def announce(self) -> str:
        port = self.args.listen_port
        print(f"{READY_LINE}: listening on port {port}", flush=True)
        wait_for(
            "the initiator's metadata",
            lambda: self.agent.check_remote_metadata(INITIATOR),
            self.args.timeout,
        )
        return f"received the metadata of {INITIATOR}"

    def send_descriptors(self) -> str:
        descs = self.descriptors(self.addresses[0])
        self.agent.send_notif(
            INITIATOR, DESCS_TAG + self.agent.get_serialized_descs(descs)
        )
        return f"buffer descriptors sent to {INITIATOR} as a UCX notification"

    def verify_write(self) -> str:
        assert self.memory is not None
        wait_for(
            "the initiator's write",
            lambda: take_notification(self.agent, INITIATOR, WROTE_TAG) is not None,
            self.args.timeout,
        )
        received = self.memory.read(self.addresses[0])
        require_equal("the target's buffer", received, pattern(self.args.size))
        return f"{self.args.size} bytes written by the initiator match the pattern"

    def serve_read(self) -> str:
        wait_for(
            "the initiator's read",
            lambda: take_notification(self.agent, INITIATOR, DONE_TAG) is not None,
            self.args.timeout,
        )
        self.agent.send_notif(INITIATOR, BYE_TAG)
        # Stay up until the initiator has let go, so the reply is not lost.
        deadline = time.monotonic() + 5.0
        while self.agent.check_remote_metadata(INITIATOR):
            if time.monotonic() > deadline:
                break
            time.sleep(0.05)
        return "the initiator read the buffer back and finished"

    def contents(self) -> list[np.ndarray]:
        return [np.zeros(self.args.size, dtype=np.uint8)]

    def steps(self) -> list[tuple[str, Callable[[], str]]]:
        return [
            ("agent", self.create_agent),
            ("register", self.register),
            ("metadata", self.announce),
            ("descriptors", self.send_descriptors),
            ("target-buffer", self.verify_write),
            ("served-read", self.serve_read),
        ]


class Initiator(Node):
    def fetch(self) -> None:
        self.agent.fetch_remote_metadata(TARGET, self.args.peer_ip, self.args.peer_port)

    def exchange_metadata(self) -> str:
        # NIXL does not retry a refused connection, so the fetch is repeated
        # until the target's metadata is here.
        deadline = time.monotonic() + self.args.timeout
        attempts = 0
        while not self.agent.check_remote_metadata(TARGET):
            if time.monotonic() > deadline:
                raise CheckFailed(
                    f"no metadata from {self.args.peer_ip}:{self.args.peer_port} "
                    f"in {self.args.timeout:.0f}s ({attempts} attempts)"
                )
            self.fetch()
            attempts += 1
            for _ in range(100):
                if self.agent.check_remote_metadata(TARGET):
                    break
                time.sleep(0.01)
        self.agent.send_local_metadata(self.args.peer_ip, self.args.peer_port)
        return (
            f"metadata exchanged with {self.args.peer_ip}:{self.args.peer_port} "
            f"({attempts} fetch attempt(s))"
        )

    def receive_descriptors(self) -> str:
        payload = wait_for(
            "the target's buffer descriptors",
            lambda: take_notification(self.agent, TARGET, DESCS_TAG),
            self.args.timeout,
        )
        self.remote = pickle.loads(payload)
        # The metadata was a snapshot; one taken before the target registered
        # its buffer does not cover these descriptors.
        deadline = time.monotonic() + self.args.timeout
        while not self.agent.check_remote_metadata(TARGET, self.remote):
            if time.monotonic() > deadline:
                raise CheckFailed("the target's metadata never covered its buffer")
            self.fetch()
            time.sleep(0.2)
        return "received over UCX and covered by the target's metadata"

    def write(self) -> str:
        local = self.descriptors(self.addresses[0])
        run_transfer(
            self.agent, "WRITE", local, self.remote, WROTE_TAG, self.args.timeout
        )
        return f"{self.args.size} bytes {self.args.mem} -> the target's {self.args.mem}"

    def read_back(self) -> str:
        assert self.memory is not None
        local = self.descriptors(self.addresses[1])
        run_transfer(self.agent, "READ", local, self.remote, b"", self.args.timeout)
        received = self.memory.read(self.addresses[1])
        require_equal("the data read back", received, pattern(self.args.size))
        return f"{self.args.size} bytes read back from the target match the pattern"

    def finish(self) -> str:
        self.agent.send_notif(TARGET, DONE_TAG)
        wait_for(
            "the target's acknowledgement",
            lambda: take_notification(self.agent, TARGET, BYE_TAG) is not None,
            self.args.timeout,
        )
        self.agent.remove_remote_agent(TARGET)
        self.agent.invalidate_local_metadata(self.args.peer_ip, self.args.peer_port)
        return "the target verified its buffer and acknowledged"

    def abort(self) -> None:
        if self.agent is not None and self.agent.check_remote_metadata(TARGET):
            with contextlib.suppress(Exception):
                self.agent.send_notif(TARGET, ABORT_TAG)
                time.sleep(0.5)

    def contents(self) -> list[np.ndarray]:
        return [pattern(self.args.size), np.zeros(self.args.size, dtype=np.uint8)]

    def steps(self) -> list[tuple[str, Callable[[], str]]]:
        return [
            ("agent", self.create_agent),
            ("register", self.register),
            ("metadata", self.exchange_metadata),
            ("descriptors", self.receive_descriptors),
            ("write", self.write),
            ("read-back", self.read_back),
            ("target-verified", self.finish),
        ]


@contextlib.contextmanager
def ucx_log_file() -> Iterator[str]:
    """Send UCX's log and its protocol selection to a file this run can read."""
    os.environ.setdefault("UCX_PROTO_INFO", "y")
    preset = os.environ.get("UCX_LOG_FILE")
    if preset:
        yield preset
        return
    with tempfile.TemporaryDirectory(prefix="nixl-2node-ucx-") as directory:
        os.environ["UCX_LOG_FILE"] = os.path.join(directory, "ucx.log")
        yield os.environ["UCX_LOG_FILE"]


def run(args: argparse.Namespace, report: Reporter, ucx_log: str) -> None:
    backend = expected_backend(args.expect_rocm)
    node: Node
    if args.role == "target":
        node = Target(args, ucx_log)
    else:
        node = Initiator(args, ucx_log)
    try:
        ok = report.run("import", lambda: check_import(backend))
        ok = ok and report.run("memory", node.allocate)
        if ok:
            status, detail = restrict_transports(args)
            report.line(status, "ucx-tls", detail)
        ok = ok and report.run("ucx-devices", lambda: check_ucx_devices(args, backend))
        if not ok:
            names = [name for name, _ in node.steps()]
            for name in ["rdma-devices", "ucx-transports", *names, "transport"]:
                report.line("SKIP", name, "an earlier check failed")
            return
        status, detail = describe_rdma_devices()
        report.line(status, "rdma-devices", detail)
        try:
            status, detail = describe_ucx_resources(backend)
        except Exception as exc:
            status, detail = "SKIP", f"{type(exc).__name__}: {exc}"
        report.line(status, "ucx-transports", detail)
        for name, check in node.steps():
            if ok:
                ok = report.run(name, check)
            else:
                report.line("SKIP", name, "an earlier check failed")
        if not ok and isinstance(node, Initiator):
            node.abort()
        if node.agent is None:
            report.line("SKIP", "transport", "no agent was created")
        elif args.role == "target":
            report.line(
                "SKIP",
                "transport",
                "the target issues no put or get; see the initiator",
            )
        else:
            status, detail = check_transport(
                ucx_log, args.require_transport, ok, args.mem
            )
            report.line(status, "transport", detail)
    finally:
        node.close()


def stop_on(reason: str, code: int) -> Callable[[int, Any], None]:
    def handler(signum: int, frame: Any) -> None:
        for number in (signal.SIGTERM, signal.SIGHUP, signal.SIGALRM):
            signal.signal(number, signal.SIG_IGN)
        raise Stopped(reason, code)

    return handler


def catch_stop_signals() -> None:
    for number in (signal.SIGTERM, signal.SIGHUP):
        name = signal.Signals(number).name
        signal.signal(number, stop_on(f"ended by {name}", 128 + number))


def main() -> int:
    args = parse_args()
    limit = 3 * args.timeout
    # Raised in the main thread so the checks' cleanup runs and the UCX log
    # is printed. A hang inside HIP, UCX or the RDMA device cannot be
    # interrupted from Python; faulthandler ends that from a thread that
    # needs no interpreter.
    catch_stop_signals()
    signal.signal(signal.SIGALRM, stop_on(f"not finished in {limit:g}s", 1))
    signal.setitimer(signal.ITIMER_REAL, limit)
    faulthandler.dump_traceback_later(limit + 60, exit=True)
    report = Reporter()
    code = 0
    with ucx_log_file() as ucx_log:
        try:
            run(args, report, ucx_log)
        except Stopped as exc:
            report.line(
                "FAIL", "terminated", f"{exc}; the checks not shown did not run"
            )
            code = exc.code
        finally:
            for line in read_ucx_log(ucx_log):
                print(f"ucx| {line}", flush=True)
    return code or (1 if report.failed else 0)


if __name__ == "__main__":
    sys.exit(main())
