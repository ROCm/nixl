# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests of amd_bench_ci.py and of what amd_bench_stack.sh renders with it.

    python3 -m unittest discover -s .ci/scripts/tests -v

fixtures/bench_ci holds what a finished nightly leaves behind, all synthetic:
a run, its artifact listing, the manifests of two nixlbench wheels (feat-a
with every ROCm version, feat-b without 10.1), the nightly's summary, and a
matrix of the five lanes with env templates. RealMatrixTest and
StackRenderTest read the real matrix under .ci/bench instead, with the values
the lane jobs set. No network, no docker, no ssh.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import shutil
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
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures" / "bench_ci"
MATRIX = FIXTURES / "matrix"
REAL_MATRIX = SCRIPTS.parent / "bench"
WHEEL_A = "nixlbench-rocm-wheel-feat-a"
VM_LANES = ["vm1-local", "vm2-net", "vm1-loop", "vm1-fabric"]
# What the lane jobs set in each env template: amd_bench_stack.sh, and
# nothing for the container lane.
JOB_SETS = {
    "bench-container": [],
    "vm1-local": ["hosts.guest.ssh_config=/t/cfg", "vars.gusli_blkdev=/dev/nvme2n1"],
    "vm2-net": [
        "hosts.guest1.ssh_config=/t/cfg",
        "hosts.guest2.ssh_config=/t/cfg",
        "hosts.guest1.vars.ucx_net_devices=enp0s5,ernic0:1",
        "hosts.guest2.vars.ucx_net_devices=enp0s5,ernic0:1",
    ],
    "vm1-loop": ["hosts.guest.ssh_config=/t/cfg"],
    "vm1-fabric": ["hosts.guest.ssh_config=/t/cfg", "vars.gusli_blkdev=/dev/nvme1n2"],
}
JOB_SETS_ARGS = [arg for item in JOB_SETS["vm1-local"] for arg in ("--set", item)]
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


class Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="amd-bench-ci-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write_json(self, name: str, value: Any) -> pathlib.Path:
        path = self.tmp / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def read_json(self, path: pathlib.Path) -> Any:
        return json.loads(path.read_text(encoding="utf-8"))


