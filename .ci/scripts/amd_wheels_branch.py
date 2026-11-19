#!/usr/bin/env python3
# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Write the name of the wheels branch, read from FILE, to stdout.

    amd_wheels_branch.py FILE

FILE is .github/amd-wheels-branch.txt: one branch name on a line of its own,
with '#' comments and blank lines ignored. The name must be one the AMD
Nightly workflow accepts in its branch list. A file that is missing, names no
branch or several, or holds another kind of name is reported as a workflow
error on stderr, and the exit status is 1.

Standard library only.
"""

from __future__ import annotations

import pathlib
import re
import sys

NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,99}")


def valid_name(name: str) -> bool:
    return (
        NAME_RE.fullmatch(name) is not None
        and ".." not in name
        and "//" not in name
        and not name.endswith(("/", ".lock"))
    )


def main() -> int:
    if len(sys.argv) != 2:
        sys.stderr.write("usage: amd_wheels_branch.py FILE\n")
        return 2
    path = sys.argv[1]
    try:
        text = pathlib.Path(path).read_text(encoding="utf-8")
    except (OSError, ValueError):
        sys.stderr.write(
            f"::error::{path}, which names the wheels branch, could not be read\n"
        )
        return 1
    names = [line.split("#", 1)[0].strip() for line in text.splitlines()]
    names = [name for name in names if name]
    if len(names) != 1:
        sys.stderr.write(
            f"::error::{path} must name exactly one branch and names {len(names)}\n"
        )
        return 1
    # A rejected name is never echoed.
    if not valid_name(names[0]):
        sys.stderr.write(
            f"::error::{path} does not hold a branch name this workflow accepts "
            "(letters, digits, '.', '_', '-' and '/')\n"
        )
        return 1
    sys.stdout.write(f"{names[0]}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
