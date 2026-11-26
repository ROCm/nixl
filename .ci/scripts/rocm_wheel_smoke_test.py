#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Smoke test for an installed nixl-rocm wheel.

The wheel carries one nixl_rocm<X>_<Y> backend per supported ROCm version;
every check looks at the backend for --expect-rocm. Needs only the standard
library, numpy and the installed wheel. Prints one PASS/FAIL/SKIP line per
check and exits 0 only if every requested check passed. --json-out FILE also
records the checks as JSON, rewritten after each one so a run that is cut
short still leaves a valid file.

    rocm_wheel_smoke_test.py --expect-rocm X.Y [--gpu] [--ais-dir DIR]
                             [--allow-ais-compat] [--json-out FILE]
"""

from __future__ import annotations

import argparse
import ctypes
import glob
import importlib
import importlib.metadata
import importlib.util
import json
import multiprocessing
import os
import pathlib
import re
import sys
import tempfile
import time
from multiprocessing.connection import Connection
from typing import Any, Callable, Optional

import numpy as np

REQUIRED_PLUGINS = ("UCX", "POSIX", "AIS")
BUFFER_SIZE = 1 << 20
XFER_TIMEOUT_S = 120.0
AIS_TIMEOUT_S = 300.0
HIP_MEMCPY_HOST_TO_DEVICE = 1
HIP_MEMCPY_DEVICE_TO_HOST = 2

AIS_FAST_ENV = {
    "HIPFILE_ALLOW_COMPAT_MODE": "false",
    "HIPFILE_FORCE_COMPAT_MODE": "false",
}
AIS_COMPAT_ENV = {
    "HIPFILE_ALLOW_COMPAT_MODE": "true",
    "HIPFILE_UNSUPPORTED_FILE_SYSTEMS": "true",
}


class CheckFailed(Exception):
    pass


class Detail(str):
    """A check's detail, with the bytes it moved and the path they took."""

    moved: Optional[int] = None
    path: Optional[str] = None


def moved(detail: str, size: int, path: Optional[str]) -> Detail:
    result = Detail(detail)
    result.moved = size
    result.path = path
    return result


class Reporter:
    """Prints each check and, with json_out, keeps a JSON record of them.

    A check's duration_s runs from the end of the check before it, so the
    durations add up to the run and a check cut short still has one.
    """

    def __init__(
        self, script: str = "", expect_rocm: str = "", json_out: Optional[str] = None
    ) -> None:
        self.failed = False
        self.json_out = json_out
        self.begun = self.mark = time.monotonic()
        self.pending: Optional[dict[str, Any]] = None
        self.record: dict[str, Any] = {
            "script": script,
            "expect_rocm": expect_rocm,
            "started": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "duration_s": 0.0,
            "checks": [],
        }
        if json_out:
            try:
                self.write()
            except OSError as exc:
                sys.exit(f"cannot write --json-out {json_out}: {exc}")

    def write(self) -> None:
        """Replace the JSON file, so a reader never sees half of one."""
        if not self.json_out:
            return
        self.record["duration_s"] = round(time.monotonic() - self.begun, 6)
        scratch = f"{self.json_out}.tmp"
        with open(scratch, "w", encoding="utf-8") as stream:
            json.dump(self.record, stream, indent=1)
            stream.write("\n")
        os.replace(scratch, self.json_out)

    def save(self) -> None:
        try:
            self.write()
        except OSError as exc:
            sys.stderr.write(f"cannot write --json-out {self.json_out}: {exc}\n")

    def line(self, status: str, name: str, detail: str) -> None:
        if status == "FAIL":
            self.failed = True
        print(f"{status} {name}: {detail}", flush=True)
        now = time.monotonic()
        if self.pending is not None and self.pending["name"] == name:
            self.record["checks"].remove(self.pending)
            self.pending = None
        self.record["checks"].append(
            {
                "name": name,
                "status": status,
                "detail": str(detail),
                "duration_s": round(now - self.mark, 6),
                "bytes": getattr(detail, "moved", None),
                "path": getattr(detail, "path", None),
            }
        )
        self.mark = now
        self.save()

    def run(self, name: str, check: Callable[[], str]) -> bool:
        # Recorded as failed until it reports, so a check that crashes or is
        # killed does not leave a record that looks like a clean partial run.
        self.pending = {
            "name": name,
            "status": "FAIL",
            "detail": "started but did not finish",
            "duration_s": 0.0,
            "bytes": None,
            "path": None,
        }
        self.record["checks"].append(self.pending)
        self.save()
        try:
            detail = check()
        except Exception as exc:
            self.line("FAIL", name, f"{type(exc).__name__}: {exc}")
            return False
        self.line("PASS", name, detail)
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--expect-rocm",
        required=True,
        metavar="X.Y",
        help="ROCm version whose backend 'import nixl' must select",
    )
    parser.add_argument(
        "--gpu",
        action="store_true",
        help="run the two-agent UCX transfer between GPU buffers",
    )
    parser.add_argument(
        "--ais-dir",
        metavar="DIR",
        help="run the AIS GPU-to-file transfer in DIR (implies --gpu)",
    )
    parser.add_argument(
        "--allow-ais-compat",
        action="store_true",
        help="accept an AIS transfer that cannot use the hipFile fast path",
    )
    parser.add_argument(
        "--json-out",
        metavar="FILE",
        help="also record the checks in FILE as JSON, rewritten after each check",
    )
    args = parser.parse_args()
    if not re.fullmatch(r"\d+\.\d+", args.expect_rocm):
        parser.error(f"--expect-rocm must be X.Y, got '{args.expect_rocm}'")
    if args.allow_ais_compat and not args.ais_dir:
        parser.error("--allow-ais-compat requires --ais-dir")
    if args.ais_dir:
        args.gpu = True
    return args


