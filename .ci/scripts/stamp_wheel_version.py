#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Append a suffix to the wheel version of the NIXL checkout in this directory.

The wheel version is in two places, which must agree: [project].version in
pyproject.toml and the version of project() in meson.build. Both get SUFFIX
appended, both are read back, and the new version is written to stdout.

SUFFIX is PEP 440 pre, post and dev segments in canonical spelling, such as
'.dev20261118' or 'rc1.dev3'; it cannot change the release number and cannot
hold a local '+' segment. A checkout whose version already ends in SUFFIX is
left as it is, with a note on stderr, so a second run does not stamp twice.

    stamp_wheel_version.py SUFFIX
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

NUMBER = r"(?:0|[1-9][0-9]*)"
SUFFIX_RE = re.compile(rf"(?:(?:a|b|rc){NUMBER})?(?:\.post{NUMBER})?(?:\.dev{NUMBER})?")
PUBLIC_VERSION_RE = re.compile(
    rf"(?:[1-9][0-9]*!)?{NUMBER}(?:\.{NUMBER})*{SUFFIX_RE.pattern}"
)
PROJECT_TABLE_RE = re.compile(r"^\[project\][ \t]*\n(?:(?!\[).*\n?)*", re.MULTILINE)
PYPROJECT_VERSION_RE = re.compile(r'^(version\s*=\s*")([^"]+)(")', re.MULTILINE)
MESON_VERSION_RE = re.compile(
    r"^(project\(\s*'nixl'[^)]*?\bversion\s*:\s*')([^']+)(')", re.MULTILINE
)


class StampError(Exception):
    pass


def one_match(pattern: re.Pattern[str], text: str, what: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise StampError(f"expected one {what}, found {len(matches)}")
    return matches[0]


def pyproject_version(text: str) -> re.Match[str]:
    """The version line of the [project] table, as a match on the whole text."""
    table = one_match(PROJECT_TABLE_RE, text, "[project] table in pyproject.toml")
    line = one_match(
        PYPROJECT_VERSION_RE, table.group(0), "version in [project] of pyproject.toml"
    )
    found = PYPROJECT_VERSION_RE.match(text, table.start() + line.start())
    assert found is not None
    return found


def meson_version(text: str) -> re.Match[str]:
    return one_match(
        MESON_VERSION_RE, text, "project('nixl', ...) version in meson.build"
    )


def versions(directory: pathlib.Path) -> tuple[str, str]:
    return (
        pyproject_version((directory / "pyproject.toml").read_text()).group(2),
        meson_version((directory / "meson.build").read_text()).group(2),
    )


def replace_version(text: str, found: re.Match[str], version: str) -> str:
    return text[: found.start(2)] + version + text[found.end(2) :]


def stamp(directory: pathlib.Path, suffix: str) -> str:
    if not suffix or not SUFFIX_RE.fullmatch(suffix):
        raise StampError(
            f"'{suffix}' is not a PEP 440 pre, post or dev suffix such as "
            "'.dev20261118'"
        )
    pyproject = directory / "pyproject.toml"
    meson = directory / "meson.build"
    pyproject_text = pyproject.read_text()
    meson_text = meson.read_text()
    in_pyproject = pyproject_version(pyproject_text)
    in_meson = meson_version(meson_text)
    current = in_pyproject.group(2)
    if in_meson.group(2) != current:
        raise StampError(
            f"pyproject.toml has version {current} but meson.build has "
            f"{in_meson.group(2)}"
        )
    if current.endswith(suffix):
        sys.stderr.write(f"version {current} already ends in {suffix}; unchanged\n")
        return current
    version = current + suffix
    if not PUBLIC_VERSION_RE.fullmatch(version):
        raise StampError(
            f"{current} with '{suffix}' appended is not a PEP 440 public version"
        )
    pyproject.write_text(replace_version(pyproject_text, in_pyproject, version))
    meson.write_text(replace_version(meson_text, in_meson, version))
    after = versions(directory)
    if after != (version, version):
        raise StampError(
            f"after stamping, pyproject.toml has {after[0]} and meson.build has "
            f"{after[1]}, expected {version} in both"
        )
    return version


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument(
        "suffix", metavar="SUFFIX", help="what to append, for example .dev20261118"
    )
    args = parser.parse_args()
    try:
        version = stamp(pathlib.Path.cwd(), args.suffix)
    except (StampError, OSError) as exc:
        sys.stderr.write(f"stamp_wheel_version.py: {exc}\n")
        return 1
    sys.stdout.write(version + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