class PlanTest(Scratch):
    def plan(self, **changes: Any) -> tuple[int, dict[str, Any], str, str]:
        """(exit status, the outputs written, stdout, stderr) of one plan."""
        options: dict[str, Any] = {
            "matrix": MATRIX,
            "run": FIXTURES / "run.json",
            "artifacts": FIXTURES / "artifacts.jsonl",
            "manifests": FIXTURES / "manifests",
            "summary": FIXTURES / "nightly-summary.json",
            "lanes": "",
            "tier": "",
            "only": "",
            "event": "workflow_run",
        }
        options.update(changes)
        output = self.tmp / "github-output"
        output.unlink(missing_ok=True)
        argv = ["plan", "--output", str(output)]
        for name, value in options.items():
            if value is not None:
                argv.append(f"--{name.replace('_', '-')}={value}")
        code, out, err = call(*argv)
        outputs: dict[str, Any] = {}
        if output.exists():
            for line in output.read_text(encoding="utf-8").splitlines():
                key, _, value = line.partition("=")
                self.assertNotIn(key, outputs)
                outputs[key] = value
            for key in ("container", "vm", "collect"):
                outputs[key] = json.loads(outputs[key])["include"]
        return code, outputs, out, err

    def manifests_with(self, **changes: Any) -> pathlib.Path:
        """A copy of the manifests in which feat-a's has the changes."""
        target = self.tmp / "manifests"
        shutil.copytree(FIXTURES / "manifests", target)
        path = target / WHEEL_A / ci.MANIFEST
        manifest = self.read_json(path)
        manifest.update(changes)
        path.write_text(json.dumps(manifest), encoding="utf-8")
        return target

    def matrix_with(self, lane: str, text: str | None) -> pathlib.Path:
        """A copy of the matrix in which a lane file is rewritten or gone."""
        target = self.tmp / "matrix"
        shutil.copytree(MATRIX, target)
        path = target / "lanes" / f"{lane}.toml"
        if text is None:
            path.unlink()
        else:
            path.write_text(text, encoding="utf-8")
        return target

    def test_workflow_run_with_empty_inputs_plans_every_lane_at_nightly(self) -> None:
        code, outputs, out, _ = self.plan()
        self.assertEqual(code, 0)
        self.assertEqual(outputs["tier"], "nightly")
        self.assertEqual(outputs["only"], "")
        self.assertEqual(outputs["count"], "2")
        self.assertEqual(
            [(e["slug"], e["rocm"]) for e in outputs["container"]],
            [
                ("feat-a", "7.14.1"),
                ("feat-a", "10.1.0"),
                ("feat-a", "10.2.0"),
                ("feat-b", "7.14.1"),
                ("feat-b", "10.2.0"),
            ],
        )
        # The guest carries ROCm 10.2, and both wheels have that backend.
        self.assertEqual(
            [(e["slug"], e["lane"], e["rocm"]) for e in outputs["vm"]],
            [
                (slug, lane, "10.2.0")
                for slug in ("feat-a", "feat-b")
                for lane in VM_LANES
            ],
        )
        self.assertNotIn("Bench VM lanes not run", out)
        vm_lanes = "vm1-local=10.2.0 vm2-net=10.2.0 vm1-loop=10.2.0 vm1-fabric=10.2.0"
        self.assertEqual(
            [(e["slug"], e["expect"]) for e in outputs["collect"]],
            [
                ("feat-a", f"bench-container=7.14.1,10.1.0,10.2.0 {vm_lanes}"),
                ("feat-b", f"bench-container=7.14.1,10.2.0 {vm_lanes}"),
            ],
        )
        element = outputs["vm"][0]
        self.assertEqual(element["suffix"], "-feat-a")
        self.assertEqual(element["branch"], "for-upstream/feat/a")
        self.assertEqual(element["nixl-artifact"], "nixl-rocm-wheels-feat-a")
        self.assertEqual(element["nixlbench-artifact"], WHEEL_A)
        meta = json.loads(element["meta"])
        self.assertEqual(meta, {**BASE_META, "upstream_main_sha": "a" * 40})
        self.assertEqual(set(meta) - set(lib.META_KEYS), set())
        self.assertEqual(json.loads(outputs["collect"][0]["meta"]), meta)

    def test_a_wheel_without_the_guests_backend_gets_no_vm_lane(self) -> None:
        code, outputs, out, _ = self.plan(guest_rocm="10.1")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["count"], "2")
        self.assertEqual(len(outputs["container"]), 5)
        # feat-b has no backend for a guest with ROCm 10.1, so no VM lane.
        self.assertEqual(
            [(e["slug"], e["lane"], e["rocm"]) for e in outputs["vm"]],
            [("feat-a", lane, "10.1.0") for lane in VM_LANES],
        )
        self.assertIn(
            "Bench VM lanes not run::feat-b: its nixlbench wheel has no ROCm 10.1 "
            "backend",
            out,
        )
        self.assertEqual(
            [(e["slug"], e["expect"]) for e in outputs["collect"]],
            [
                (
                    "feat-a",
                    "bench-container=7.14.1,10.1.0,10.2.0 vm1-local=10.1.0 "
                    "vm2-net=10.1.0 vm1-loop=10.1.0 vm1-fabric=10.1.0",
                ),
                ("feat-b", "bench-container=7.14.1,10.2.0"),
            ],
        )
        self.assertEqual(self.plan(guest_rocm="10")[0], 2)

    def test_allow_failure_is_a_json_boolean(self) -> None:
        _, outputs, _, _ = self.plan()
        self.assertEqual(len(outputs["vm"]), 2 * len(VM_LANES))
        allowed = {e["lane"]: e["allow-failure"] for e in outputs["vm"]}
        self.assertEqual(
            allowed,
            {"vm1-local": False, "vm2-net": True, "vm1-loop": True, "vm1-fabric": True},
        )
        for element in outputs["vm"] + outputs["container"]:
            self.assertIs(type(element["allow-failure"]), bool)
            for key, value in element.items():
                if key != "allow-failure":
                    self.assertIs(type(value), str, key)
        self.assertTrue(all(e["allow-failure"] is False for e in outputs["container"]))

    def test_a_listing_of_more_than_100_lines_is_read_to_the_end(self) -> None:
        lines = [
            json.dumps({"id": n, "name": f"rocm-check-results-{n}", "expired": False})
            for n in range(1, 151)
        ]
        lines += (FIXTURES / "artifacts.jsonl").read_text().splitlines()
        listing = self.tmp / "artifacts.jsonl"
        listing.write_text("\n".join(lines) + "\n\n", encoding="utf-8")
        code, outputs, _, _ = self.plan(artifacts=listing)
        self.assertEqual(code, 0)
        self.assertEqual(outputs["count"], "2")

    def test_of_a_name_listed_twice_the_highest_id_counts(self) -> None:
        # In the fixture the older upload has expired and the newer has not.
        _, outputs, _, _ = self.plan()
        self.assertIn("feat-a", [e["slug"] for e in outputs["collect"]])
        text = (FIXTURES / "artifacts.jsonl").read_text()
        newer = {"id": 999, "name": WHEEL_A, "expired": True}
        listing = self.tmp / "artifacts.jsonl"
        listing.write_text(json.dumps(newer) + "\n" + text, encoding="utf-8")
        code, outputs, out, _ = self.plan(artifacts=listing)
        self.assertEqual(code, 0)
        self.assertEqual([e["slug"] for e in outputs["collect"]], ["feat-b"])
        self.assertIn(f"{WHEEL_A}: the artifact has expired", out)

    def test_concatenated_json_documents_are_refused(self) -> None:
        # Written here, not kept as a fixture: it is not a JSON document.
        pages = self.tmp / "artifacts-concatenated"
        pages.write_text(
            "".join(
                json.dumps({"total_count": 2, "artifacts": [entry]})
                for entry in (
                    {"id": 110, "name": "nixl-rocm-wheels-feat-a", "expired": False},
                    {"id": 111, "name": WHEEL_A, "expired": False},
                )
            )
            + "\n",
            encoding="utf-8",
        )
        code, outputs, _, err = self.plan(artifacts=pages)
        self.assertEqual(code, 2)
        self.assertEqual(outputs, {})
        self.assertIn("one JSON object per line", err)
        # One document on its line parses, and is still not an entry.
        page = self.write_json("page.json", {"total_count": 0, "artifacts": []})
        code, _, _, err = self.plan(artifacts=page)
        self.assertEqual(code, 2)
        self.assertIn("page.json:1 is not an artifact entry", err)

    def test_the_pr_tier_drops_the_lanes_with_nothing_to_run(self) -> None:
        code, outputs, out, _ = self.plan(tier="pr", event="workflow_dispatch")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["tier"], "pr")
        self.assertEqual({e["lane"] for e in outputs["vm"]}, {"vm1-local"})
        self.assertEqual(len(outputs["container"]), 5)
        self.assertEqual(
            outputs["collect"][0]["expect"],
            "bench-container=7.14.1,10.1.0,10.2.0 vm1-local=10.2.0",
        )
        # vm1-loop has a case in the tier, but a blocked one.
        for lane in ("vm2-net", "vm1-loop", "vm1-fabric"):
            self.assertIn(
                f"Bench lane not run::{lane} has no active case in the pr tier", out
            )
        meta = json.loads(outputs["vm"][0]["meta"])
        self.assertEqual((meta["tier"], meta["reason"]), ("pr", "dispatch"))

    def test_the_weekly_tier_is_planned_like_the_nightly(self) -> None:
        code, outputs, _, _ = self.plan(
            tier="weekly", lanes="vm2-net vm1-loop", event="workflow_dispatch"
        )
        self.assertEqual(code, 0)
        self.assertEqual(outputs["tier"], "weekly")
        self.assertEqual({e["lane"] for e in outputs["vm"]}, {"vm2-net", "vm1-loop"})
        self.assertEqual(outputs["container"], [])
        meta = json.loads(outputs["vm"][0]["meta"])
        self.assertEqual((meta["tier"], meta["reason"]), ("weekly", "dispatch"))

    def test_a_lane_that_is_blocked_or_absent_is_dropped(self) -> None:
        text = (MATRIX / "lanes" / "vm1-fabric.toml").read_text()
        blocked = text.replace(
            'topology = "single"',
            'topology = "single"\nstatus = "blocked"\nblocked_by = ["W9"]',
        )
        code, outputs, out, _ = self.plan(
            matrix=self.matrix_with("vm1-fabric", blocked)
        )
        self.assertEqual(code, 0)
        self.assertNotIn("vm1-fabric", {e["lane"] for e in outputs["vm"]})
        self.assertNotIn("vm1-fabric", outputs["collect"][0]["expect"])
        self.assertIn("vm1-fabric is blocked in the matrix", out)
        shutil.rmtree(self.tmp / "matrix")
        code, outputs, out, _ = self.plan(matrix=self.matrix_with("vm2-net", None))
        self.assertEqual(code, 0)
        self.assertNotIn("vm2-net", {e["lane"] for e in outputs["vm"]})
        self.assertIn("vm2-net is not in the matrix", out)

    def test_a_subset_of_the_lanes(self) -> None:
        code, outputs, _, _ = self.plan(lanes="vm2-net  bench-container")
        self.assertEqual(code, 0)
        self.assertEqual({e["lane"] for e in outputs["vm"]}, {"vm2-net"})
        self.assertEqual(
            outputs["collect"][0]["expect"],
            "bench-container=7.14.1,10.1.0,10.2.0 vm2-net=10.2.0",
        )
        # Only VM lanes, and for a 10.1 guest feat-b has none: it gets no
        # collect job either.
        code, outputs, _, _ = self.plan(lanes="vm1-local", guest_rocm="10.1")
        self.assertEqual(outputs["count"], "1")
        self.assertEqual(outputs["container"], [])
        self.assertEqual([e["slug"] for e in outputs["collect"]], ["feat-a"])

    def test_an_unknown_lane_is_refused_and_not_echoed(self) -> None:
        code, outputs, out, err = self.plan(lanes="vm1-local vm9-::error::x")
        self.assertEqual(code, 2)
        self.assertEqual(outputs, {})
        self.assertNotIn("vm9", out + err)
        self.assertIn("bench-container vm1-local vm2-net vm1-loop vm1-fabric", err)

    def test_a_tier_or_glob_that_is_not_one_is_refused(self) -> None:
        self.assertEqual(self.plan(tier="monthly")[0], 2)
        self.assertEqual(self.plan(only="vm1-local/* $(id)")[0], 2)
        # The runner would take a glob that starts with a dash for an option.
        for bad in ("-x*", "vm1-local/* --tier", "-"):
            code, outputs, _, err = self.plan(only=bad)
            self.assertEqual((code, outputs), (2, {}), bad)
            self.assertIn("none starting with a dash", err)
        code, outputs, _, _ = self.plan(only=" vm1-local/ucx/*   */[DV]D/?RITE/S ")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["only"], "vm1-local/ucx/* */[DV]D/?RITE/S")

    def test_a_run_without_a_numeric_id_is_refused(self) -> None:
        source = self.read_json(FIXTURES / "run.json")
        for bad in ("4242", None, True, 0):
            path = self.write_json("run.json", {**source, "id": bad})
            code, outputs, _, err = self.plan(run=path)
            self.assertEqual(code, 2, bad)
            self.assertEqual(outputs, {})
            self.assertIn("no numeric 'id'", err)
        self.assertEqual(self.plan(run=self.tmp / "absent.json")[0], 2)

    def test_a_manifest_of_another_run_or_suffix_is_not_planned(self) -> None:
        for change, reason in (
            ({"workflow_run_id": 4243}, "written by another workflow run"),
            ({"artifact_suffix": "-feat-b"}, "written for another artifact suffix"),
            ({"artifact_suffix": ""}, "written for another artifact suffix"),
            ({"schema": 2}, "not a schema 1 nixlbench-build.json"),
            ({"sha256": "abc"}, "no usable 'sha256'"),
            ({"rocm": []}, "lists no ROCm version"),
            ({"ref": "a/../b"}, "no usable 'ref'"),
            ({"nixl_wheels_artifact": "nixl-rocm-wheels-main"}, "another NIXL wheel"),
        ):
            code, outputs, out, _ = self.plan(manifests=self.manifests_with(**change))
            self.assertEqual(code, 0, change)
            self.assertEqual([e["slug"] for e in outputs["collect"]], ["feat-b"])
            self.assertNotIn("feat-a", [e["slug"] for e in outputs["container"]])
            self.assertIn(f"Bench branch not run::{WHEEL_A}: its manifest", out)
            self.assertIn(reason, out)
            shutil.rmtree(self.tmp / "manifests")

    def test_a_wheel_without_its_nixl_wheels_is_not_planned(self) -> None:
        lines = (FIXTURES / "artifacts.jsonl").read_text().splitlines()
        listing = self.tmp / "artifacts.jsonl"
        kept = [line for line in lines if '"nixl-rocm-wheels-feat-a"' not in line]
        listing.write_text("\n".join(kept) + "\n", encoding="utf-8")
        code, outputs, out, _ = self.plan(artifacts=listing)
        self.assertEqual(code, 0)
        self.assertEqual([e["slug"] for e in outputs["collect"]], ["feat-b"])
        self.assertIn("the run has no artifact nixl-rocm-wheels-feat-a", out)

    def test_a_lone_artifact_extracted_without_its_directory(self) -> None:
        lone = self.tmp / "lone"
        lone.mkdir()
        shutil.copy(FIXTURES / "manifests" / WHEEL_A / ci.MANIFEST, lone)
        lines = (FIXTURES / "artifacts.jsonl").read_text().splitlines()
        listing = self.tmp / "artifacts.jsonl"
        listing.write_text(
            "\n".join(line for line in lines if "feat-b" not in line) + "\n"
        )
        code, outputs, _, _ = self.plan(artifacts=listing, manifests=lone)
        self.assertEqual(code, 0)
        self.assertEqual([e["slug"] for e in outputs["collect"]], ["feat-a"])
        # With both wheels listed, the lone file serves only the one it names.
        code, outputs, out, _ = self.plan(manifests=lone)
        self.assertEqual([e["slug"] for e in outputs["collect"]], ["feat-a"])
        self.assertIn("nixlbench-rocm-wheel-feat-b: its manifest was written", out)

    def test_a_dispatched_wheels_run_has_no_suffix_and_no_summary(self) -> None:
        manifests = self.tmp / "manifests" / ci.NIXLBENCH_ARTIFACT
        manifests.mkdir(parents=True)
        manifest = self.read_json(FIXTURES / "manifests" / WHEEL_A / ci.MANIFEST)
        manifest.update(artifact_suffix="", nixl_wheels_artifact="nixl-rocm-wheels")
        (manifests / ci.MANIFEST).write_text(json.dumps(manifest))
        listing = self.tmp / "artifacts.jsonl"
        listing.write_text(
            '{"id":1,"name":"nixl-rocm-wheels","expired":false}\n'
            '{"id":2,"name":"nixlbench-rocm-wheel","expired":false}\n'
            '{"id":3,"name":"nixlbench-rocm-backend-10.1.0","expired":false}\n'
        )
        code, outputs, _, _ = self.plan(
            artifacts=listing,
            manifests=self.tmp / "manifests",
            summary=None,
            event="workflow_dispatch",
        )
        self.assertEqual(code, 0)
        self.assertEqual(outputs["count"], "1")
        element = outputs["vm"][0]
        self.assertEqual(element["suffix"], "")
        self.assertEqual(element["slug"], "for-upstream-feat-a")
        self.assertEqual(element["nixlbench-artifact"], "nixlbench-rocm-wheel")
        meta = json.loads(element["meta"])
        # The stamp is the time the source run was created.
        self.assertEqual(meta["stamp"], "20261119T032345Z")
        self.assertEqual(meta["reason"], "dispatch")
        self.assertIsNone(meta["upstream_main_sha"])

    def test_the_summary_of_another_run_is_not_used(self) -> None:
        summary = self.read_json(FIXTURES / "nightly-summary.json")
        path = self.write_json("summary.json", {**summary, "run_id": 1})
        code, outputs, out, _ = self.plan(summary=path)
        self.assertEqual(code, 0)
        self.assertEqual(
            json.loads(outputs["vm"][0]["meta"])["stamp"], "20261119T032345Z"
        )
        self.assertIn("the nightly summary is of another run", out)
        code, outputs, _, _ = self.plan(summary=self.tmp / "absent.json")
        self.assertEqual(code, 0)
        self.assertEqual(outputs["count"], "2")

    def test_at_most_max_branches_are_planned(self) -> None:
        code, outputs, out, _ = self.plan(max_branches=1)
        self.assertEqual(code, 0)
        self.assertEqual([e["slug"] for e in outputs["collect"]], ["feat-a"])
        self.assertIn("feat-b: one run benches at most 1 branches", out)

    def test_a_run_with_no_nixlbench_wheel_plans_nothing(self) -> None:
        listing = self.tmp / "artifacts.jsonl"
        listing.write_text(
            '{"id":130,"name":"nixl-rocm-wheels-main","expired":false}\n'
        )
        code, outputs, out, _ = self.plan(
            artifacts=listing, manifests=self.tmp / "absent"
        )
        self.assertEqual(code, 0)
        self.assertEqual(outputs["count"], "0")
        for key in ("container", "vm", "collect"):
            self.assertEqual(outputs[key], [])
        self.assertIn("::notice title=Nothing to bench::", out)

    def test_run_as_a_subprocess_the_way_the_workflows_do(self) -> None:
        output = self.tmp / "github-output"
        done = subprocess.run(
            [
                "python3",
                str(SCRIPTS / "amd_bench_ci.py"),
                "plan",
                "--matrix",
                str(MATRIX),
                "--run",
                str(FIXTURES / "run.json"),
                "--artifacts",
                str(FIXTURES / "artifacts.jsonl"),
                "--manifests",
                str(FIXTURES / "manifests"),
                "--lanes=",
                "--tier=",
                "--only=",
                "--event",
                "workflow_run",
                "--guest-rocm",
                "10.2",
                "--max-branches",
                "4",
                "--output",
                str(output),
            ],
            cwd=self.tmp,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("count=2\n", output.read_text(encoding="utf-8"))
        # With -I the script directory is not searched, so the library is lost.
        done = subprocess.run(
            ["python3", "-I", str(SCRIPTS / "amd_bench_ci.py"), "plan", "--help"],
            cwd=self.tmp,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("amd_bench_lib.py must sit beside this script", done.stderr)


class EnvTest(Scratch):
    def render(self, lane: str, *extra: str) -> tuple[int, str, str]:
        self.out = self.tmp / "env.json"
        return call(
            "env",
            "--matrix",
            str(MATRIX),
            "--lane",
            lane,
            "--out",
            str(self.out),
            *extra,
        )

    def test_the_job_values_are_set_and_a_need_withheld(self) -> None:
        code, out, _ = self.render(
            "vm1-local",
            "--set",
            "hosts.guest.ssh_config=/tmp/amd-bench/guest-ssh-config",
            "--set",
            "vars.gusli_blkdev=/nonexistent/amd-bench",
            "--withhold",
            "blkdev",
            "--withhold",
            "rdma",
        )
        self.assertEqual(code, 0)
        self.assertIn("withheld blkdev", out)
        env = run.load_env(self.out)
        self.assertEqual(env.env_class, "gha-kvm-vm1")
        self.assertEqual(env.provides, ("gpu", "io_uring"))
        host = env.hosts["guest"]
        self.assertEqual(host.ssh_config, "/tmp/amd-bench/guest-ssh-config")
        self.assertEqual(env.vars["gusli_blkdev"], "/nonexistent/amd-bench")
        self.assertEqual([probe.name for probe in env.probes], ["gpu-kfd"])
        self.assertNotIn("@job", self.out.read_text())

    def test_host_variables_are_set(self) -> None:
        sets = [arg for item in JOB_SETS["vm2-net"] for arg in ("--set", item)]
        code, _, _ = self.render("vm2-net", *sets)
        self.assertEqual(code, 0)
        env = run.load_env(self.out)
        self.assertEqual(env.hosts["guest1"].vars["ucx_net_devices"], "enp0s5,ernic0:1")
        self.assertEqual(env.hosts["guest2"].vars["ip"], "192.168.100.12")

    def test_a_template_with_nothing_to_set(self) -> None:
        code, _, _ = self.render("bench-container", "--withhold", "io_uring")
        self.assertEqual(code, 0)
        self.assertEqual(run.load_env(self.out).provides, ("redis",))

    def test_a_job_value_left_unset_is_refused(self) -> None:
        code, _, err = self.render(
            "vm1-local", "--set", "hosts.guest.ssh_config=/t/cfg"
        )
        self.assertEqual(code, 2)
        self.assertIn("vars.gusli_blkdev", err)
        self.assertNotIn("hosts.guest.ssh_config", err)
        self.assertFalse(self.out.exists())
        code, _, err = self.render("vm2-net")
        self.assertEqual(code, 2)
        for path in JOB_SETS["vm2-net"]:
            self.assertIn(path.split("=")[0], err)

    def test_a_set_on_a_key_the_template_lacks_is_refused(self) -> None:
        for path in (
            "vars.ais_dir=/x",
            "hosts.guest9.ssh_config=/x",
            "hosts.guest.vars.ip=1.2.3.4",
        ):
            code, _, err = self.render("vm1-local", *JOB_SETS_ARGS, "--set", path)
            self.assertEqual(code, 2, path)
            self.assertIn("the template has no such key", err)
        for path in (
            "provides=x",
            "hosts.guest.kind=local",
            "hosts.guest.nixlbench=/bin/true",
            "vars=x",
            "vars.a.b=x",
            "roles.init=guest",
        ):
            code, _, err = self.render("vm1-local", *JOB_SETS_ARGS, "--set", path)
            self.assertEqual(code, 2, path)
            self.assertIn("a path is vars.NAME", err)
        self.assertEqual(self.render("vm1-local", "--set", "vars.gusli_blkdev")[0], 2)
        self.assertEqual(self.render("vm1-local", "--set", "vars.posix_dir=a\nb")[0], 2)

    def test_what_else_is_refused(self) -> None:
        self.assertEqual(self.render("vm1-local", *JOB_SETS_ARGS)[0], 0)
        code, _, err = self.render("vm1-local", *JOB_SETS_ARGS, "--withhold", "wifi")
        self.assertEqual(code, 2)
        self.assertIn("--withhold takes a need", err)
        # A lane of the matrix whose env class has no template, and no lane.
        self.assertEqual(self.render("vm1-loop")[0], 2)
        self.assertEqual(self.render("vm7-none")[0], 2)
        self.assertEqual(self.render("../lanes/vm1-local")[0], 2)
        code, _, err = call(
            "env",
            "--matrix",
            str(MATRIX),
            "--lane",
            "bench-container",
            "--out",
            "e.toml",
        )
        self.assertEqual(code, 2)
        self.assertIn(".json", err)


class MetaTest(Scratch):
    def test_the_job_keys_are_added_to_the_plans(self) -> None:
        out = self.tmp / "meta.json"
        code, _, _ = call(
            "meta",
            "--base",
            json.dumps(BASE_META),
            "--out",
            str(out),
            "--set",
            "bench_run_id=987",
            "--set-int",
            "run_attempt=2",
            "--set",
            "rocm_version=10.1.0",
            "--set",
            "ernic_backend=nvmeof:ip=192.168.100.1,size=1024M,bs=4096",
            "--set-int",
            "ib_mtu=1024",
        )
        self.assertEqual(code, 0)
        meta = self.read_json(out)
        self.assertEqual(
            meta,
            {
                **BASE_META,
                "bench_run_id": "987",
                "run_attempt": 2,
                "rocm_version": "10.1.0",
                "ernic_backend": "nvmeof:ip=192.168.100.1,size=1024M,bs=4096",
                "ib_mtu": 1024,
            },
        )
        self.assertIs(type(meta["bench_run_id"]), str)
        self.assertIs(type(meta["run_attempt"]), int)
        # What the runner and the collector take as --run-meta.
        self.assertEqual(run.load_meta(out), meta)

    def test_a_key_that_is_not_run_meta_is_refused(self) -> None:
        out = self.tmp / "meta.json"
        code, _, err = call(
            "meta", "--base", "{}", "--out", str(out), "--set", "lane=x"
        )
        self.assertEqual(code, 2)
        self.assertIn("not run-meta key(s): lane", err)
        code, _, err = call(
            "meta", "--base", '{"status": "complete"}', "--out", str(out)
        )
        self.assertEqual(code, 2)
        self.assertIn("status", err)
        code, _, _ = call(
            "meta", "--base", "{}", "--out", str(out), "--set-int", "env_fp=1"
        )
        self.assertEqual(code, 2)
        self.assertFalse(out.exists())

    def test_an_integer_that_is_not_one_is_refused(self) -> None:
        out = self.tmp / "meta.json"
        for value in ("1024 (3)", "", "1.5", "0x10", "ten"):
            code, _, err = call(
                "meta",
                "--base",
                "{}",
                "--out",
                str(out),
                "--set-int",
                f"ib_mtu={value}",
            )
            self.assertEqual(code, 2, value)
            self.assertIn("is not an integer", err)
        code, _, _ = call(
            "meta", "--base", "{}", "--out", str(out), "--set-int", "gpu_vram_limit=-1"
        )
        self.assertEqual(code, 0)
        self.assertEqual(self.read_json(out), {"gpu_vram_limit": -1})

    def test_a_base_that_is_not_an_object_is_refused(self) -> None:
        out = self.tmp / "meta.json"
        for base in ("", "[]", "{", '"x"'):
            self.assertEqual(
                call("meta", "--base", base, "--out", str(out))[0], 2, base
            )


class VramTimingTest(Scratch):
    def test_the_report_lists_every_vram_case(self) -> None:
        out = self.tmp / "diag" / "e1-vram-timing.txt"
        code, printed, _ = call(
            "vram-timing",
            "--records",
            str(FIXTURES / "records.ndjson"),
            "--out",
            str(out),
        )
        self.assertEqual(code, 0)
        text = out.read_text(encoding="utf-8")
        self.assertEqual(printed, text)
        self.assertIn("VRAM cases: 3; launched: 2; error_class asio: 1", text)
        self.assertIn(
            "first VRAM case launched: vm1-local/ucx/tcp/VV/WRITE/S: status pass, "
            "proc_wall_s init 9.500 tgt 8.250",
            text,
        )
        rows = [line.split("\t") for line in text.splitlines() if "\t" in line]
        self.assertEqual(
            rows[0][-2:], ["proc_wall_s.init", "proc_wall_s.tgt"], "the header"
        )
        self.assertEqual(
            rows[1:],
            [
                [
                    "vm1-local/ucx/tcp/VV/WRITE/S",
                    "head",
                    "0",
                    "pass",
                    "-",
                    "-",
                    "9.500",
                    "8.250",
                ],
                [
                    "vm1-local/ucx/tcp/DV/WRITE/S",
                    "head",
                    "0",
                    "crash",
                    "asio",
                    "-",
                    "16.125",
                    "17.500",
                ],
                [
                    "vm1-local/ucx/tcp/VD/WRITE/S",
                    "head",
                    "0",
                    "skip",
                    "-",
                    "device-suspect",
                    "-",
                    "-",
                ],
            ],
        )

    def test_it_never_fails(self) -> None:
        out = self.tmp / "e1.txt"
        code, _, _ = call(
            "vram-timing", "--records", str(self.tmp / "absent"), "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertEqual(len(out.read_text().splitlines()), 1)
        self.assertIn("no records at", out.read_text())
        # A directory where the file should be, and nowhere to write.
        blocker = self.tmp / "file"
        blocker.write_text("")
        code, printed, _ = call(
            "vram-timing",
            "--records",
            str(FIXTURES / "records.ndjson"),
            "--out",
            str(blocker / "e1.txt"),
        )
        self.assertEqual(code, 0)
        self.assertIn("VRAM cases: 3", printed)
        empty = self.tmp / "empty.ndjson"
        empty.write_text("")
        code, printed, _ = call(
            "vram-timing", "--records", str(empty), "--out", str(out)
        )
        self.assertEqual(code, 0)
        self.assertIn("first VRAM case launched: none", printed)


class RealMatrixTest(Scratch):
    """The helper on .ci/bench itself, with what the lane jobs give it."""

    def plan(self, tier: str) -> dict[str, Any]:
        output = self.tmp / f"output-{tier}"
        code, _, err = call(
            "plan",
            "--matrix",
            str(REAL_MATRIX),
            "--run",
            str(FIXTURES / "run.json"),
            "--artifacts",
            str(FIXTURES / "artifacts.jsonl"),
            "--manifests",
            str(FIXTURES / "manifests"),
            f"--tier={tier}",
            "--output",
            str(output),
        )
        self.assertEqual(code, 0, err)
        lines = output.read_text(encoding="utf-8").splitlines()
        return dict(line.split("=", 1) for line in lines)

    def test_the_lanes_of_each_tier(self) -> None:
        nightly = json.loads(self.plan("nightly")["collect"])["include"][0]
        self.assertEqual(
            [item.split("=")[0] for item in nightly["expect"].split()], list(ci.LANES)
        )
        pr = json.loads(self.plan("pr")["collect"])["include"][0]
        self.assertEqual(
            [item.split("=")[0] for item in pr["expect"].split()],
            ["bench-container", "vm1-local"],
        )
        weekly = json.loads(self.plan("weekly")["collect"])["include"][0]
        self.assertEqual(weekly["expect"], nightly["expect"])

    def test_every_lane_has_a_job_and_every_job_a_lane(self) -> None:
        matrix = lib.load_matrix(REAL_MATRIX)
        active = {
            name for name, lane in matrix.lanes.items() if lane.status == "active"
        }
        self.assertEqual(active, set(ci.LANES))
        self.assertEqual(set(JOB_SETS), set(ci.LANES))
        self.assertLess(set(ci.HARD_LANES), set(ci.LANES))

    def test_each_template_renders_with_what_its_job_sets(self) -> None:
        for lane, sets in JOB_SETS.items():
            out = self.tmp / f"{lane}.json"
            args = [arg for item in sets for arg in ("--set", item)]
            code, _, err = call(
                "env",
                "--matrix",
                str(REAL_MATRIX),
                "--lane",
                lane,
                "--out",
                str(out),
                *args,
            )
            self.assertEqual(code, 0, f"{lane}: {err}")
            with contextlib.redirect_stdout(io.StringIO()) as said:
                code = run.main(
                    [
                        "validate",
                        "--matrix",
                        str(REAL_MATRIX),
                        "--lane",
                        lane,
                        "--env",
                        str(out),
                    ]
                )
            self.assertEqual(code, 0, f"{lane}: {said.getvalue()}")
            # Nothing less than the job's sets will do.
            for index in range(len(sets)):
                less = [
                    arg
                    for item in sets[:index] + sets[index + 1 :]
                    for arg in ("--set", item)
                ]
                code, _, _ = call(
                    "env",
                    "--matrix",
                    str(REAL_MATRIX),
                    "--lane",
                    lane,
                    "--out",
                    str(out),
                    *less,
                )
                self.assertEqual(code, 2, f"{lane} without {sets[index]}")

    def test_the_needs_a_job_may_withhold_are_in_its_template(self) -> None:
        withheld = {
            "bench-container": {"io_uring", "redis"},
            "vm1-local": {"gpu", "blkdev"},
            "vm2-net": {"gpu", "rdma"},
            "vm1-loop": {"gpu", "rdma"},
            "vm1-fabric": {"gpu", "blkdev"},
        }
        for lane, needs in withheld.items():
            out = self.tmp / f"{lane}.json"
            args = [arg for item in JOB_SETS[lane] for arg in ("--set", item)]
            code, _, err = call(
                "env",
                "--matrix",
                str(REAL_MATRIX),
                "--lane",
                lane,
                "--out",
                str(out),
                *args,
            )
            self.assertEqual(code, 0, err)
            self.assertLessEqual(needs, set(run.load_env(out).provides), lane)


@unittest.skipUnless(shutil.which("bash"), "needs bash")
class StackRenderTest(Scratch):
    """'amd_bench_stack.sh render' on the state files the lane's steps leave."""

    def render(
        self, lane: str, files: dict[str, list[str]]
    ) -> subprocess.CompletedProcess[str]:
        work = self.tmp / "amd-bench"
        work.mkdir(exist_ok=True)
        for name, lines in files.items():
            (work / name).write_text("".join(line + "\n" for line in lines))
        env = {
            "PATH": os.environ["PATH"],
            "PYTHONDONTWRITEBYTECODE": "1",
            "LANE": lane,
            "RUNNER_TEMP": str(self.tmp),
            "GITHUB_WORKSPACE": str(REPO),
            "BENCH_DIAG": str(self.tmp / "diag"),
            "RUN_META": json.dumps(BASE_META),
            "ROCM_VERSION": "10.2.0",
            "GITHUB_RUN_ID": "987",
            "GITHUB_RUN_ATTEMPT": "2",
        }
        return subprocess.run(
            ["bash", str(SCRIPTS / "amd_bench_stack.sh"), "render"],
            cwd=self.tmp,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_a_two_guest_lane_with_rdma_withheld(self) -> None:
        done = self.render(
            "vm2-net",
            {
                "env-set": [
                    "hosts.guest1.ssh_config=/t/cfg",
                    "hosts.guest2.ssh_config=/t/cfg",
                    "hosts.guest1.vars.ucx_net_devices=enp0s5",
                    "hosts.guest2.vars.ucx_net_devices=enp0s6",
                ],
                "withhold": ["rdma", "rdma"],
                "meta-set": [
                    "runner_image=example.invalid/qemu@sha256:" + "0" * 64,
                    "ernic_backend=mesh",
                    "overlay_sha256=" + "c" * 64,
                ],
                "meta-set-int": ["gpu_vram_limit=0"],
            },
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        env = run.load_env(self.tmp / "amd-bench" / "env.json")
        self.assertEqual(env.provides, ("gpu", "two_hosts"))
        self.assertEqual(env.hosts["guest2"].vars["ucx_net_devices"], "enp0s6")
        meta = self.read_json(self.tmp / "amd-bench" / "run-meta.json")
        self.assertEqual(
            meta,
            {
                **BASE_META,
                "bench_run_id": "987",
                "run_attempt": 2,
                "rocm_version": "10.2.0",
                "runner_image": "example.invalid/qemu@sha256:" + "0" * 64,
                "ernic_backend": "mesh",
                "overlay_sha256": "c" * 64,
                "gpu_vram_limit": 0,
            },
        )
        self.assertEqual(self.read_json(self.tmp / "diag" / "run-meta.json"), meta)
        # The record the runner would start from is valid with this run-meta.
        matrix = lib.load_matrix(REAL_MATRIX)
        record = run.first_run_record(meta, matrix, matrix.lanes["vm2-net"], "nightly")
        self.assertEqual(lib.validate_record(record, lib.load_schema()), [])

    def test_the_container_lane_and_a_missing_job_value(self) -> None:
        done = self.render(
            "bench-container",
            {"withhold": ["io_uring"], "meta-set": ["runner_image=ubuntu:24.04"]},
        )
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        env = run.load_env(self.tmp / "amd-bench" / "env.json")
        self.assertEqual(env.provides, ("redis",))
        done = self.render("vm1-local", {"env-set": ["hosts.guest.ssh_config=/t/cfg"]})
        self.assertEqual(done.returncode, 2, done.stdout + done.stderr)
        self.assertIn("vars.gusli_blkdev", done.stderr)

    def test_a_lane_it_does_not_have(self) -> None:
        done = self.render("vm3-none", {})
        self.assertEqual(done.returncode, 2)
        self.assertNotIn("vm3", done.stdout + done.stderr)


class GuestCpuTest(Scratch):
    """'guest-cpu' on what 'amd_bench_guest.sh facts' printed in a guest."""

    CPUINFO = (
        "processor\t: 0\nvendor_id\t: GenuineIntel\ncpu family\t: 6\n"
        "model\t\t: 207\nmodel name\t: INTEL(R) XEON(R) PLATINUM 8573C\n\n"
        "processor\t: 1\nvendor_id\t: Other\nmodel name\t: second\n\n"
    )

    def guest_cpu(self, guest: str, *lines: str, lane: str = "vm2-net") -> list[str]:
        facts = self.tmp / f"{guest}-facts.txt"
        if lines:
            facts.write_text("".join(line + "\n" for line in lines))
        cpuinfo = self.tmp / "cpuinfo"
        cpuinfo.write_text(self.CPUINFO)
        code, out, err = call(
            "guest-cpu",
            "--lane",
            lane,
            "--guest",
            guest,
            "--facts",
            str(facts),
            "--cpuinfo",
            str(cpuinfo),
            "--notes",
            str(self.tmp / "notes.txt"),
        )
        self.assertEqual((code, err), (0, ""))
        return out.splitlines()

    @staticmethod
    def printed(
        vendor: str, family: str, name: str = "AMD_EPYC_Processor"
    ) -> list[str]:
        return [
            "--- CPU",
            f"vendor_id: {vendor}; cpu family: {family}",
            f"CPU_VENDOR={vendor}",
            f"CPU_FAMILY={family}",
            "CPU_MODEL=1",
            f"CPU_MODEL_NAME={name}",
        ]

    def test_an_amd_zen_guest_is_stated_and_not_warned_about(self) -> None:
        for family in ("23", "26"):
            lines = self.guest_cpu("guest1", *self.printed("AuthenticAMD", family))
            self.assertEqual(
                lines,
                [
                    "::notice title=AMD bench vm2-net guest CPU::guest1 reports the "
                    f"CPU vendor AuthenticAMD, family {family}, model 1 "
                    "(AMD_EPYC_Processor); the runner's CPU is GenuineIntel, "
                    "INTEL(R) XEON(R) PLATINUM 8573C"
                ],
            )

    def test_a_guest_that_is_not_amd_zen_is_warned_about(self) -> None:
        lines = self.guest_cpu("guest2", *self.printed("GenuineIntel", "6", "x"))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("::notice title=AMD bench vm2-net guest"))
        self.assertIn("guest2 reports the CPU vendor GenuineIntel, family 6", lines[0])
        self.assertTrue(lines[1].startswith("::warning title=AMD bench vm2-net guest"))
        self.assertIn("guest2 is GenuineIntel, not AuthenticAMD", lines[1])
        self.assertIn("expected to refuse peer-to-peer DMA", lines[1])
        self.assertTrue(lines[1].endswith("No case is skipped for it."))
        old = self.guest_cpu("guest", *self.printed("AuthenticAMD", "21"))
        self.assertIn("guest is 21, not 23 (Zen) or later", old[1])
        notes = (self.tmp / "notes.txt").read_text().splitlines()
        self.assertEqual(len(notes), 4)
        self.assertIn("not AuthenticAMD", notes[1])

    def test_the_last_value_a_guest_printed_counts(self) -> None:
        lines = self.guest_cpu(
            "guest",
            *self.printed("GenuineIntel", "6"),
            *self.printed("AuthenticAMD", "23"),
            "CPU_VENDOR=not plain::warning::x",
        )
        self.assertEqual(len(lines), 1)
        self.assertIn("vendor AuthenticAMD, family 23", lines[0])

    def test_a_guest_that_printed_nothing_usable_fails_nothing(self) -> None:
        for lines in (
            [],
            ["ssh: connect to host guest port 2222: Connection refused"],
            self.printed("unknown", "unknown", "unknown"),
            self.printed("AuthenticAMD", "0x17"),
            ["CPU_VENDOR=a::warning::b", "CPU_FAMILY=23"],
        ):
            with self.subTest(lines=lines):
                out = self.guest_cpu("guest", *lines, lane="vm1-fabric")
                self.assertEqual(len(out), 2, out)
                self.assertIn("CPU of guest could not be read", out[1])
                self.assertNotIn("expected to refuse", out[1])
                self.assertNotIn("a::warning::b", " ".join(out))

    def test_names_that_are_not_plain_are_not_echoed(self) -> None:
        code, out, _ = call(
            "guest-cpu",
            "--lane",
            "vm9::error::x",
            "--guest",
            "g::error::y",
            "--facts",
            str(self.tmp / "absent"),
            "--cpuinfo",
            str(self.tmp / "absent"),
        )
        self.assertEqual(code, 0)
        self.assertNotIn("error", out)
        self.assertIn("the runner's CPU is unknown", out)


if __name__ == "__main__":
    unittest.main()