def expected_backend(rocm_version: str) -> str:
    return "nixl_rocm" + rocm_version.replace(".", "_")


def load_api() -> Any:
    importlib.import_module("nixl")
    return importlib.import_module("nixl._api")


def pattern(size: int) -> np.ndarray:
    return np.random.default_rng(0x10C).integers(0, 256, size, dtype=np.uint8)


def require_equal(what: str, actual: np.ndarray, expected: np.ndarray) -> None:
    if actual.shape != expected.shape or not np.array_equal(actual, expected):
        raise CheckFailed(f"{what} does not match the data written")


def normalise(dist_name: str) -> str:
    return re.sub(r"[-_.]+", "-", dist_name).lower()


def site_dir(backend: str) -> pathlib.Path:
    spec = importlib.util.find_spec(backend)
    if spec is None or not spec.origin:
        raise CheckFailed(f"cannot locate the {backend} package")
    return pathlib.Path(spec.origin).parent.parent


def installed_backends(site: pathlib.Path) -> list[str]:
    return sorted(
        entry.name
        for entry in site.iterdir()
        if entry.is_dir() and re.fullmatch(r"nixl_rocm\d+_\d+", entry.name)
    )


def check_import(backend: str) -> str:
    api = load_api()
    selected = api.__name__.rsplit(".", 1)[0]
    if selected != backend:
        raise CheckFailed(f"'import nixl' selected {selected}, expected {backend}")
    for name in ("nixl._api", "nixl._bindings", "nixl._utils", "nixl.logging"):
        owner = sys.modules[name].__name__.split(".", 1)[0]
        if owner != backend:
            raise CheckFailed(f"{name} comes from {owner}, expected {backend}")
    others = [b for b in installed_backends(site_dir(backend)) if b != backend]
    also = f"; also installed: {', '.join(others)}" if others else ""
    return f"'import nixl' selected {selected}{also}"


def unconditional_requirement(requirement: str) -> Optional[str]:
    """Name a requirement installs without any extra, or None for an extra's."""
    spec, _, marker = requirement.partition(";")
    if re.search(r"\bextra\b", marker):
        return None
    return normalise(re.split(r"[^A-Za-z0-9._-]", spec.strip())[0])


def check_no_torch() -> str:
    wheels = []
    for dist in importlib.metadata.distributions():
        name = normalise(dist.metadata["Name"] or "")
        # The merged wheel, or a nixl-rocm<X>.<Y> backend wheel.
        if not name.startswith("nixl-rocm"):
            continue
        wheels.append(name)
        for requirement in dist.requires or []:
            if unconditional_requirement(requirement) == "torch":
                raise CheckFailed(f"{name} depends on torch ({requirement})")
    if not wheels:
        raise CheckFailed("no nixl-rocm distribution is installed")
    if "torch" in sys.modules:
        raise CheckFailed("importing nixl imported torch")
    try:
        torch = f"torch {importlib.metadata.version('torch')} is installed separately"
    except importlib.metadata.PackageNotFoundError:
        torch = "torch is not installed"
    return f"{', '.join(sorted(wheels))} has no torch requirement; {torch}"


