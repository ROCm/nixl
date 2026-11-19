#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Pip index for the nightly rocm-nixl wheels published as release assets.

    amd_wheel_index.py select --summary nightly-summary.json --branch NAME...
    amd_wheel_index.py update --site DIR --branch SLUG --base-url URL
                              [--ref NAME] [--sha SHA] [--date ISO] WHEEL...

``select`` reads the "AMD Nightly" summary and writes, as a JSON list on
stdout, the named branches whose wheels may be published: the entry must have
passed, be supported and carry a development version. Every other branch gets
a notice on stderr.

``update`` writes a PEP 503 index at DIR/wheels/SLUG/simple/ whose links point
at URL/<wheel file>, DIR/wheels/SLUG/index.json describing that night, and the
landing page DIR/wheels/index.html. Only the wheels named on the command line
are listed, so each run replaces the previous night. The wheels themselves are
never copied into DIR. Nothing outside DIR/wheels/SLUG/ is deleted: the index
of a branch that is no longer published stays, and the landing page marks
every index older than the newest one as no longer updated.

Standard library only.
"""

from __future__ import annotations

import argparse
import datetime
import email.parser
import hashlib
import html
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import urllib.parse
import zipfile
from typing import Any, Optional

PROJECT = "rocm-nixl"
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
BRANCH_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}")
ARTIFACT_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
SHA_RE = re.compile(r"[0-9a-f]{40}")
URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/%-]+")
# A PEP 440 public version in normal form; a local "+" segment never matches.
VERSION_RE = re.compile(
    r"(?:[1-9][0-9]*!)?(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))*"
    r"(?:(?:a|b|rc)(?:0|[1-9][0-9]*))?(?:\.post(?:0|[1-9][0-9]*))?"
    r"(?P<dev>\.dev(?:0|[1-9][0-9]*))?"
)
WHEEL_RE = re.compile(
    r"(?P<dist>[A-Za-z0-9_.]+)-(?P<version>[A-Za-z0-9_.!]+)"
    r"(?:-(?P<build>[0-9][A-Za-z0-9_.]*))?"
    r"-(?P<python>[A-Za-z0-9_.]+)-(?P<abi>[A-Za-z0-9_.]+)"
    r"-(?P<platform>[A-Za-z0-9_.]+)\.whl"
)

STYLE = (
    ":root{color-scheme:light dark}"
    "body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;"
    "max-width:60rem;margin:2rem auto;padding:0 1rem;line-height:1.5}"
    "table{border-collapse:collapse}"
    "th,td{text-align:left;padding:.25rem .75rem;"
    "border-bottom:1px solid color-mix(in srgb,currentColor 20%,transparent)}"
    "a{color:#2a78d6}"
    "code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace}"
)


class WheelIndexError(Exception):
    pass


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def is_dev_version(version: Any) -> bool:
    if not isinstance(version, str):
        return False
    match = VERSION_RE.fullmatch(version)
    return bool(match and match.group("dev"))


def write_text(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        pathlib.Path(tmp).unlink(missing_ok=True)
        raise


def read_wheel(path: pathlib.Path) -> dict[str, Any]:
    match = WHEEL_RE.fullmatch(path.name)
    if not match:
        raise WheelIndexError(f"{path.name}: not a wheel file name this index accepts")
    if normalise(match.group("dist")) != PROJECT:
        raise WheelIndexError(f"{path.name}: not a {PROJECT} wheel")
    version = match.group("version")
    if not VERSION_RE.fullmatch(version):
        raise WheelIndexError(
            f"{path.name}: {version!r} is not a PEP 440 public version"
        )

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1 << 20), b""):
            digest.update(block)

    member = f"{match.group('dist')}-{version}.dist-info/METADATA"
    try:
        with zipfile.ZipFile(path) as wheel:
            raw = wheel.read(member)
    except (KeyError, zipfile.BadZipFile, OSError) as exc:
        raise WheelIndexError(f"{path.name}: cannot read {member}: {exc}") from exc
    metadata = email.parser.BytesHeaderParser().parsebytes(raw)
    if normalise(str(metadata.get("Name", ""))) != PROJECT:
        raise WheelIndexError(f"{path.name}: METADATA Name is {metadata.get('Name')!r}")
    if metadata.get("Version") != version:
        raise WheelIndexError(
            f"{path.name}: METADATA Version {metadata.get('Version')!r} does not "
            f"match the file name"
        )
    requires_python = metadata.get("Requires-Python")
    return {
        "file": path.name,
        "bytes": path.stat().st_size,
        "sha256": digest.hexdigest(),
        "python": match.group("python"),
        "requires_python": str(requires_python).strip() if requires_python else None,
        "version": version,
    }


def page(title: str, body: list[str]) -> str:
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="pypi:repository-version" content="1.0">',
            f"<title>{html.escape(title)}</title>",
            "</head>",
            "<body>",
            *body,
            "</body>",
            "</html>",
            "",
        ]
    )


def project_page(files: list[dict[str, Any]]) -> str:
    body = [f"<h1>Links for {PROJECT}</h1>"]
    for entry in files:
        attrs = (
            f'href="{html.escape(entry["url"], quote=True)}#sha256={entry["sha256"]}"'
        )
        if entry["requires_python"]:
            attrs += (
                " data-requires-python="
                f'"{html.escape(entry["requires_python"], quote=True)}"'
            )
        body.append(f"<a {attrs}>{html.escape(entry['file'])}</a><br/>")
    return page(f"Links for {PROJECT}", body)


def landing_page(wheels_dir: pathlib.Path) -> str:
    indexes = []
    for index in sorted(wheels_dir.glob("*/index.json")):
        try:
            data = json.loads(index.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        indexes.append((index.parent.name, data))
    # One branch is published at a time, so an index older than the newest
    # belongs to a branch that is no longer published.
    newest = max((str(data.get("date") or "") for _, data in indexes), default="")
    rows = []
    for slug, data in indexes:
        date = str(data.get("date") or "")
        cells = [
            f'<a href="{html.escape(urllib.parse.quote(slug), quote=True)}/simple/">'
            f"{html.escape(slug)}</a>",
            html.escape(str(data.get("branch") or "")),
            f"<code>{html.escape(str(data.get('version') or ''))}</code>",
            f"<code>{html.escape(str(data.get('sha') or '')[:12])}</code>",
            html.escape(date),
            html.escape(str(len(data.get("files") or []))),
            "updated nightly" if date == newest else "no longer updated",
        ]
        rows.append("<tr>" + "".join(f"<td>{cell}</td>" for cell in cells) + "</tr>")
    head = ["Index", "Branch", "Version", "Commit", "Built (UTC)", "Wheels", "Status"]
    return "\n".join(
        [
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '<meta charset="utf-8">',
            '<meta name="viewport" content="width=device-width,initial-scale=1">',
            f"<title>{PROJECT} nightly wheels</title>",
            f"<style>{STYLE}</style>",
            "</head>",
            "<body>",
            f"<h1>{PROJECT} nightly wheels</h1>",
            "<p>Development builds from the last successful nightly of the published "
            "branch. They are not PyPI releases and have only been tested against "
            "emulated devices, not on hardware. Each index lists the latest night "
            "only. An index that is no longer updated holds the last build of a "
            "branch that used to be published.</p>",
            "<p><code>pip install --pre --index-url "
            "&lt;this page&gt;&lt;index&gt;/simple/ "
            f"--extra-index-url https://pypi.org/simple {PROJECT}</code></p>",
            "<table>",
            "<tr>" + "".join(f"<th>{name}</th>" for name in head) + "</tr>",
            *rows,
            "</table>",
            "</body>",
            "</html>",
            "",
        ]
    )


def update(args: argparse.Namespace) -> int:
    if not SLUG_RE.fullmatch(args.branch):
        raise WheelIndexError(f"invalid slug {args.branch!r}")
    base_url = args.base_url.rstrip("/")
    if not URL_RE.fullmatch(base_url):
        raise WheelIndexError(f"invalid base URL {args.base_url!r}")
    if args.sha and not SHA_RE.fullmatch(args.sha):
        raise WheelIndexError(f"invalid commit sha {args.sha!r}")
    if args.ref and not BRANCH_RE.fullmatch(args.ref):
        raise WheelIndexError(f"invalid branch name {args.ref!r}")
    date = args.date or datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    if not re.fullmatch(r"[0-9T:Z.+-]{8,40}", date):
        raise WheelIndexError(f"invalid date {date!r}")

    files = sorted(
        (read_wheel(pathlib.Path(wheel)) for wheel in args.wheels),
        key=lambda entry: entry["file"],
    )
    names = [entry["file"] for entry in files]
    if len(set(names)) != len(names):
        raise WheelIndexError("the same wheel file name was given more than once")
    versions = sorted({entry["version"] for entry in files})
    if len(versions) != 1:
        raise WheelIndexError(
            f"wheels of several versions given: {', '.join(versions)}"
        )
    for entry in files:
        entry["url"] = f"{base_url}/{urllib.parse.quote(entry.pop('file'))}"
        entry["file"] = urllib.parse.unquote(entry["url"].rsplit("/", 1)[1])
        del entry["version"]

    site = pathlib.Path(args.site)
    if not site.is_dir():
        raise WheelIndexError(f"{site} is not a directory")
    slug_dir = site / "wheels" / args.branch
    simple = slug_dir / "simple"
    if simple.exists():
        shutil.rmtree(simple)
    write_text(
        simple / "index.html",
        page("Simple index", [f'<a href="{PROJECT}/">{PROJECT}</a><br/>']),
    )
    write_text(simple / PROJECT / "index.html", project_page(files))
    summary = {
        "schema": 1,
        "project": PROJECT,
        "slug": args.branch,
        "branch": args.ref or None,
        "version": versions[0],
        "sha": args.sha or None,
        "date": date,
        "base_url": base_url,
        "files": [
            {
                key: entry[key]
                for key in (
                    "file",
                    "url",
                    "bytes",
                    "sha256",
                    "python",
                    "requires_python",
                )
            }
            for entry in files
        ],
    }
    write_text(slug_dir / "index.json", json.dumps(summary, indent=2) + "\n")
    write_text(site / "wheels" / "index.html", landing_page(site / "wheels"))
    sys.stdout.write(f"{slug_dir / 'index.json'}\n")
    return 0


def notice(level: str, branch: str, message: str) -> None:
    text = f"{branch}: {message}".replace("%", "%25").replace("\n", " ")
    sys.stderr.write(f"::{level} title=Nightly wheels not published::{text}\n")


def branch_entries(summary: Any) -> dict[str, dict[str, Any]]:
    branches = summary.get("branches") if isinstance(summary, dict) else None
    if isinstance(branches, dict):
        items = [
            {"branch": name, **entry}
            for name, entry in branches.items()
            if isinstance(entry, dict)
        ]
    elif isinstance(branches, list):
        items = [entry for entry in branches if isinstance(entry, dict)]
    else:
        raise WheelIndexError("the summary has no 'branches' list")
    found: dict[str, dict[str, Any]] = {}
    for entry in items:
        name = lookup(entry, "branch") or lookup(entry, "ref")
        if isinstance(name, str):
            found.setdefault(name, entry)
    return found


def lookup(entry: dict[str, Any], key: str) -> Any:
    if key in entry:
        return entry[key]
    results = entry.get("results")
    return results.get(key) if isinstance(results, dict) else None


def select(args: argparse.Namespace) -> int:
    try:
        summary = json.loads(pathlib.Path(args.summary).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WheelIndexError(f"cannot read {args.summary}: {exc}") from exc
    entries = branch_entries(summary)
    chosen: list[dict[str, str]] = []
    for branch in dict.fromkeys(args.branch):
        entry: Optional[dict[str, Any]] = entries.get(branch)
        if not BRANCH_RE.fullmatch(branch) or entry is None:
            notice("notice", branch, "not part of this nightly run")
            continue
        if entry.get("passed") is not True or lookup(entry, "supported") is not True:
            reason = entry.get("reason")
            if not (isinstance(reason, str) and reason):
                reason = "the nightly did not pass"
            notice("notice", branch, f"{reason}; release left as it is")
            continue
        slug = lookup(entry, "slug") or branch
        version = lookup(entry, "version")
        sha = lookup(entry, "sha")
        artifact = lookup(entry, "wheels_artifact") or f"rocm-nixl-wheels-{slug}"
        if not is_dev_version(version):
            notice(
                "warning", branch, f"{version!r} is not a PEP 440 development version"
            )
            continue
        if not (
            isinstance(slug, str)
            and SLUG_RE.fullmatch(slug)
            and isinstance(sha, str)
            and SHA_RE.fullmatch(sha)
            and isinstance(artifact, str)
            and ARTIFACT_RE.fullmatch(artifact)
        ):
            notice(
                "warning",
                branch,
                "the summary entry has an invalid slug, sha or artifact",
            )
            continue
        if any(item["slug"] == slug for item in chosen):
            notice(
                "warning", branch, f"slug {slug!r} is already used by another branch"
            )
            continue
        chosen.append(
            {
                "branch": branch,
                "slug": slug,
                "sha": sha,
                "version": version,
                "wheels_artifact": artifact,
            }
        )
    sys.stdout.write(json.dumps(chosen, separators=(",", ":")) + "\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    sel = commands.add_parser("select", help="list the branches that may be published")
    sel.add_argument("--summary", required=True, help="nightly-summary.json")
    sel.add_argument("--branch", required=True, nargs="+", help="branch names")
    sel.set_defaults(func=select)

    upd = commands.add_parser("update", help="write the index for one branch")
    upd.add_argument("--site", required=True, help="checkout of the Pages branch")
    upd.add_argument("--branch", required=True, metavar="SLUG")
    upd.add_argument("--base-url", required=True, help="URL the wheel files live under")
    upd.add_argument("--ref", default="", help="branch name, recorded in index.json")
    upd.add_argument("--sha", default="", help="commit built, recorded in index.json")
    upd.add_argument("--date", default="", help="UTC build time; defaults to now")
    upd.add_argument("wheels", nargs="+", metavar="WHEEL")
    upd.set_defaults(func=update)

    args = parser.parse_args()
    try:
        return int(args.func(args))
    except WheelIndexError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1


if __name__ == "__main__":
    sys.exit(main())
