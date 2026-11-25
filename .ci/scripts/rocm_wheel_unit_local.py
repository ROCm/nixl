#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Run a rocjitsu lane on this machine, from the workflow files.

The steps of three jobs are read from the workflows and run as written, in a
copy of the working tree (uncommitted changes included):

  rocm-wheels.yml                build-unit-tests
  rocm-wheels-rocjitsu-unit.yml  gate, unit-test

or, with --lane smoke, the wheel smoke lane on wheels given by --wheels-dir:

  rocm-wheels-rocjitsu.yml       gate, smoke-test

Only the steps that need GitHub are replaced. Checkout is skipped, or for a
checkout of another commit into a subdirectory, taken from this repository.
Artifact upload and download become a copy under the work directory that
drops file modes and symlinks, as the real ones do. qemu-tool-action is
fetched at the pinned commit and run directly, with two of its inputs
changed: work-dir points under the work directory and setup-host is false, so
nothing is installed on this machine with sudo.

Needs docker, /dev/kvm, git, python3 with PyYAML, and what qemu-tool needs
on PATH (qemu-img, zstd, oras, ssh). Behind a TLS-intercepting proxy, pass
--ca-bundle for the image build and --guest-ca for what the guests download.
"""

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
WHEELS = ".github/workflows/rocm-wheels.yml"
UNIT = ".github/workflows/rocm-wheels-rocjitsu-unit.yml"
SMOKE = ".github/workflows/rocm-wheels-rocjitsu.yml"
ACTION = "sbates130272/qemu-tool-action"
CA_NAME = "local-ca-bundle.crt"
EXPR = re.compile(r"\$\{\{(.*?)\}\}", re.S)
STATUS = re.compile(r"\b(always|failure|cancelled|success)\(\)")


class Stop(Exception):
    pass


def say(text: str) -> None:
    print(f"\033[1m==> {text}\033[0m", flush=True)


def text_of(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return "" if value is None else str(value)


def truthy(value) -> bool:
    return text_of(value) not in ("", "false", "0")


def lookup(path: str, ctx: dict):
    node = ctx
    for part in path.split("."):
        if not isinstance(node, dict):
            raise Stop(f"cannot evaluate '{path}'")
        if part not in node:
            if path.split(".")[0] not in ctx:
                raise Stop(f"unknown context in '{path}'")
            return ""
        node = node[part]
    return node


def evaluate(expr: str, ctx: dict, failed: bool):
    expr = expr.strip()
    if "&&" in expr:
        return all(truthy(evaluate(p, ctx, failed)) for p in expr.split("&&"))
    for op in ("==", "!="):
        if op in expr:
            left, right = (text_of(evaluate(p, ctx, failed)) for p in expr.split(op, 1))
            return (left == right) == (op == "==")
    if expr.startswith("!"):
        return not truthy(evaluate(expr[1:], ctx, failed))
    if expr in ("always()", "cancelled()", "success()", "failure()"):
        return {
            "always()": True,
            "cancelled()": False,
            "success()": not failed,
            "failure()": failed,
        }[expr]
    if len(expr) > 1 and expr[0] == expr[-1] == "'":
        return expr[1:-1]
    if re.fullmatch(r"[A-Za-z_][\w.-]*", expr):
        return lookup(expr, ctx)
    raise Stop(f"this runner cannot evaluate '{expr}'")


def fill(value, ctx: dict) -> str:
    return EXPR.sub(lambda m: text_of(evaluate(m.group(1), ctx, False)), text_of(value))


def wanted(condition, ctx: dict, failed: bool) -> bool:
    if condition is None:
        return not failed
    expr = text_of(condition).strip()
    whole = EXPR.fullmatch(expr)
    if whole:
        expr = whole.group(1)
    result = truthy(evaluate(expr, ctx, failed))
    return result if STATUS.search(expr) else result and not failed


def read_commands(path: Path, clear: bool = True) -> dict:
    """The key=value and key<<delimiter records of a GITHUB_OUTPUT-style file."""
    found, lines = {}, path.read_text().splitlines() if path.exists() else []
    if clear:
        path.write_text("")
    while lines:
        line = lines.pop(0)
        if "<<" in line.split("=", 1)[0]:
            key, delimiter = line.split("<<", 1)
            body = []
            while lines and lines[0] != delimiter:
                body.append(lines.pop(0))
            lines[:1] = []
            found[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            found[key] = value
    return found


def flat_copy(source: Path, target: Path) -> None:
    """Copy as an artifact round trip does: links followed, modes dropped."""
    if target.exists():
        shutil.rmtree(target)
    for root, _dirs, files in os.walk(source, followlinks=True):
        out = target / Path(root).relative_to(source)
        out.mkdir(parents=True, exist_ok=True)
        for name in files:
            shutil.copyfile(Path(root) / name, out / name)
            (out / name).chmod(0o644)


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.work = Path(args.work_dir).expanduser().resolve()
        self.workspace = self.work / "workspace"
        self.artifacts = self.work / "artifacts"
        self.files = self.work / "runner"
        self.env: dict[str, str] = {}
        self.paths: list[str] = []
        self.post: list[tuple[Path, dict]] = []

    # ── Setting up ───────────────────────────────────────────────────────

    def prepare(self) -> None:
        if self.args.lane == "smoke" and not self.args.wheels_dir:
            raise Stop("--lane smoke needs --wheels-dir")
        tests_dir = self.args.tests_dir
        if tests_dir and self.work in Path(tests_dir).expanduser().resolve().parents:
            raise Stop(
                f"--tests-dir {tests_dir} is under the work directory, which is cleared"
            )
        for path in (self.workspace, self.artifacts, self.files):
            if path.exists():
                shutil.rmtree(path)
            path.mkdir(parents=True)
        listed = subprocess.run(
            ["git", "ls-files", "-co", "--exclude-standard", "-z"],
            cwd=REPO,
            check=True,
            capture_output=True,
        ).stdout
        for name in filter(None, listed.decode().split("\0")):
            source = REPO / name
            if source.is_file() or source.is_symlink():
                target = self.workspace / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, target, follow_symlinks=False)
        if self.args.reuse_subprojects:
            for entry in (REPO / "subprojects").iterdir():
                if entry.is_dir():
                    shutil.copytree(
                        entry,
                        self.workspace / "subprojects" / entry.name,
                        symlinks=True,
                        dirs_exist_ok=True,
                    )
        if self.args.ca_bundle:
            self.trust_ca(Path(self.args.ca_bundle).expanduser())

    def trust_ca(self, bundle: Path) -> None:
        """Make the copy of Dockerfile.tests trust a proxy's CA. Not the repo's."""
        shutil.copyfile(bundle, self.workspace / CA_NAME)
        dockerfile = self.workspace / "contrib/Dockerfile.tests"
        trust = (
            f"COPY {CA_NAME} /{CA_NAME}\n"
            f"ENV GIT_SSL_CAINFO=/{CA_NAME} PIP_CERT=/{CA_NAME} "
            f"SSL_CERT_FILE=/{CA_NAME} REQUESTS_CA_BUNDLE=/{CA_NAME}\n"
        )
        dockerfile.write_text(
            re.sub(r"(?m)^(FROM .*\n)", r"\1" + trust, dockerfile.read_text())
        )

    def load(self, name: str) -> dict:
        return yaml.safe_load((self.workspace / name).read_text())

    # ── Steps ────────────────────────────────────────────────────────────

    def step_env(self, extra: dict) -> dict:
        env = dict(os.environ, **self.env, **extra)
        env.update(
            GITHUB_WORKSPACE=str(self.workspace),
            RUNNER_TEMP=str(self.files),
            GITHUB_OUTPUT=str(self.files / "output"),
            GITHUB_ENV=str(self.files / "env"),
            GITHUB_PATH=str(self.files / "path"),
            GITHUB_STATE=str(self.files / "state"),
            GITHUB_STEP_SUMMARY=str(self.files / "summary.md"),
            PATH=os.pathsep.join(self.paths + [os.environ["PATH"]]),
        )
        return env

    def finish_step(self) -> dict:
        self.env.update(read_commands(self.files / "env"))
        path_file = self.files / "path"
        if path_file.exists():
            self.paths[:0] = reversed(path_file.read_text().split())
            path_file.write_text("")
        return read_commands(self.files / "output")

    def execute(self, argv: list, env: dict, minutes) -> bool:
        try:
            done = subprocess.run(
                argv,
                cwd=self.workspace,
                env=env,
                timeout=60 * minutes if minutes else None,
            )
        except subprocess.TimeoutExpired:
            print(f"timed out after {minutes} minutes", flush=True)
            return False
        return done.returncode == 0

    def run_script(self, step: dict, ctx: dict) -> bool:
        script = self.files / "step.sh"
        script.write_text(fill(step["run"], ctx))
        extra = {k: fill(v, ctx) for k, v in step.get("env", {}).items()}
        # What `shell: bash` means on a runner.
        return self.execute(
            ["bash", "--noprofile", "--norc", "-eo", "pipefail", str(script)],
            self.step_env(extra),
            step.get("timeout-minutes"),
        )

    def run_action(self, step: dict, ctx: dict, sha: str) -> bool:
        checkout = self.work / "actions" / sha
        if not (checkout / "action.yml").is_file():
            checkout.mkdir(parents=True, exist_ok=True)
            for cmd in (
                ["init", "-q"],
                [
                    "fetch",
                    "-q",
                    "--depth",
                    "1",
                    f"https://github.com/{ACTION}.git",
                    sha,
                ],
                ["checkout", "-q", "FETCH_HEAD"],
            ):
                subprocess.run(["git", *cmd], cwd=checkout, check=True)
        declared = yaml.safe_load((checkout / "action.yml").read_text())["inputs"]
        given = {k: fill(v, ctx) for k, v in step.get("with", {}).items()}
        unknown = sorted(set(given) - set(declared))
        if unknown:
            print(f"::error::not inputs of the action: {', '.join(unknown)}")
            return False
        local = {"work-dir": str(self.work / "qemu-tool"), "setup-host": "false"}
        print(f"local overrides: {local}", flush=True)
        values = {k: text_of(v.get("default")) for k, v in declared.items()}
        values.update(given, **local)
        inputs = {f"INPUT_{k.upper()}": v for k, v in values.items() if v}
        env = self.step_env(dict(inputs, PYTHONUNBUFFERED="1"))
        script = checkout / "qemu_tool_action.py"
        ok = self.execute(
            [sys.executable, str(script), "main"], env, step.get("timeout-minutes")
        )
        state = read_commands(self.files / "state")
        self.post.append(
            (script, dict(inputs, **{f"STATE_{k}": v for k, v in state.items()}))
        )
        if ok and self.args.guest_ca:
            ok = self.trust_ca_in_guests(Path(self.args.guest_ca).expanduser())
        return ok

    def trust_ca_in_guests(self, cert: Path) -> bool:
        """Make every guest trust a proxy's CA, for pip and uv as well as curl."""
        # Not finish_step(): the step's outputs are still to be collected.
        exported = read_commands(self.files / "env", clear=False)
        config = exported.get("QEMU_TOOL_SSH_CONFIG", "")
        hosts = re.findall(r"(?m)^Host (\S+)", Path(config).read_text())
        bundle = "/etc/ssl/certs/ca-certificates.crt"
        names = "PIP_CERT SSL_CERT_FILE REQUESTS_CA_BUNDLE"
        remote = (
            "sudo -n tee /usr/local/share/ca-certificates/local-proxy.crt >/dev/null"
            " && sudo -n update-ca-certificates >/dev/null"
            f' && for n in {names}; do echo "$n={bundle}"; done'
            " | sudo -n tee -a /etc/environment >/dev/null"
            " && echo UV_NATIVE_TLS=1 | sudo -n tee -a /etc/environment >/dev/null"
        )
        for host in hosts:
            with cert.open("rb") as handle:
                done = subprocess.run(["ssh", "-F", config, host, remote], stdin=handle)
            if done.returncode != 0:
                print(f"::error::could not add the CA certificate to {host}")
                return False
            print(f"{host} trusts {cert}")
        return True

    def run_step(self, step: dict, ctx: dict) -> bool:
        uses = step.get("uses", "")
        if "run" in step:
            return self.run_script(step, ctx)
        if uses.startswith(f"{ACTION}@"):
            return self.run_action(step, ctx, uses.split("@", 1)[1])
        given = {k: fill(v, ctx) for k, v in step.get("with", {}).items()}
        if uses.startswith("actions/upload-artifact@"):
            source = self.workspace / given["path"]
            if source.is_dir():
                flat_copy(source, self.artifacts / given["name"])
                print(f"kept as artifact {given['name']}")
            else:
                print(f"not kept: {given['path']} is not a directory")
            return True
        if uses.startswith("actions/download-artifact@"):
            source = self.artifacts / given["name"]
            if not source.is_dir():
                print(f"::error::no artifact named {given['name']}")
                return False
            flat_copy(source, self.workspace / given["path"])
            return True
        if uses.startswith("actions/checkout@"):
            if not given.get("path"):
                print("skipped: the workspace is a copy of the working tree")
                return True
            # Another commit beside the workspace: from this repository's
            # objects, so the commit has to have been fetched.
            target = self.workspace / given["path"]
            target.mkdir(parents=True)
            archive = subprocess.Popen(
                ["git", "archive", given["ref"]], cwd=REPO, stdout=subprocess.PIPE
            )
            unpacked = subprocess.run(["tar", "-xC", str(target)], stdin=archive.stdout)
            return archive.wait() == 0 and unpacked.returncode == 0
        raise Stop(f"this runner does not know the action {uses}")

    # ── Jobs ─────────────────────────────────────────────────────────────

    def run_job(self, workflow: dict, job_id: str, ctx: dict) -> tuple[bool, dict]:
        job = workflow["jobs"][job_id]
        ctx = dict(ctx, steps={}, env={})
        self.env, self.paths, self.post = {}, [], []
        for scope in (workflow.get("env", {}), job.get("env", {})):
            self.env.update({k: fill(v, ctx) for k, v in scope.items()})
        say(f"job {job_id}")
        if not wanted(job.get("if"), ctx, False):
            print("skipped by its if")
            return True, {}
        failed = False
        try:
            for step in job["steps"]:
                ctx["env"] = dict(self.env)
                name = fill(step.get("name", step.get("uses", "run")), ctx)
                if not wanted(step.get("if"), ctx, failed):
                    say(f"{job_id} / {name}: skipped")
                    continue
                say(f"{job_id} / {name}")
                ok = self.run_step(step, ctx)
                if "id" in step:
                    ctx["steps"][step["id"]] = {"outputs": self.finish_step()}
                else:
                    self.finish_step()
                if not ok:
                    print(f"\033[31mFAILED: {name}\033[0m", flush=True)
                    failed = True
        finally:
            for script, env in self.post:
                if self.args.keep:
                    print("--keep: the guest is left up; qemu-tool down removes it")
                else:
                    say(f"{job_id} / post")
                    self.execute(
                        [sys.executable, str(script), "post"], self.step_env(env), 10
                    )
        outputs = {k: fill(v, ctx) for k, v in job.get("outputs", {}).items()}
        return not failed, outputs

    def smoke(self, wheels: dict, github: dict) -> bool:
        """The wheel smoke lane, on wheels that were built elsewhere."""
        source = subprocess.run(
            ["git", "rev-parse", "--verify", f"{self.args.source_ref}^{{commit}}"],
            cwd=REPO,
            capture_output=True,
            text=True,
        )
        if source.returncode != 0:
            raise Stop(f"--source-ref {self.args.source_ref} is not a commit here")
        ctx = {
            "github": github,
            "matrix": {"rocm-version": self.args.rocm_version},
            "inputs": {"artifact-suffix": ""},
            "needs": {
                "plan": {
                    "outputs": {
                        "wheels-artifact": "rocm-nixl-wheels",
                        "test-python": self.args.python_version,
                        "sha": source.stdout.strip(),
                    }
                }
            },
        }
        called = {
            k: fill(v, ctx) for k, v in wheels["jobs"]["rocjitsu"]["with"].items()
        }
        flat_copy(
            Path(self.args.wheels_dir).expanduser(),
            self.artifacts / called["artifact-name"],
        )
        workflow = self.load(SMOKE)
        ctx = {"github": github, "inputs": dict(called, **{"run-id": ""})}
        ok, outputs = self.run_job(workflow, "gate", ctx)
        if not ok:
            return False
        ctx["needs"] = {"gate": {"outputs": outputs}}
        return self.run_job(workflow, "smoke-test", ctx)[0]

    def main(self) -> int:
        self.prepare()
        version = self.args.rocm_version
        wheels, unit = self.load(WHEELS), self.load(UNIT)
        github = {"workspace": str(self.workspace), "token": "", "sha": "local"}
        if self.args.lane == "smoke":
            ok = self.smoke(wheels, github)
            for name in ("rocjitsu-results", "rocjitsu-diagnostics"):
                if (self.workspace / name).is_dir():
                    print(f"{name}: {self.workspace / name}")
            say("PASSED" if ok else "FAILED")
            return 0 if ok else 1
        ctx = {
            "github": github,
            "matrix": {"rocm-version": version},
            "inputs": {"artifact-suffix": ""},
            "needs": {
                "plan": {"outputs": {"supported": "true", "run-vm-tests": "true"}}
            },
        }
        # The inputs the calling job hands to the unit-test workflow.
        called = {k: fill(v, ctx) for k, v in wheels["jobs"]["unit"]["with"].items()}
        if self.args.tests_dir:
            say(f"build-unit-tests: skipped, using {self.args.tests_dir}")
            flat_copy(
                Path(self.args.tests_dir).expanduser(),
                self.artifacts / called["artifact-name"],
            )
        elif not self.run_job(wheels, "build-unit-tests", ctx)[0]:
            return 1
        ctx = {"github": github, "inputs": dict(called, **{"run-id": ""})}
        ok, outputs = self.run_job(unit, "gate", ctx)
        if not ok:
            return 1
        ctx["needs"] = {"gate": {"outputs": outputs}}
        ok, _ = self.run_job(unit, "unit-test", ctx)
        for name in ("unit-results", "unit-diagnostics"):
            if (self.workspace / name).is_dir():
                print(f"{name}: {self.workspace / name}")
        say("PASSED" if ok else "FAILED")
        return 0 if ok else 1


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--lane",
        choices=["unit", "smoke"],
        default="unit",
        help="unit: build the C++ unit tests and run them (default). smoke: "
        "the wheel smoke lane, which needs --wheels-dir.",
    )
    p.add_argument(
        "--wheels-dir",
        metavar="DIR",
        help="smoke: directory holding the merged rocm_nixl wheel, such as a "
        "downloaded rocm-nixl-wheels artifact.",
    )
    p.add_argument(
        "--source-ref",
        default="rocm/for-upstream/feat/rocm-wheels",
        help="smoke: commit the wheels were built from, already fetched "
        "(default: %(default)s).",
    )
    p.add_argument(
        "--python-version",
        default="3.12",
        help="smoke: Python version of the wheel to install (default: 3.12).",
    )
    p.add_argument(
        "--guest-ca",
        metavar="FILE",
        help="CA certificate every guest must trust, for a TLS-intercepting "
        "proxy between the guests and PyPI.",
    )
    p.add_argument(
        "--rocm-version",
        default="10.2.0",
        help="ROCm version of the matrix entry to run (default: 10.2.0).",
    )
    p.add_argument(
        "--work-dir",
        default="~/.cache/nixl-unit-local",
        help="Where the workspace copy, artifacts, guest images and "
        "qemu-tool go (default: %(default)s).",
    )
    p.add_argument(
        "--ca-bundle",
        metavar="FILE",
        help="CA bundle the image build must trust, for a TLS-"
        "intercepting proxy. Only the workspace copy is changed.",
    )
    p.add_argument(
        "--reuse-subprojects",
        action="store_true",
        help="Copy the meson subprojects this checkout has already "
        "downloaded into the workspace, so the image build "
        "does not fetch them.",
    )
    p.add_argument(
        "--tests-dir",
        metavar="DIR",
        help="Skip the image build and use this test tree, such as "
        "a downloaded nixl-unit-tests artifact.",
    )
    p.add_argument(
        "--keep", action="store_true", help="Leave the guest running at the end."
    )
    return p.parse_args()


if __name__ == "__main__":
    try:
        sys.exit(Runner(parse_args()).main())
    except Stop as stop:
        sys.exit(f"error: {stop}")