def plugin_dir(backend: str) -> pathlib.Path:
    site = site_dir(backend)
    candidates = [
        site / f".{backend}.mesonpy.libs" / "plugins",
        site / f"{backend}.libs" / "nixl",
    ]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise CheckFailed(
        "no plugin directory at " + " or ".join(str(c) for c in candidates)
    )


def check_plugins(backend: str) -> str:
    override = os.environ.get("NIXL_PLUGIN_DIR")
    if override:
        raise CheckFailed(
            f"NIXL_PLUGIN_DIR={override} is set, so the wheel's plugins are not used"
        )
    directory = plugin_dir(backend)
    files = sorted(entry.name for entry in directory.iterdir())
    missing = [
        name
        for name in REQUIRED_PLUGINS
        if not any(f.startswith(f"libplugin_{name}.") for f in files)
    ]
    if missing:
        raise CheckFailed(f"{directory} has no {', '.join(missing)} plugin: {files}")
    return f"{', '.join(REQUIRED_PLUGINS)} plugins in {directory}"


def mapped_backends() -> set[str]:
    """Backends whose bundled libraries are mapped into this process."""
    try:
        maps = pathlib.Path("/proc/self/maps").read_text()
    except OSError:
        return set()
    return set(re.findall(r"/\.?(nixl_rocm\d+_\d+)(?:\.mesonpy)?(?:\.libs)?/", maps))


def check_agent(backend: str) -> str:
    api = load_api()
    agent = api.nixl_agent("smoke-agent")
    plugins = agent.get_plugin_list()
    missing = [name for name in REQUIRED_PLUGINS if name not in plugins]
    if missing:
        raise CheckFailed(f"agent does not list {', '.join(missing)}: {plugins}")
    if "UCX" not in agent.backends:
        raise CheckFailed("agent has no UCX backend")
    foreign = sorted(mapped_backends() - {backend})
    if foreign:
        raise CheckFailed(
            f"libraries of {', '.join(foreign)} are loaded alongside {backend}"
        )
    return f"agent with UCX backend, plugins {', '.join(sorted(plugins))}"


def wait_for_transfer(agent: Any, handle: Any, what: str) -> None:
    state = agent.transfer(handle)
    deadline = time.monotonic() + XFER_TIMEOUT_S
    while state == "PROC":
        if time.monotonic() > deadline:
            raise CheckFailed(f"{what} did not complete in {XFER_TIMEOUT_S:.0f}s")
        time.sleep(0.001)
        state = agent.check_xfer_state(handle)
    if state != "DONE":
        raise CheckFailed(f"{what} ended in state {state}")


def run_transfer(
    agent: Any, op: str, local: Any, remote: Any, peer: str, backend: str
) -> None:
    handle = agent.initialize_xfer(op, local, remote, peer, backends=[backend])
    try:
        wait_for_transfer(agent, handle, f"{backend} {op}")
    finally:
        agent.release_xfer_handle(handle)


def file_round_trip(
    agent: Any, backend: str, mem_type: str, src: int, dst: int, size: int, fd: int
) -> None:
    """Write src to the file, then read the file back into dst."""
    memory = agent.register_memory(
        [(src, size, 0, ""), (dst, size, 0, "")], mem_type, backends=[backend]
    )
    try:
        file_reg = agent.register_memory([(0, size, fd, "")], "FILE", [backend])
        try:
            file_descs = file_reg.trim()
            for op, addr in (("WRITE", src), ("READ", dst)):
                local = agent.get_xfer_descs([(addr, size, 0)], mem_type)
                run_transfer(agent, op, local, file_descs, agent.name, backend)
        finally:
            agent.deregister_memory(file_reg, backends=[backend])
    finally:
        agent.deregister_memory(memory, backends=[backend])


def check_posix_transfer() -> str:
    api = load_api()
    agent = api.nixl_agent("smoke-posix", api.nixl_agent_config(backends=[]))
    agent.create_backend("POSIX")
    src = pattern(BUFFER_SIZE)
    dst = np.zeros(BUFFER_SIZE, dtype=np.uint8)
    with tempfile.TemporaryDirectory(prefix="nixl-smoke-") as directory:
        path = os.path.join(directory, "posix.bin")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            file_round_trip(
                agent, "POSIX", "DRAM", src.ctypes.data, dst.ctypes.data, src.size, fd
            )
        finally:
            os.close(fd)
        on_disk = np.fromfile(path, dtype=np.uint8)
    require_equal("file content", on_disk, src)
    require_equal("buffer read back from the file", dst, src)
    return moved(
        f"{BUFFER_SIZE} bytes DRAM -> file -> DRAM through POSIX",
        2 * BUFFER_SIZE,
        "posix",
    )


