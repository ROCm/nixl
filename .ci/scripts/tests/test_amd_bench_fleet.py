# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of the fleet subcommands of amd_bench_ci.py and of the fleet layers.

    python3 -B -m unittest discover -s .ci/scripts/tests -v

The fleet XML and the manifests of the first classes are written here, all
synthetic. The last classes read the overlays under .ci/rocjitsu, what
amd_bench_stack.sh makes of them before and after a boot, and the workflows
as text. No network, no docker, no ssh, no qemu-tool.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import amd_bench_ci as ci  # noqa: E402
import amd_bench_lib as lib  # noqa: E402
import amd_bench_run as run  # noqa: E402

SCRIPTS = pathlib.Path(__file__).resolve().parents[1]
REPO = SCRIPTS.parents[1]
OVERLAYS = REPO / ".ci" / "rocjitsu"
WORKFLOWS = REPO / ".github" / "workflows"
COMMON = "fleet-overlay-common.xml"
# The guests each VM lane boots and the overlay it lays over the common one.
VM_LANES = {
    "vm1-local": (1, "fleet-overlay-bench-local.xml"),
    "vm2-net": (2, "fleet-overlay-bench-net.xml"),
    "vm1-loop": (1, "fleet-overlay-backends.xml"),
    "vm1-fabric": (1, "fleet-overlay-backends.xml"),
}
FABRIC_LANES = ("vm1-loop", "vm1-fabric")
# The overlay each rocjitsu wheel test lays over the common one.
WHEEL_TESTS = {
    "rocm-wheels-rocjitsu.yml": "fleet-overlay.xml",
    "rocm-wheels-rocjitsu-2vm.yml": "fleet-overlay-2vm.xml",
}
VM_WORKFLOWS = ("amd-bench-vm.yml", "amd-bench-probe.yml", *WHEEL_TESTS)
TAGS = {
    "qemu-ref": "registry.invalid/ci/qemu-fork:20260101.g1-qemu.2",
    "ernic-ref": "registry.invalid/ci/ernic-fork:20260101.g1-ernic.3",
    "rocjitsu-ref": "registry.invalid/ci/rocjitsu:20260101.g1",
    "backing-ref": "registry.invalid/guest:20260102.g4",
}
DIGESTS = {name: "sha256:" + char * 64 for name, char in zip(TAGS, "abcd")}
PINNED = {name: f"{tag}@{DIGESTS[name]}" for name, tag in TAGS.items()}
TOOL_REF = "c" * 40
BASE_META: dict[str, Any] = {
    "stamp": "20261119T032301Z",
    "nightly_run_id": "4242",
    "reason": "nightly",
    "tier": "nightly",
    "repo": "ROCm/nixl",
    "branch": "for-upstream/feat/a",
    "slug": "feat-a",
    "sha": "1" * 40,
    "upstream_main_sha": None,
    "nixl_version": "1.2.0.dev20261119",
    "nixlbench_version": "1.2.0.dev20261119",
    "nixlbench_sha256": "ab" * 32,
    "features": [],
}


