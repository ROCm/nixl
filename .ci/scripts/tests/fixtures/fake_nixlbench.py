#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Stand-in for nixlbench in the tests of amd_bench_run.py.

Takes --name=value flags only, prints what nixlbench prints (preamble, config
block, table) and exits the way it does: a process of a pair whose peer goes
away without finishing aborts over its ASIO runtime, as nixlbench does.
FAKE_NIXLBENCH_MODE selects what the initiator, or the only process, does;
FAKE_NIXLBENCH_TGT_MODE what an ASIO target does. The modes are the keys of
MODES. Standard library only.
"""

from __future__ import annotations

import os
import resource
import signal
import socket
import sys
import time
from typing import NoReturn

MODES = (
    "ok",
    "fail",
    "inconsistent",
    "short",
    "bad-header",
    "twelve",
    "echo-mismatch",
    "hang",
    "hang-hard",
    "abort",
    "creds",
)
DEFAULTS = {
    "start_block_size": "4096",
    "max_block_size": str(64 << 20),
    "start_batch_size": "1",
    "max_batch_size": "1",
}
# The descriptions printConfig gives the flags the runner compares.
DESCRIPTIONS = {
    "runtime_type": "Runtime (--runtime_type=[ETCD,ASIO])",
    "asio_address": "ASIO Address (--asio_address) ",
    "asio_port": "ASIO Port (--asio_port) ",
    "backend": "Backend (--backend=[UCX,GDS,GDS_MT,AIS,POSIX,MOONCAKE,HF3FS,OBJ,REDIS,"
    "GUSLI,AZURE_BLOB,DOCA_MEMOS])",
    "pipeline_depth": "Pipeline depth (--pipeline_depth=N)",
    "posix_api_type": "POSIX API type (--posix_api_type=[AIO,URING,POSIXAIO])",
    "filepath": "filepath (--filepath=path)",
    "num_files": "Number of files (--num_files=N)",
    "storage_enable_direct": "Storage enable direct (--storage_enable_direct=[0,1])",
    "initiator_seg_type": "Initiator seg type (--initiator_seg_type=[DRAM,VRAM])",
    "target_seg_type": "Target seg type (--target_seg_type=[DRAM,VRAM])",
    "scheme": "Scheme (--scheme=[pairwise,manytoone,onetomany,tp])",
    "op_type": "Op type (--op_type=[READ,WRITE,QUERY])",
    "check_consistency": "Check consistency (--check_consistency=[0,1])",
    "total_buffer_size": "Total buffer size (--total_buffer_size=N)",
    "start_block_size": "Start block size (--start_block_size=N)",
    "max_block_size": "Max block size (--max_block_size=N)",
    "start_batch_size": "Start batch size (--start_batch_size=N)",
    "max_batch_size": "Max batch size (--max_batch_size=N)",
    "num_iter": "Num iter (--num_iter=N)",
    "warmup_iter": "Warmup iter (--warmup_iter=N)",
    "large_blk_iter_ftr": "Large block iter factor (--large_blk_iter_ftr=N)",
    "num_threads": "Num threads (--num_threads=N)",
}
CREDENTIALS = {
    "obj_access_key": "OBJ S3 access key (--obj_access_key=key)",
    "obj_secret_key": "OBJ S3 secret key (--obj_secret_key=key)",
    "obj_session_token": "OBJ S3 session token (--obj_session_token=token)",
    "azure_blob_connection_string": "Azure Blob Storage connection string "
    "(--azure_blob_connection_string=connection-string)",
}
HEADER = [
    ("Block Size (B)", 20),
    ("Batch Size", 15),
    ("B/W (GB/Sec)", 15),
    ("Avg Lat. (us)", 15),
    ("Avg Prep (us)", 15),
    ("P99 Prep (us)", 15),
    ("Avg Post (us)", 15),
    ("P99 Post (us)", 15),
    ("Avg Tx (us)", 15),
    ("P99 Tx (us)", 15),
]
EXTRA = [("Aggregate B/W (GB/Sec)", 25), ("Network Util (%)", 20)]
CONNECT_LINE = "ASIO runtime bind/listen error -- using connect() instead"


def out(text: str = "") -> None:
    print(text, flush=True)


def err(text: str) -> None:
    print(text, file=sys.stderr, flush=True)


def abort() -> NoReturn:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    os.abort()


def peer_lost(what: str = "ASIO Read head failed: End of file") -> NoReturn:
    """The end nixlbench makes when the other process of the pair is gone."""
    err("terminate called after throwing an instance of 'std::runtime_error'")
    err(f"  what():  {what}")
    abort()


def said(peer: socket.socket, byte: bytes) -> bool:
    try:
        return peer.recv(1) == byte
    except OSError:
        return False


def on_sigint(signum: int, frame: object) -> None:
    os.write(1, b"Ctrl-C received, exiting...\n")
    os._exit(1)


def hang(hard: bool) -> NoReturn:
    if hard:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    while True:
        time.sleep(3600)


def parse(argv: list[str]) -> dict[str, str]:
    flags = {}
    for arg in argv:
        name, eq, value = arg.partition("=")
        if "flagfile" in arg or not name.startswith("--") or not eq:
            err(f"fake nixlbench: bad argument '{arg}'")
            sys.exit(1)
        flags[name[2:]] = value
    return flags


def as_printed(value: str) -> str:
    return {"true": "1", "false": "0"}.get(value, value)


def print_config(flags: dict[str, str], mode: str) -> None:
    out("*" * 160)
    out("NIXLBench Configuration")
    out("*" * 160)
    for name, description in DESCRIPTIONS.items():
        if name not in flags:
            continue
        value = as_printed(flags[name])
        if mode == "echo-mismatch" and name == "num_iter":
            value = str(int(value) + 16)
        out(f"{description:<60}: {value}")
    if mode == "creds":
        azure = os.environ.get("FAKE_NIXLBENCH_AZURE_SECRET", "")
        for name, description in CREDENTIALS.items():
            out(f"{description:<60}: {flags.get(name, azure)}")
        secret = flags.get("obj_secret_key", "")
        err(f"AWS_SECRET_ACCESS_KEY={secret} was exported")
        # Forms no pattern knows: only the value itself gives them away.
        err(f'aws: {{"SecretAccessKey": "{secret}"}} password: {secret}')
    out("-" * 160)
    out()


def sweep(flags: dict[str, str]) -> list[tuple[int, int]]:
    start_block, max_block, start_batch, max_batch = (
        int(flags.get(name, default)) for name, default in DEFAULTS.items()
    )
    rows = []
    block = start_block
    while block <= max_block:
        batch = start_batch
        while batch <= max_batch:
            rows.append((block, batch))
            batch *= 2
        block *= 2
    return rows


def print_table(flags: dict[str, str], mode: str) -> None:
    header = list(HEADER)
    if mode == "twelve":
        header[3:3] = EXTRA
    if mode == "bad-header":
        header[3] = ("Med Lat. (us)", 15)
    out("".join(f"{name:<{width}}" for name, width in header))
    out("-" * 160)
    if mode == "abort":
        abort()
    if mode in ("hang", "hang-hard"):
        hang(mode == "hang-hard")
    rows = sweep(flags)
    for block, batch in rows[:-1] if mode == "short" else rows:
        cells = [f"{block:<20}{batch:<15}{'1.250000':<15}"]
        if mode == "twelve":
            cells.append(f"{'2.500000':<25}{'1.000000':<20}")
        cells.extend(
            f"{value:<15.1f}" for value in (3.7, 5.0, 5.0, 14.1, 31.0, 0.6, 2.0)
        )
        out("".join(cells))


def write_ucx_log(path: str) -> None:
    tls = os.environ.get("UCX_TLS", "tcp").split(",")
    lanes = ["tcp/eth0" if tls[0] == "tcp" else f"{tls[0]}_verbs/ernic0:1"]
    if "rocm" in tls:
        lanes.append("rocm_copy/rocm")
    stamp = f"[1700000000.000000] [host:{os.getpid()}:0]  "
    lines = []
    for number, operation in enumerate(("ucp_put*", "ucp_get*")):
        lines += [
            f"{stamp} | nixlbench inter-node cfg#{number + 1} |",
            f"{stamp} | remote memory access by {operation} |",
            f"{stamp} | 0..inf | zero-copy | {' '.join(lanes)} |",
        ]
    with open(path, "w", encoding="utf-8") as log:
        log.write("\n".join(lines) + "\n")


def asio(flags: dict[str, str]) -> tuple[socket.socket, bool]:
    """The connection to the peer, and whether this process bound the address."""
    address = (flags.get("asio_address", "127.0.0.1"), int(flags["asio_port"]))
    out(f"Using address {address[0]} port {address[1]} for ASIO runtime")
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(address)
        listener.listen()
    except OSError:
        listener.close()
        out(CONNECT_LINE)
        try:
            return socket.create_connection(address, timeout=15), False
        except OSError as exc:
            err(f"ASIO Connection failed: {exc}")
            abort()
    out("Init nixl worker, dev all rank 0, type initiator, hostname host")
    listener.settimeout(15)
    try:
        peer, _ = listener.accept()
    except OSError:
        err("ASIO Receive timeout")
        abort()
    listener.close()
    return peer, True


def target(peer: socket.socket, mode: str) -> int:
    if mode == "fail":
        err("fake nixlbench: the target fails as asked")
        return 1
    if mode == "abort":
        abort()
    if mode in ("hang", "hang-hard"):
        hang(mode == "hang-hard")
    peer.settimeout(60)
    if not said(peer, b"D"):
        peer_lost()
    if mode == "inconsistent":
        err("Consistency check failed")
        return 1
    try:
        peer.sendall(b"K")
    except OSError:
        peer_lost()
    return 0


def main(argv: list[str] | None = None) -> int:
    signal.signal(signal.SIGINT, on_sigint)
    flags = parse(sys.argv[1:] if argv is None else argv)
    mode = os.environ.get("FAKE_NIXLBENCH_MODE", "ok")
    target_mode = os.environ.get("FAKE_NIXLBENCH_TGT_MODE", "ok")
    if mode not in MODES or target_mode not in MODES:
        err(f"fake nixlbench: unknown mode '{mode}' or '{target_mode}'")
        return 1
    if os.environ.get("UCX_LOG_FILE"):
        write_ucx_log(os.environ["UCX_LOG_FILE"])
    peer = None
    if flags.get("runtime_type") == "ASIO":
        peer, bound = asio(flags)
        if not bound:
            return target(peer, target_mode)
    else:
        out("Using null runtime for storage backend without ETCD")
    out("All processes are ready to proceed")
    if mode == "fail":
        err("fake nixlbench: failing as asked")
        return 1
    print_config(flags, mode)
    print_table(flags, mode)
    if mode == "inconsistent":
        err("Consistency check failed")
        return 1
    if peer is not None:
        # The target has the last word: it is the one that checks a WRITE.
        peer.settimeout(60)
        try:
            peer.sendall(b"D")
        except OSError:
            peer_lost()
        if not said(peer, b"K"):
            peer_lost()
        peer.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