def load_hip_library() -> ctypes.CDLL:
    names = ["libamdhip64.so"]
    rocm_lib = os.path.join(os.environ.get("ROCM_PATH") or "/opt/rocm", "lib")
    for directory in [*os.environ.get("LD_LIBRARY_PATH", "").split(":"), rocm_lib]:
        if directory:
            names += sorted(glob.glob(os.path.join(directory, "libamdhip64.so*")))
    errors = []
    for name in names:
        try:
            return ctypes.CDLL(name)
        except OSError as exc:
            errors.append(str(exc))
    raise CheckFailed("cannot load libamdhip64: " + "; ".join(errors))


class Hip:
    """GPU memory through the HIP C API, device 0."""

    def __init__(self) -> None:
        self.lib = load_hip_library()
        self.lib.hipInit.argtypes = [ctypes.c_uint]
        self.lib.hipMalloc.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_size_t]
        self.lib.hipFree.argtypes = [ctypes.c_void_p]
        self.lib.hipMemcpy.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_int,
        ]
        for function in ("hipInit", "hipMalloc", "hipFree", "hipMemcpy"):
            getattr(self.lib, function).restype = ctypes.c_int
        self.call("hipInit", 0)

    def call(self, function: str, *args: Any) -> None:
        error = getattr(self.lib, function)(*args)
        if error != 0:
            raise CheckFailed(f"{function} failed with hipError_t {error}")

    def malloc(self, size: int) -> int:
        pointer = ctypes.c_void_p()
        self.call("hipMalloc", ctypes.byref(pointer), size)
        if not pointer.value:
            raise CheckFailed("hipMalloc returned a null pointer")
        return pointer.value

    def free(self, pointer: int) -> None:
        self.call("hipFree", pointer)

    def upload(self, pointer: int, data: np.ndarray) -> None:
        self.call(
            "hipMemcpy", pointer, data.ctypes.data, data.size, HIP_MEMCPY_HOST_TO_DEVICE
        )

    def download(self, pointer: int, size: int) -> np.ndarray:
        data = np.zeros(size, dtype=np.uint8)
        self.call(
            "hipMemcpy", data.ctypes.data, pointer, size, HIP_MEMCPY_DEVICE_TO_HOST
        )
        return data


def gpu_buffers(hip: Hip, data: np.ndarray) -> tuple[int, int]:
    """Two GPU buffers: one holding data, one zeroed."""
    src = hip.malloc(data.size)
    try:
        dst = hip.malloc(data.size)
    except Exception:
        hip.free(src)
        raise
    try:
        hip.upload(src, data)
        hip.upload(dst, np.zeros(data.size, dtype=np.uint8))
    except Exception:
        hip.free(src)
        hip.free(dst)
        raise
    return src, dst


def check_ucx_vram_transfer() -> str:
    api = load_api()
    hip = Hip()
    data = pattern(BUFFER_SIZE)
    src, dst = gpu_buffers(hip, data)
    try:
        initiator = api.nixl_agent("smoke-ucx-initiator")
        target = api.nixl_agent("smoke-ucx-target")
        src_reg = initiator.register_memory([(src, data.size, 0, "")], "VRAM")
        dst_reg = target.register_memory([(dst, data.size, 0, "")], "VRAM")
        try:
            initiator.add_remote_agent(target.get_agent_metadata())
            target.add_remote_agent(initiator.get_agent_metadata())
            local = initiator.get_xfer_descs([(src, data.size, 0)], "VRAM")
            remote = initiator.get_xfer_descs([(dst, data.size, 0)], "VRAM")
            run_transfer(initiator, "WRITE", local, remote, target.name, "UCX")
            received = hip.download(dst, data.size)
        finally:
            initiator.deregister_memory(src_reg)
            target.deregister_memory(dst_reg)
        del initiator, target
    finally:
        hip.free(src)
        hip.free(dst)
    require_equal("GPU buffer on the target agent", received, data)
    return moved(
        f"{BUFFER_SIZE} bytes VRAM -> VRAM between two agents through UCX",
        BUFFER_SIZE,
        "ucx",
    )