def call(*argv: str) -> tuple[int, str, str]:
    """(exit status, stdout, stderr) of amd_bench_ci.py run in this process."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = ci.main(list(argv))
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


def fleet(*elements: tuple[str, str], comment: str = "") -> str:
    """A fleet document with the elements in the order given."""
    lines = ['<?xml version="1.0" encoding="UTF-8"?>', "<!-- a header -->"]
    lines.append(f'<fleet xmlns="{ci.FLEET_NS}">')
    if comment:
        lines.append(f"  <!-- {comment} -->")
    lines += [f"  <{name}>{text}</{name}>" for name, text in elements]
    return "\n".join(lines + ["</fleet>"]) + "\n"


def flags(option: str, paths: list[pathlib.Path]) -> list[str]:
    return [arg for path in paths for arg in (option, str(path))]


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="amd-bench-fleet-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, name: str, text: str) -> pathlib.Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def pinned(self, **changes: str | None) -> pathlib.Path:
        """An overlay that pins the four images, but for the changes."""
        refs: dict[str, str | None] = {**PINNED}
        refs.update({key.replace("_", "-"): value for key, value in changes.items()})
        elements = [(key, value) for key, value in refs.items() if value]
        return self.write("common.xml", fleet(*elements))


class FleetValueTest(Scratch):
    def value(self, element: str, *layers: pathlib.Path) -> tuple[int, str, str]:
        return call("fleet-value", "--element", element, *flags("--layer", [*layers]))

    def test_the_last_layer_that_has_the_element_wins(self) -> None:
        base = self.write(
            "base.xml",
            fleet(("vm-count", "2"), ("ernic-tap", "true"), ("vm-nvme", "1")),
        )
        common = self.write("common.xml", fleet(("vm-nvme-vram", "false")))
        lane = self.write("lane.xml", fleet(("vm-count", "1"), ("ernic-tap", "false")))
        for element, want in (
            ("vm-count", "1"),
            ("ernic-tap", "false"),
            ("vm-nvme", "1"),
            ("vm-nvme-vram", "false"),
        ):
            self.assertEqual(
                self.value(element, base, common, lane), (0, want + "\n", ""), element
            )
        self.assertEqual(self.value("vm-count", lane, base)[1], "2\n")

    def test_comments_are_ignored_and_the_text_is_stripped(self) -> None:
        layer = self.write(
            "layer.xml",
            fleet(
                ("vm-count", " 1\n  "),
                ("vm-name-prefix", "<!-- inside -->lane"),
                comment="<vm-count>9</vm-count> and <ernic-tap>true</ernic-tap>",
            ),
        )
        self.assertEqual(self.value("vm-count", layer), (0, "1\n", ""))
        self.assertEqual(self.value("ernic-tap", layer)[0], 2)
        # A comment inside an element is no part of its text.
        self.assertEqual(self.value("vm-name-prefix", layer)[1], "lane\n")

    def test_an_element_no_layer_has_is_refused(self) -> None:
        layer = self.write("layer.xml", fleet(("vm-count", "1")))
        code, out, err = self.value("vm-nvme", layer, layer)
        self.assertEqual(code, 2)
        self.assertIn("no layer has <vm-nvme>", err)
        self.assertTrue(out.startswith("::error title=amd_bench_ci.py fleet-value::"))
        code, out, err = self.value("vm-count::error::x", layer)
        self.assertEqual(code, 2)
        self.assertNotIn("error::x", out + err)

    def test_only_elements_directly_under_fleet_count(self) -> None:
        text = (
            f'<fleet xmlns="{ci.FLEET_NS}"><devices><vm-count>7</vm-count></devices>'
            '<vm-count xmlns="http://example.invalid/other">8</vm-count></fleet>'
        )
        layer = self.write("layer.xml", text)
        self.assertEqual(self.value("vm-count", layer)[0], 2)
        self.assertEqual(self.value("devices", layer), (0, "\n", ""))

    def test_a_file_that_is_not_a_fleet_document_is_refused(self) -> None:
        good = self.write("good.xml", fleet(("vm-count", "1")))
        for name, text, reason in (
            ("other.xml", "<fleet><vm-count>1</vm-count></fleet>", "not a fleet"),
            ("root.xml", f'<stack xmlns="{ci.FLEET_NS}"/>', "not a fleet document"),
            ("broken.xml", "<fleet", "is not XML"),
            ("empty.xml", "", "is not XML"),
        ):
            code, _, err = self.value("vm-count", good, self.write(name, text))
            self.assertEqual(code, 2, name)
            self.assertIn(reason, err)
        code, _, err = self.value("vm-count", self.tmp / "absent.xml", good)
        self.assertEqual(code, 2)
        self.assertIn("cannot read the fleet layer", err)

    def test_an_element_given_twice_in_the_layer_that_decides_is_refused(self) -> None:
        twice = self.write("twice.xml", fleet(("vm-count", "1"), ("vm-count", "2")))
        once = self.write("once.xml", fleet(("vm-count", "3"), ("vm-nvme", "1")))
        code, _, err = self.value("vm-count", once, twice)
        self.assertEqual(code, 2)
        self.assertIn("has <vm-count> more than once", err)
        # A later layer decides, and the repeat below it is never asked.
        self.assertEqual(self.value("vm-count", twice, once), (0, "3\n", ""))
        self.assertEqual(self.value("vm-nvme", once, twice), (0, "1\n", ""))


class FleetPinsTest(Scratch):
    def pins(self, *overlays: pathlib.Path) -> tuple[int, str, str]:
        return call("fleet-pins", *flags("--overlay", [*overlays]))

    def test_the_six_lines_in_order(self) -> None:
        common = self.pinned()
        lane = self.write("lane.xml", fleet(("vm-count", "1")))
        code, out, err = self.pins(common, lane)
        self.assertEqual((code, err), (0, ""))
        whole = hashlib.sha256(common.read_bytes() + lane.read_bytes()).hexdigest()
        self.assertEqual(
            out.splitlines(),
            [
                f"runner_image={PINNED['qemu-ref']}",
                f"overlay_sha256={whole}",
                f"guest_image_digest={DIGESTS['backing-ref']}",
                f"qemu_digest={DIGESTS['qemu-ref']}",
                f"ernic_digest={DIGESTS['ernic-ref']}",
                f"rocjitsu_digest={DIGESTS['rocjitsu-ref']}",
            ],
        )
        # Every line is a run-meta key the job may set.
        keys = [line.split("=")[0] for line in out.splitlines()]
        self.assertLessEqual(set(keys), set(lib.META_KEYS))
        # The digest is of the files in the order given.
        other = self.pins(lane, common)[1].splitlines()
        self.assertNotEqual(other[1], f"overlay_sha256={whole}")
        self.assertEqual(other[2:], out.splitlines()[2:])

    def test_a_later_layer_wins(self) -> None:
        moved = "registry.invalid/ci/qemu-fork:20260303.g9@sha256:" + "e" * 64
        common = self.pinned()
        lane = self.write("lane.xml", fleet(("qemu-ref", moved)))
        lines = self.pins(common, lane)[1].splitlines()
        self.assertEqual(lines[0], f"runner_image={moved}")
        self.assertEqual(lines[3], "qemu_digest=sha256:" + "e" * 64)
        self.assertEqual(lines[2], f"guest_image_digest={DIGESTS['backing-ref']}")

    def test_a_missing_ref_is_refused(self) -> None:
        for name in ci.PIN_ELEMENTS:
            code, out, err = self.pins(self.pinned(**{name.replace("-", "_"): None}))
            self.assertEqual(code, 2, name)
            self.assertIn(f"<{name}>: no overlay has it", err)
            self.assertNotIn("runner_image=", out)
            self.assertNotIn("_digest=", out)

    def test_a_ref_without_a_digest_is_refused(self) -> None:
        for name in ci.PIN_ELEMENTS:
            for value in (
                TAGS[name],
                TAGS[name] + "@sha256:" + "a" * 63,
                TAGS[name] + "@sha256:" + "A" * 64,
                TAGS[name] + "@sha512:" + "a" * 64,
                PINNED[name] + "0",
                "two words@sha256:" + "a" * 64,
            ):
                key = name.replace("-", "_")
                code, out, err = self.pins(self.pinned(**{key: value}))
                self.assertEqual(code, 2, (name, value))
                self.assertIn(f"<{name}>: ", err)
                self.assertIn("@sha256: and 64 hex digits", err)
                self.assertNotIn("_digest=", out)
        # Every ref that is not pinned is named, not only the first.
        code, _, err = self.pins(
            self.pinned(qemu_ref=TAGS["qemu-ref"], backing_ref=TAGS["backing-ref"])
        )
        self.assertEqual(code, 2)
        self.assertIn("<qemu-ref>: ", err)
        self.assertIn("<backing-ref>: ", err)

    def test_an_element_given_twice_in_one_file_is_refused(self) -> None:
        elements = [*PINNED.items(), ("vm-count", "1"), ("vm-count", "1")]
        twice = self.write("twice.xml", fleet(*elements))
        code, _, err = self.pins(self.pinned(), twice)
        self.assertEqual(code, 2)
        self.assertIn("has more than once: <vm-count>", err)

    def test_a_file_that_cannot_be_read_is_refused(self) -> None:
        self.assertEqual(self.pins(self.pinned(), self.tmp / "absent.xml")[0], 2)
        self.assertEqual(self.pins(self.pinned(), self.write("x.xml", "<a/>"))[0], 2)
        self.assertEqual(call("fleet-pins")[0], 2)


class FleetBaseCheckTest(Scratch):
    def check(
        self, base: pathlib.Path, *overlays: pathlib.Path
    ) -> tuple[int, str, str]:
        return call(
            "fleet-base-check", "--base", str(base), *flags("--overlay", [*overlays])
        )

    def base(self, **changes: str | None) -> pathlib.Path:
        tags: dict[str, str | None] = {**TAGS}
        tags.update({key.replace("_", "-"): value for key, value in changes.items()})
        elements = [(key, value) for key, value in tags.items() if value]
        return self.write(
            "rocm-golden.xml", fleet(("vm-count", "2"), *elements, ("vm-nvme", "1"))
        )

    def test_four_matching_tags_pass(self) -> None:
        lane = self.write("lane.xml", fleet(("vm-count", "1")))
        code, out, err = self.check(self.base(), self.pinned(), lane)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(
            out.splitlines(), [f"{name}: {TAGS[name]}" for name in ci.PIN_ELEMENTS]
        )

    def test_a_differing_tag_is_named_with_both_values(self) -> None:
        older = "registry.invalid/guest:20251231.g3"
        code, out, err = self.check(self.base(backing_ref=older), self.pinned())
        self.assertEqual(code, 2)
        self.assertIn("<backing-ref>", err)
        self.assertIn(f"'{PINNED['backing-ref']}'", err)
        self.assertIn(f"'{older}'", err)
        for name in ("qemu-ref", "ernic-ref", "rocjitsu-ref"):
            self.assertNotIn(f"<{name}>", err)
            self.assertNotIn(f"{name}: ", out)
        # Every element that differs is named.
        code, _, err = self.check(
            self.base(backing_ref=older, qemu_ref=TAGS["qemu-ref"] + "-stock"),
            self.pinned(),
        )
        self.assertEqual(code, 2)
        self.assertIn("<backing-ref>", err)
        self.assertIn("<qemu-ref>", err)
        self.assertEqual(len(err.splitlines()), 2)

    def test_the_overlays_effective_value_is_what_is_compared(self) -> None:
        moved = "registry.invalid/ci/rocjitsu:20260303.g9"
        lane = self.write(
            "lane.xml", fleet(("rocjitsu-ref", moved + "@sha256:" + "e" * 64))
        )
        code, _, err = self.check(self.base(), self.pinned(), lane)
        self.assertEqual(code, 2)
        self.assertIn("<rocjitsu-ref>", err)
        self.assertEqual(
            self.check(self.base(rocjitsu_ref=moved), self.pinned(), lane)[0], 0
        )

    def test_an_element_missing_from_either_side_is_refused(self) -> None:
        for name in ci.PIN_ELEMENTS:
            key = name.replace("-", "_")
            code, _, err = self.check(self.base(**{key: None}), self.pinned())
            self.assertEqual(code, 2, name)
            self.assertIn(f"<{name}>: the base fleet file", err)
            self.assertIn("has none", err)
            code, _, err = self.check(self.base(), self.pinned(**{key: None}))
            self.assertEqual(code, 2, name)
            self.assertIn(f"<{name}>: no overlay has it", err)

    def test_a_base_that_cannot_be_read_is_refused(self) -> None:
        self.assertEqual(self.check(self.tmp / "absent.xml", self.pinned())[0], 2)
        self.assertEqual(self.check(self.base(), self.tmp / "absent.xml")[0], 2)


class SshConfigTest(Scratch):
    def manifest(self, *changes: dict[str, Any]) -> pathlib.Path:
        """The action's manifest of len(changes) guests, each with its changes."""
        key = self.write("keys/id_rsa", "not a key\n")
        guests = []
        for index, change in enumerate(changes, start=1):
            guest = {
                "name": f"lane-{index}",
                "index": index,
                "ssh_host": "127.0.0.1",
                "ssh_port": 2221 + index,
                "username": "ubuntu",
                "ssh_key_file": str(key),
                "ssh_cmd": f"ssh -i {key} -p {2221 + index} ubuntu@127.0.0.1",
                "rdma_ip": None,
                "nics": [],
                "gpu": None,
            }
            guest.update(change)
            guests.append(guest)
        return self.write("vm-manifest.json", json.dumps(guests))

    def config(self, manifest: pathlib.Path, *aliases: str) -> tuple[int, str, str]:
        self.out = self.tmp / "amd-bench" / "guest-ssh-config"
        return call(
            "ssh-config",
            "--manifest",
            str(manifest),
            *(arg for alias in aliases for arg in ("--alias", alias)),
            "--out",
            str(self.out),
        )

    def test_the_text_of_a_two_guest_file(self) -> None:
        # Listed out of order: an alias goes to the guest of its index.
        manifest = self.manifest({}, {"ssh_host": "172.31.3.2", "username": "ci_user"})
        guests = json.loads(manifest.read_text())
        manifest.write_text(json.dumps(guests[::-1]))
        code, out, err = self.config(manifest, "guest1", "guest2")
        self.assertEqual((code, err), (0, ""))
        key = self.tmp / "keys" / "id_rsa"
        self.assertEqual(
            self.out.read_text(),
            "Host guest1\n"
            "  HostName 127.0.0.1\n"
            "  Port 2222\n"
            "  User ubuntu\n"
            f"  IdentityFile {key}\n"
            "  IdentitiesOnly yes\n"
            "  BatchMode yes\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /dev/null\n"
            "  LogLevel ERROR\n"
            "  ConnectTimeout 10\n"
            "  ServerAliveInterval 15\n"
            "Host guest2\n"
            "  HostName 172.31.3.2\n"
            "  Port 2223\n"
            "  User ci_user\n"
            f"  IdentityFile {key}\n"
            "  IdentitiesOnly yes\n"
            "  BatchMode yes\n"
            "  StrictHostKeyChecking no\n"
            "  UserKnownHostsFile /dev/null\n"
            "  LogLevel ERROR\n"
            "  ConnectTimeout 10\n"
            "  ServerAliveInterval 15\n",
        )
        self.assertEqual(
            out.splitlines(),
            ["guest1: ubuntu@127.0.0.1:2222", "guest2: ci_user@172.31.3.2:2223"],
        )
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertEqual(os.listdir(self.out.parent), ["guest-ssh-config"])

    def test_the_file_is_private_when_it_replaces_another(self) -> None:
        manifest = self.manifest({})
        self.write("amd-bench/guest-ssh-config", "Host old\n").chmod(0o644)
        previous = os.umask(0)
        try:
            code, _, _ = self.config(manifest, "guest")
        finally:
            os.umask(previous)
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(self.out.stat().st_mode), 0o600)
        self.assertTrue(self.out.read_text().startswith("Host guest\n"))
        self.assertEqual(len(self.out.read_text().splitlines()), 12)

    def test_the_guests_must_be_one_for_each_alias(self) -> None:
        two = self.manifest({}, {})
        for aliases in (("guest",), ("guest1", "guest2", "guest3")):
            code, _, err = self.config(two, *aliases)
            self.assertEqual(code, 2, aliases)
            self.assertIn(f"{len(aliases)} alias(es) need the guests 1 to", err)
            self.assertFalse(self.out.exists())
        for indexes in ((1, 3), (1, 1), (0, 1), (2, 3), ("1", "2"), (True, 2)):
            manifest = self.manifest(*({"index": index} for index in indexes))
            code, _, err = self.config(manifest, "guest1", "guest2")
            self.assertEqual(code, 2, indexes)
            self.assertIn("each once", err)
        code, _, err = self.config(self.manifest(), "guest")
        self.assertEqual(code, 2)

    def test_a_guest_that_cannot_be_reached_as_described_is_refused(self) -> None:
        absent = str(self.tmp / "keys" / "absent")
        for change, field in (
            ({"ssh_host": "a b"}, "ssh_host"),
            ({"ssh_host": "host;id"}, "ssh_host"),
            ({"ssh_host": ""}, "ssh_host"),
            ({"ssh_host": None}, "ssh_host"),
            ({"ssh_port": 0}, "ssh_port"),
            ({"ssh_port": 65536}, "ssh_port"),
            ({"ssh_port": "2222"}, "ssh_port"),
            ({"ssh_port": True}, "ssh_port"),
            ({"username": "Root"}, "username"),
            ({"username": "a b"}, "username"),
            ({"username": "-oProxyCommand"}, "username"),
            ({"ssh_key_file": "keys/id_rsa"}, "ssh_key_file"),
            ({"ssh_key_file": "/tmp/a key"}, "ssh_key_file"),
            ({"ssh_key_file": absent}, "is not a file"),
        ):
            code, out, err = self.config(self.manifest({}, change), "guest1", "guest2")
            self.assertEqual(code, 2, change)
            self.assertIn("guest 2 of the manifest", err)
            self.assertIn(field, err)
            self.assertNotIn("guest1:", out)
            self.assertFalse(self.out.exists())

    def test_what_else_is_refused(self) -> None:
        manifest = self.manifest({}, {})
        self.assertEqual(self.config(manifest, "guest", "guest")[0], 2)
        code, out, err = self.config(manifest, "guest1", "g::error::x")
        self.assertEqual(code, 2)
        self.assertNotIn("error::x", out + err)
        for text in ('{"index": 1}', "[1, 2]", "", '[{"index": 1}, 7]'):
            bad = self.write("bad.json", text)
            self.assertEqual(self.config(bad, "guest")[0], 2, text)
        self.assertEqual(self.config(self.tmp / "absent.json", "guest")[0], 2)
        self.assertFalse(self.out.exists())