def ais_round_trip(directory: str, direct: bool) -> None:
    api = load_api()
    hip = Hip()
    agent = api.nixl_agent("smoke-ais", api.nixl_agent_config(backends=[]))
    agent.create_backend("AIS")
    data = pattern(BUFFER_SIZE)
    path = os.path.join(directory, f"nixl-smoke-ais-{os.getpid()}.bin")
    flags = os.O_RDWR | os.O_CREAT | (os.O_DIRECT if direct else 0)
    src, dst = gpu_buffers(hip, data)
    try:
        fd = os.open(path, flags, 0o600)
        try:
            file_round_trip(agent, "AIS", "VRAM", src, dst, data.size, fd)
        finally:
            os.close(fd)
        received = hip.download(dst, data.size)
        on_disk = np.fromfile(path, dtype=np.uint8)
    finally:
        hip.free(src)
        hip.free(dst)
        if os.path.exists(path):
            os.unlink(path)
    require_equal("file content", on_disk, data)
    require_equal("GPU buffer read back from the file", received, data)


def ais_worker(
    directory: str, env: dict[str, str], direct: bool, result: Connection
) -> None:
    # hipFile reads its environment when the driver opens, so each attempt
    # gets a fresh process.
    os.environ.update(env)
    try:
        ais_round_trip(directory, direct)
    except Exception as exc:
        result.send(f"{type(exc).__name__}: {exc}")
    else:
        result.send(None)


def ais_attempt(directory: str, env: dict[str, str], direct: bool) -> Optional[str]:
    """Run the AIS round trip in a child process; return its error, if any."""
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    worker = context.Process(target=ais_worker, args=(directory, env, direct, sender))
    worker.start()
    sender.close()
    error: Optional[str]
    try:
        if receiver.poll(AIS_TIMEOUT_S):
            error = receiver.recv()
        else:
            error = f"no result in {AIS_TIMEOUT_S:.0f}s"
            worker.kill()
    except EOFError:
        error = None
    worker.join(30)
    if worker.is_alive():
        worker.kill()
        worker.join()
        if error is None:
            error = "worker did not exit after reporting"
    if error is None and worker.exitcode != 0:
        error = f"worker process exited with status {worker.exitcode}"
    return error


def check_ais_transfer(directory: str, allow_compat: bool) -> str:
    if not os.path.isdir(directory):
        raise CheckFailed(f"--ais-dir {directory} is not a directory")
    what = f"{BUFFER_SIZE} bytes VRAM -> file -> VRAM through AIS in {directory}"
    fast_error = ais_attempt(directory, AIS_FAST_ENV, direct=True)
    if fast_error is None:
        return moved(
            f"{what}; hipFile fast path (O_DIRECT file, "
            "HIPFILE_ALLOW_COMPAT_MODE=false so no POSIX fallback was possible)",
            2 * BUFFER_SIZE,
            "ais-fast-path",
        )
    if not allow_compat:
        raise CheckFailed(
            f"hipFile fast path failed ({fast_error}); "
            "--allow-ais-compat would accept the POSIX fallback"
        )
    compat_error = ais_attempt(directory, AIS_COMPAT_ENV, direct=False)
    if compat_error is not None:
        raise CheckFailed(
            f"hipFile fast path failed ({fast_error}) and the compat attempt "
            f"failed too ({compat_error})"
        )
    return moved(
        f"{what}; NOT the hipFile fast path, which failed ({fast_error}); passed "
        "with HIPFILE_ALLOW_COMPAT_MODE=true and "
        "HIPFILE_UNSUPPORTED_FILE_SYSTEMS=true on a buffered file, where hipFile "
        "is free to use its POSIX fallback",
        2 * BUFFER_SIZE,
        "ais-compat",
    )


def main() -> int:
    args = parse_args()
    backend = expected_backend(args.expect_rocm)
    report = Reporter("rocm_wheel_smoke_test.py", args.expect_rocm, args.json_out)

    def after_import(name: str, check: Callable[[], str]) -> None:
        if imported:
            report.run(name, check)
        else:
            report.line("SKIP", name, "'import nixl' did not select " + backend)

    imported = report.run("import", lambda: check_import(backend))
    report.run("no-torch", check_no_torch)
    report.run("plugins", lambda: check_plugins(backend))
    after_import("agent", lambda: check_agent(backend))
    after_import("posix-transfer", check_posix_transfer)
    if args.gpu:
        after_import("ucx-vram-transfer", check_ucx_vram_transfer)
    else:
        report.line("SKIP", "ucx-vram-transfer", "--gpu not given")
    if args.ais_dir:
        after_import(
            "ais-transfer",
            lambda: check_ais_transfer(args.ais_dir, args.allow_ais_compat),
        )
    else:
        report.line("SKIP", "ais-transfer", "--ais-dir not given")
    report.save()
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main())