def stack(
    lane: str, subcommand: str, tmp: pathlib.Path, **more: str
) -> subprocess.CompletedProcess[str]:
    """One subcommand of amd_bench_stack.sh, as a step of the lane's job."""
    env = {
        "PATH": os.environ["PATH"],
        "PYTHONDONTWRITEBYTECODE": "1",
        "LANE": lane,
        "RUNNER_TEMP": str(tmp),
        "GITHUB_WORKSPACE": str(REPO),
        "GITHUB_OUTPUT": str(tmp / "github-output"),
        "BENCH_DIAG": str(tmp / "diag"),
        "QEMU_MINIMAL_REF": TOOL_REF,
        **more,
    }
    return subprocess.run(
        ["bash", str(SCRIPTS / "amd_bench_stack.sh"), subcommand],
        cwd=tmp,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def lane_files(lane: str) -> list[pathlib.Path]:
    return [OVERLAYS / COMMON, OVERLAYS / VM_LANES[lane][1]]


def effective(lane: str, element: str) -> str | None:
    return ci.effective([ci.read_layer(path) for path in lane_files(lane)], element)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class LaneLayersTest(Scratch):
    """What each VM lane hands the boot step, and what its overlays hold."""

    def layers(self, lane: str) -> dict[str, str]:
        done = stack(lane, "layers", self.tmp)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        written = (self.tmp / "github-output").read_text(encoding="utf-8")
        self.assertEqual(done.stdout, written)
        head, mark, rest = written.partition("layers<<AMD_BENCH_LAYERS_EOF\n")
        self.assertTrue(mark, written)
        body, end, tail = rest.partition("AMD_BENCH_LAYERS_EOF\n")
        self.assertEqual((end, tail), ("AMD_BENCH_LAYERS_EOF\n", ""))
        outputs = dict(line.split("=", 1) for line in head.splitlines())
        outputs["layers"] = body
        (self.tmp / "github-output").unlink()
        return outputs

    def test_every_vm_lane_names_its_stack_and_two_layers(self) -> None:
        for lane, (guests, _) in VM_LANES.items():
            with self.subTest(lane=lane):
                outputs = self.layers(lane)
                self.assertEqual(sorted(outputs), ["guests", "layers", "stack-dir"])
                self.assertEqual(
                    outputs["stack-dir"], f"/var/lib/qemu-tool/stacks/nixl-bench-{lane}"
                )
                self.assertEqual(outputs["guests"], str(guests))
                paths = [pathlib.Path(line) for line in outputs["layers"].splitlines()]
                self.assertEqual(paths, [path.resolve() for path in lane_files(lane)])
                self.assertTrue(all(path.is_file() for path in paths))
                code, out, err = call("fleet-pins", *flags("--overlay", paths))
                self.assertEqual((code, err), (0, ""))
                self.assertEqual(len(out.splitlines()), 6)

    def test_the_lanes_overlays_give_the_fleet_its_shape_and_nothing_else(self) -> None:
        common = ci.read_layer(OVERLAYS / COMMON)
        self.assertEqual(common.repeated, ())
        self.assertLessEqual(set(ci.PIN_ELEMENTS), set(common.values))
        for lane, (guests, name) in VM_LANES.items():
            with self.subTest(lane=lane):
                own = ci.read_layer(OVERLAYS / name)
                self.assertEqual(own.repeated, ())
                # Only the common overlay names an image.
                self.assertEqual(set(own.values) & set(ci.PIN_ELEMENTS), set())
                self.assertNotIn("sha256:", own.data.decode("utf-8"))
                self.assertEqual(effective(lane, "vm-nvme-vram"), "false")
                self.assertEqual(effective(lane, "sdxi-per-vm"), "0")
                self.assertEqual(effective(lane, "vm-count"), str(guests))
                self.assertEqual(own.values["vm-count"], str(guests))
        for lane in FABRIC_LANES:
            self.assertEqual(effective(lane, "vm-count"), "1")
            self.assertEqual(effective(lane, "ernic-tap"), "false")
            self.assertEqual(effective(lane, "p4-enable"), "false")
        self.assertEqual(effective("vm2-net", "ernic-tap"), "true")
        self.assertEqual(effective("vm1-local", "vm-nvme"), "2")

    def test_a_lane_without_guests_has_no_layers(self) -> None:
        for lane in ("bench-container", "vm3-none"):
            done = stack(lane, "layers", self.tmp)
            self.assertNotEqual(done.returncode, 0, lane)
            self.assertFalse((self.tmp / "github-output").exists())
            self.assertNotIn("vm3", done.stdout + done.stderr)
            self.assertNotIn("stack-dir=", done.stdout)

    def test_the_pins_reach_the_run_meta(self) -> None:
        lane = "vm1-loop"
        done = stack(lane, "pins", self.tmp)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        work = self.tmp / "amd-bench"
        (work / "env-set").write_text("hosts.guest.ssh_config=/t/cfg\n")
        done = stack(
            lane,
            "render",
            self.tmp,
            RUN_META=json.dumps(BASE_META),
            ROCM_VERSION="10.2.0",
            GITHUB_RUN_ID="987",
            GITHUB_RUN_ATTEMPT="1",
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        meta = json.loads((work / "run-meta.json").read_text())
        files = lane_files(lane)
        whole = hashlib.sha256(b"".join(path.read_bytes() for path in files))
        common = ci.read_layer(OVERLAYS / COMMON).values
        self.assertEqual(
            {key: meta[key] for key in meta if key not in BASE_META},
            {
                "bench_run_id": "987",
                "run_attempt": 1,
                "rocm_version": "10.2.0",
                "runner_image": common["qemu-ref"],
                "overlay_sha256": whole.hexdigest(),
                "guest_image_digest": common["backing-ref"].rpartition("@")[2],
                "qemu_digest": common["qemu-ref"].rpartition("@")[2],
                "ernic_digest": common["ernic-ref"].rpartition("@")[2],
                "rocjitsu_digest": common["rocjitsu-ref"].rpartition("@")[2],
                "qemu_minimal_ref": TOOL_REF,
                "ernic_backend": "loopback",
            },
        )
        matrix = lib.load_matrix(SCRIPTS.parent / "bench")
        record = run.first_run_record(meta, matrix, matrix.lanes[lane], "nightly")
        self.assertEqual(lib.validate_record(record, lib.load_schema()), [])
        self.assertIn(
            "--- before the boot", (self.tmp / "diag" / "runner.txt").read_text()
        )

    def test_a_lane_that_ends_before_the_render_leaves_its_pins(self) -> None:
        lane = "vm1-local"
        if os.path.exists(f"/var/lib/qemu-tool/stacks/nixl-bench-{lane}"):
            self.skipTest("this host has the lane's stack")
        done = stack(lane, "pins", self.tmp)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        # The stack never came up: a docker that knows no container.
        docker = self.write("bin/docker", "#!/bin/sh\nexit 1\n")
        docker.chmod(0o755)
        path = f"{docker.parent}{os.pathsep}{os.environ['PATH']}"
        done = stack(lane, "diagnostics", self.tmp, PATH=path)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        diag = self.tmp / "diag"
        self.assertEqual(
            (diag / "meta-set").read_text(),
            (self.tmp / "amd-bench" / "meta-set").read_text(),
        )
        self.assertIn(f"qemu_minimal_ref={TOOL_REF}\n", (diag / "meta-set").read_text())
        self.assertFalse((diag / "run-meta.json").exists())


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class FleetCheckTest(Scratch):
    """'fleet-check' up to where it first asks docker: what the boot reports."""

    LANE = "vm2-net"

    def setUp(self) -> None:
        super().setUp()
        common = ci.read_layer(OVERLAYS / COMMON).values
        self.tags = {name: common[name].rpartition("@")[0] for name in ci.PIN_ELEMENTS}
        self.overlays = [str(path.resolve()) for path in lane_files(self.LANE)]

    def base(self, **changes: str) -> str:
        tags = {**self.tags}
        tags.update({key.replace("_", "-"): value for key, value in changes.items()})
        elements = [("vm-count", "2"), *tags.items(), ("vm-nvme-vram", "true")]
        return str(self.write("tool/stacks/rocm-golden.xml", fleet(*elements)))

    def check(self, **changes: str) -> subprocess.CompletedProcess[str]:
        reported = {"VM_COUNT": "2", **changes}
        # Not when a test wrote a base of its own: there is one file of the name.
        if "FLEET_XML" not in reported:
            reported["FLEET_XML"] = "\n".join([self.base(), *self.overlays])
        return stack(self.LANE, "fleet-check", self.tmp, **reported)

    def refused(self, *wanted: str, **changes: str) -> str:
        done = self.check(**changes)
        self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
        errors = [line for line in done.stdout.splitlines() if "::error" in line]
        self.assertEqual(len(errors), 1, done.stdout)
        self.assertTrue(errors[0].startswith("::error title=AMD bench vm2-net::"))
        for text in wanted:
            self.assertIn(text, errors[0])
        return errors[0]

    def test_layers_that_are_not_the_lanes_are_refused(self) -> None:
        base = self.base()
        local = str((OVERLAYS / "fleet-overlay-bench-local.xml").resolve())
        self.refused("'not set'", "rocm-golden.xml", FLEET_XML="")
        self.refused("rocm-golden.xml", FLEET_XML="\n".join(self.overlays))
        # An xml-overlay input that reached the action empty.
        self.refused("rendered 0 layer(s)", "the lane has 2", FLEET_XML=base)
        self.refused(
            "rendered 1 layer(s)", FLEET_XML="\n".join([base, self.overlays[0]])
        )
        self.refused(
            "rendered 3 layer(s)", FLEET_XML="\n".join([base, *self.overlays, local])
        )
        self.refused(
            "layer 3 of the boot",
            f"'{local}'",
            self.overlays[1],
            FLEET_XML="\n".join([base, self.overlays[0], local]),
        )
        self.refused(
            "layer 2 of the boot", FLEET_XML="\n".join([base, *self.overlays[::-1]])
        )
        saved = (self.tmp / "diag" / "fleet-layers.txt").read_text()
        self.assertEqual(saved.splitlines(), [base, *self.overlays[::-1]])

    def test_a_base_that_names_another_image_is_refused(self) -> None:
        older = "registry.invalid/guest:20251231.g3"
        text = self.refused(
            "fleet-overlay-common.xml and QEMU_MINIMAL_REF were not moved together",
            "<backing-ref>",
            f"'{older}'",
            self.tags["backing-ref"] + "@sha256:",
            FLEET_XML="\n".join([self.base(backing_ref=older), *self.overlays]),
        )
        self.assertNotIn("<qemu-ref>", text)
        kept = (self.tmp / "diag" / "fleet-base.xml").read_text()
        self.assertIn(older, kept)

    def test_another_number_of_guests_is_refused(self) -> None:
        self.refused("'1' guest(s)", "has 2", VM_COUNT="1")
        self.refused("'no' guest(s)", VM_COUNT="")

    def test_what_the_boot_reports_is_checked_before_docker_is_asked(self) -> None:
        compose = f"/var/lib/qemu-tool/stacks/nixl-bench-{self.LANE}/docker-compose.yml"
        if os.path.exists(compose):
            self.skipTest("this host has the lane's stack")
        self.refused("the boot left no " + compose)
        diag = self.tmp / "diag"
        self.assertEqual(
            (diag / "fleet-base-check.txt").read_text().splitlines(),
            [f"{name}: {self.tags[name]}" for name in ci.PIN_ELEMENTS],
        )
        self.assertEqual(len((diag / "fleet-layers.txt").read_text().splitlines()), 3)

    def test_a_reported_value_starts_no_workflow_command(self) -> None:
        text = self.refused(VM_COUNT="x\n::warning::y")
        self.assertIn("'x : :warning: :y' guest(s)", text)


class WheelTestOverlaysTest(unittest.TestCase):
    """The overlays of the rocjitsu wheel tests, which the bench lanes do not boot."""

    def test_they_name_no_image(self) -> None:
        for name in WHEEL_TESTS.values():
            layer = ci.read_layer(OVERLAYS / name)
            self.assertEqual(set(ci.PIN_ELEMENTS) & set(layer.values), set(), name)

    def test_each_workflow_lays_its_own_over_the_common_one(self) -> None:
        for workflow, name in WHEEL_TESTS.items():
            text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
            layers = re.findall(
                r"^ +\$\{\{ github\.workspace \}\}/\.ci/rocjitsu/(\S+)$", text, re.M
            )
            self.assertEqual(layers, [COMMON, name], workflow)

    def test_no_bench_lane_passes_them(self) -> None:
        text = (SCRIPTS / "amd_bench_stack.sh").read_text(encoding="utf-8")
        for name in WHEEL_TESTS.values():
            self.assertNotIn(f"/{name}", text)
        for _, name in VM_LANES.values():
            self.assertIn(f".ci/rocjitsu/{name}", text)
        self.assertIn(f".ci/rocjitsu/{COMMON}", text)


class WorkflowTextTest(unittest.TestCase):
    """What the workflows ask of the helpers, read as text."""

    def steps(self, name: str) -> list[str]:
        text = (WORKFLOWS / name).read_text(encoding="utf-8")
        return re.findall(r"^ {4}- name: (.+)$", text, flags=re.MULTILINE)

    def test_the_records_are_uploaded_before_the_stack_is_taken_down(self) -> None:
        steps = self.steps("amd-bench-vm.yml")
        order = ["Upload the records", "Take the stack down", "Upload diagnostics"]
        for step in order:
            self.assertEqual(steps.count(step), 1, step)
        self.assertEqual([step for step in steps if step in order], order)

    def test_the_workflows_boot_from_the_same_pins(self) -> None:
        texts = [
            (WORKFLOWS / name).read_text(encoding="utf-8") for name in VM_WORKFLOWS
        ]
        for pattern in (
            r"uses: sbates130272/qemu-tool-action@(\S+ # \S+)$",
            r"^  QEMU_MINIMAL_REF: (\S+ # \S+)$",
        ):
            found = [re.findall(pattern, text, flags=re.MULTILINE) for text in texts]
            self.assertTrue(all(found), pattern)
            pins = {pin for each in found for pin in each}
            self.assertEqual(len(pins), 1, found)
            # A commit and the release it is, not a tag or a branch.
            self.assertRegex(pins.pop(), r"^[0-9a-f]{40} # v[0-9.]+$")

    def test_every_subcommand_a_workflow_runs_exists(self) -> None:
        script = (SCRIPTS / "amd_bench_stack.sh").read_text(encoding="utf-8")
        have = set(re.findall(r"^  ([a-z-]+)\) cmd_", script, flags=re.MULTILINE))
        self.assertLessEqual({"layers", "fleet-check", "ssh", "render"}, have)
        self.assertEqual(have & {"up", "down"}, set())
        helper = set(
            re.findall(r"^    amd_bench_ci\.py ([a-z-]+)", ci.__doc__ or "", re.M)
        )
        self.assertLessEqual({"plan", "fleet-pins", "ssh-config"}, helper)
        for path in sorted(WORKFLOWS.glob("amd-*.yml")):
            text = path.read_text(encoding="utf-8")
            asked = set(
                re.findall(r"bash \.ci/scripts/amd_bench_stack\.sh ([a-z-]+)", text)
            )
            self.assertLessEqual(asked, have, path.name)
            asked = set(
                re.findall(r"python3 \.ci/scripts/amd_bench_ci\.py ([a-z-]+)", text)
            )
            self.assertLessEqual(asked, helper, path.name)


if __name__ == "__main__":
    unittest.main()
