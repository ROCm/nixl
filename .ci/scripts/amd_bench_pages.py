# SPDX-FileCopyrightText: Copyright (c) 2026 Advanced Micro Devices, Inc. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Static pages of the published bench runs: the landing page and one per run.

    render_landing(manifest, latest)   bench/index.html
    render_run(index)                  bench/runs/<shard id>.html

Pure functions over the JSON documents the publisher writes under
data/bench/v1 (manifest.json, latest.json and a shard's index.json). Nothing
here reads or writes a file, and the same input always gives the same text.

The pages are plain HTML with one inline style sheet: no script, no request to
another host, readable without the style sheet. Every value taken from a
document is escaped. A link to another site is written only for a URL under
https://github.com/, and a path inside the site only when it is made of plain
path characters; anything else is shown as text.

Standard library only.
"""

from __future__ import annotations

import datetime
import html
import math
import re
from typing import Any, Mapping, Optional, Sequence


class PageError(ValueError):
    """A document lacks a key the page needs, or holds the wrong kind of value."""


STATUSES: tuple[str, ...] = (
    "pass",
    "fail",
    "timeout",
    "crash",
    "skip",
    "blocked",
    "not-run",
)
# In order of severity.
FAILING: tuple[str, ...] = ("crash", "fail", "timeout", "not-run")
TEST_STATUSES: tuple[str, ...] = ("pass", "fail", "skip", "timeout")

_STAMP = r"[0-9]{8}T[0-9]{6}Z"
_SLUG = r"[A-Za-z0-9][A-Za-z0-9._+-]{0,99}"
SHARD_ID_RE: re.Pattern[str] = re.compile(
    rf"({_STAMP})-({_SLUG})-([0-9]{{1,20}})\.a([0-9]{{1,4}})"
)
LANDING = "bench/index.html"
DATA_ROOT = "data/bench/v1"

MAX_LANDING_RUNS = 120
MAX_LANDING_FAILURES = 50
MAX_CASE_ROWS = 500

_GITHUB_RE = re.compile(r"https://github\.com/[!-~]*")
_PATH_RE = re.compile(r"[A-Za-z0-9._+/-]+")
_IDENT_RE = re.compile(r"[^A-Za-z0-9_-]")
_STAMP_PARTS_RE = re.compile(
    r"([0-9]{4})([0-9]{2})([0-9]{2})T([0-9]{2})([0-9]{2})[0-9]{2}Z"
)
_ISO_RE = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):[0-9]{2}Z"
)
_SIGNALS = {
    1: "HUP",
    2: "INT",
    3: "QUIT",
    4: "ILL",
    6: "ABRT",
    7: "BUS",
    8: "FPE",
    9: "KILL",
    11: "SEGV",
    13: "PIPE",
    15: "TERM",
}
_STATUS_CLASS = {
    "pass": "pass",
    "fail": "fail",
    "timeout": "fail",
    "crash": "fail",
    "not-run": "fail",
    "missing": "fail",
    "skip": "skip",
    "partial": "skip",
    "blocked": "blocked",
    "complete": "pass",
}
_FAILING_WORDS = (
    ("fail", "failed"),
    ("timeout", "timed out"),
    ("crash", "crashed"),
    ("not-run", "did not run"),
)
_EMULATED = (
    '<p class="note">Everything here runs on emulated devices on shared GitHub'
    " runners. A failure is a real failure. A rate only shows change from one"
    " run to the next and says nothing about real hardware.</p>"
)

_HEAD = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; \
style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@@TITLE@@</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #ffffff; --fg: #1f2328; --muted: #59636e; --rule: #d1d9e0;
  --card: #f6f8fa; --series: #2a78d6;
  --pass: #1baf7a; --fail: #d03b3b; --skip: #fab219; --blocked: #8c959f;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --rule: #3d444d;
    --card: #151b23; --series: #5b9dea;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0 auto; padding: 1.5rem 1rem 3rem; max-width: 84rem;
  background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
}
h1 { font-size: 1.6rem; margin: 0 0 .25rem; }
h2 { font-size: 1.25rem; margin: 2.5rem 0 .25rem; padding-top: 1rem;
     border-top: 1px solid var(--rule); }
h3 { font-size: 1rem; margin: 1.5rem 0 .5rem; }
p { margin: .4rem 0; }
ul { margin: .4rem 0; padding-left: 1.3rem; }
a { color: var(--series); }
code, pre { font: 12px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
code { overflow-wrap: anywhere; }
pre { margin: .2rem 0 .5rem; padding: .4rem .6rem; max-height: 16rem;
  overflow: auto; white-space: pre-wrap; overflow-wrap: anywhere;
  background: var(--card); border: 1px solid var(--rule); border-radius: 6px; }
.muted { color: var(--muted); }
.note { border-left: 3px solid var(--skip); padding: .4rem .8rem;
        background: var(--card); margin: .8rem 0; }
.verdict { margin: .8rem 0; padding: .6rem .9rem; border-radius: 8px;
  font-size: 1.15rem; font-weight: 600; background: var(--card);
  border: 1px solid var(--rule); border-left: .5rem solid var(--pass); }
.verdict.fail { background: var(--fail); border-color: var(--fail);
  color: #ffffff; }
.verdict a { color: inherit; }
.scroll { overflow-x: auto; }
table { border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; vertical-align: top;
  padding: .3rem .9rem .3rem 0; border-bottom: 1px solid var(--rule); }
thead th { font-weight: 600; vertical-align: bottom; }
td.num, th.num { text-align: right; }
td.num { white-space: nowrap; }
td.key { min-width: 13rem; }
td.nowrap { white-space: nowrap; }
td.wrap { overflow-wrap: anywhere; }
table.grid { font-size: 12px; }
table.grid th, table.grid td { padding-right: .45rem; }
tr.main > td { border-bottom: 0; padding-bottom: .1rem; }
tr.more > td { padding-top: 0; }
.label { font-size: 12px; color: var(--muted); margin: .3rem 0 0; }
.st { display: inline-block; padding: 0 .4rem; border-radius: 4px;
  font-weight: 600; white-space: nowrap; border: 1px solid transparent; }
.st.pass { background: var(--pass); color: #04281b; }
.st.fail { background: var(--fail); color: #ffffff; }
.st.skip { background: var(--skip); color: #3a2800; }
.st.blocked { background: var(--blocked); color: #0d1117; }
.st.none, .st.muted { border-color: var(--rule); color: var(--muted); }
.badge { display: inline-block; padding: 0 .35rem; border-radius: 4px;
  border: 1px solid var(--fail); font-size: 11px; font-weight: 600; }
details { margin: .3rem 0; }
summary { cursor: pointer; }
summary > h2 { display: inline; border: 0; margin: 0; padding: 0; }
details.section { margin-top: 2.5rem; padding-top: 1rem;
  border-top: 1px solid var(--rule); }
.legend { font-size: 12px; color: var(--muted); list-style: none;
  padding: 0; display: flex; flex-wrap: wrap; gap: .3rem 1.4rem; }
footer { margin-top: 2.5rem; padding-top: 1rem; font-size: 13px;
  border-top: 1px solid var(--rule); }
</style>
</head>
<body>
"""
_TAIL = "</body>\n</html>\n"


# -- values ------------------------------------------------------------------


def _text(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _show(value: Any, absent: str = "-") -> str:
    return absent if value is None or value == "" else _text(value)


def _code(value: Any) -> str:
    return f"<code>{_text(value)}</code>"


def _get(obj: Any, key: str, where: str) -> Any:
    if not isinstance(obj, Mapping):
        raise PageError(f"{where} is not an object")
    if key not in obj:
        raise PageError(f"{where} has no '{key}'")
    return obj[key]


def _obj(obj: Any, key: str, where: str) -> Mapping[str, Any]:
    value = _get(obj, key, where)
    if not isinstance(value, Mapping):
        raise PageError(f"'{key}' of {where} is not an object")
    return value


def _seq(obj: Any, key: str, where: str) -> Sequence[Any]:
    value = _get(obj, key, where)
    if not isinstance(value, (list, tuple)):
        raise PageError(f"'{key}' of {where} is not a list")
    return value


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PageError(f"{value!r} is not a count")
    return int(value)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return float(value) if math.isfinite(value) else None
    except OverflowError:
        # An integer no float can hold.
        return None


def _counts(obj: Any, where: str, key: str = "n") -> list[int]:
    """The 'n' of a lane or a shard (one count per status, in STATUSES order),
    or its 'tests' (in TEST_STATUSES order)."""
    value = _seq(obj, key, where)
    size = len(STATUSES) if key == "n" else len(TEST_STATUSES)
    if len(value) != size:
        raise PageError(f"'{key}' of {where} does not hold {size} counts")
    return [_int(item) for item in value]


def _totals(obj: Any, key: str, where: str, names: Sequence[str]) -> list[int]:
    totals = _obj(obj, key, where)
    return [_int(_get(totals, name, f"'{key}' of {where}")) for name in names]


def _failing(counts: Sequence[int]) -> int:
    return sum(counts[STATUSES.index(status)] for status in FAILING)


def _counts_title(counts: Sequence[int]) -> str:
    return ", ".join(f"{name} {count}" for name, count in zip(STATUSES, counts))


def _plural(count: int, one: str, many: str) -> str:
    return f"{count} {one if count == 1 else many}"


def stamp_text(stamp: Any) -> str:
    """'YYYY-MM-DD HH:MM UTC' of a run stamp or of a record's ISO time."""
    for pattern in (_STAMP_PARTS_RE, _ISO_RE):
        found = pattern.fullmatch(stamp) if isinstance(stamp, str) else None
        if found:
            year, month, day, hour, minute = found.groups()
            return f"{year}-{month}-{day} {hour}:{minute} UTC"
    return "-" if stamp is None else str(stamp)


def _clock(when: Any) -> str:
    """'HH:MM' of a record's ISO time."""
    found = _ISO_RE.fullmatch(when) if isinstance(when, str) else None
    if found:
        return f"{found.group(4)}:{found.group(5)}"
    return "-" if when is None else str(when)


def _elapsed(start: Any, end: Any) -> str:
    try:
        seconds = int(
            (
                datetime.datetime.strptime(end, "%Y-%m-%dT%H:%M:%SZ")
                - datetime.datetime.strptime(start, "%Y-%m-%dT%H:%M:%SZ")
            ).total_seconds()
        )
    except (TypeError, ValueError):
        return "-"
    if seconds < 0:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    return f"{minutes // 60}h {minutes % 60:02d}m"


def _fixed(value: Any, digits: int = 1) -> str:
    number = _number(value)
    return "-" if number is None else f"{number:.{digits}f}"


def exit_text(code: Optional[int]) -> str:
    """An exit status with what it means: 134 is '134 (signal 6 ABRT)'."""
    if code is None:
        return "-"
    if isinstance(code, bool) or not isinstance(code, int):
        return str(code)
    if code == 124:
        return "124 (timed out)"
    if 129 <= code <= 192:
        signal = code - 128
        name = _SIGNALS.get(signal)
        return (
            f"{code} (signal {signal} {name})" if name else f"{code} (signal {signal})"
        )
    return str(code)


# -- links and ids -----------------------------------------------------------


def _github(url: Any) -> Optional[str]:
    return url if isinstance(url, str) and _GITHUB_RE.fullmatch(url) else None


def _relative(path: Any) -> Optional[str]:
    """path when it is safe to put in a link inside the site."""
    if not isinstance(path, str) or not _PATH_RE.fullmatch(path):
        return None
    if path.startswith("/") or ".." in path.split("/"):
        return None
    return path


def _link(href: Optional[str], label: str, absent: Optional[str] = None) -> str:
    """An anchor around label, which is HTML already; plain text with no href."""
    if href is None:
        return label if absent is None else absent
    return f'<a href="{_text(href)}">{label}</a>'


def _ident(*parts: Any) -> str:
    return _IDENT_RE.sub("-", "-".join(str(part) for part in parts))


def lane_anchor(lane: Any, rocm_version: Any) -> str:
    """The id of a lane's row on a run page."""
    return _ident("lane", lane, rocm_version)


def case_anchor(rocm_version: Any, case_key: Any, variant: Any, round_: Any) -> str:
    """The id of a failing case's row on a run page."""
    return _ident("case", rocm_version, case_key, variant, round_)


def run_page_path(shard_id: str) -> str:
    """The page of one shard, relative to the site root."""
    if not isinstance(shard_id, str) or not SHARD_ID_RE.fullmatch(shard_id):
        raise PageError(f"not a shard id: {shard_id!r}")
    return f"bench/runs/{shard_id}.html"


def _run_href(shard_id: Any, prefix: str, fragment: str = "") -> Optional[str]:
    """The run page of shard_id as seen from the landing page or a run page."""
    if not isinstance(shard_id, str) or not SHARD_ID_RE.fullmatch(shard_id):
        return None
    return f"{prefix}{shard_id}.html" + (f"#{fragment}" if fragment else "")


# -- markup ------------------------------------------------------------------


def _page(title: str, body: Sequence[str]) -> str:
    return _HEAD.replace("@@TITLE@@", _text(title)) + "\n".join(body) + "\n" + _TAIL


def _badge(status: Any, text: Optional[str] = None) -> str:
    name = _STATUS_CLASS.get(status, "none") if isinstance(status, str) else "none"
    return f'<span class="st {name}">{_text(status if text is None else text)}</span>'


def _cells(cells: Sequence[str], classes: Sequence[str] = ()) -> str:
    out = []
    for position, cell in enumerate(cells):
        name = classes[position] if position < len(classes) else ""
        out.append(f'<td class="{name}">{cell}</td>' if name else f"<td>{cell}</td>")
    return "".join(out)


def _table(
    head: Sequence[str],
    rows: Sequence[str],
    classes: Sequence[str] = (),
    grid: bool = False,
) -> list[str]:
    """A table in a box that scrolls sideways. head and rows are HTML: the
    heading of each column, and complete <tr> elements. A grid is a table of
    many narrow columns, one per lane."""
    heads = [
        (
            f'<th class="num">{title}</th>'
            if "num" in classes[at : at + 1]
            else f"<th>{title}</th>"
        )
        for at, title in enumerate(head)
    ]
    return [
        '<div class="scroll">',
        '<table class="grid">' if grid else "<table>",
        "<thead><tr>" + "".join(heads) + "</tr></thead>",
        "<tbody>",
        *rows,
        "</tbody>",
        "</table>",
        "</div>",
    ]


def _keys_details(keys: Sequence[Any]) -> str:
    if not keys:
        return "-"
    listed = "<br>".join(_code(key) for key in keys)
    return (
        f"<details><summary>{_plural(len(keys), 'case key', 'case keys')}</summary>"
        f"{listed}</details>"
    )


def _excerpt(text: Any) -> str:
    """An excerpt as the lines it was cut from: the collector joins with ' | '."""
    return "<pre>" + _text(str(text).replace(" | ", "\n")) + "</pre>"


def _verdict_text(counts: Sequence[int], failed_tests: int) -> str:
    by_status = dict(zip(STATUSES, counts))
    failing = _failing(counts)
    words = [word for status, word in _FAILING_WORDS if by_status[status]]
    phrase = "failed"
    if words:
        phrase = (
            words[-1] if len(words) == 1 else f"{', '.join(words[:-1])} or {words[-1]}"
        )
    parts = [
        f"{failing} of {_plural(sum(counts), 'case', 'cases')} {phrase}",
        f"{by_status['skip']} skipped",
        f"{by_status['blocked']} blocked",
        f"{by_status['pass']} passed",
    ]
    if failed_tests:
        parts.append(
            f"{_plural(failed_tests, 'probe or test', 'probes or tests')} failed"
        )
    return " · ".join(parts)


def _verdict(counts: Sequence[int], failed_tests: int, href: Optional[str]) -> str:
    bad = _failing(counts) > 0 or failed_tests > 0
    text = _link(href, _text(_verdict_text(counts, failed_tests)))
    return f'<p class="verdict {"fail" if bad else "ok"}">{text}</p>'


def _lane_cell(status: Any, counts: Sequence[int], filtered: Any) -> tuple[str, str]:
    """The text and the class of one lane of one run."""
    if status == "missing":
        return "missing", "fail"
    if status == "blocked":
        return "blocked", "blocked"
    failing = _failing(counts)
    if failing:
        tail = (
            " · partial" if status == "partial" else " · filtered" if filtered else ""
        )
        return f"{failing} failed{tail}", "fail"
    if status == "partial":
        return f"partial {counts[0]}", "skip"
    if filtered:
        return f"filtered {counts[0]}", "skip"
    return f"ok {counts[0]}", "pass"


def _lane_link(text: str, name: str, title: str, href: Optional[str]) -> str:
    title = _text(title)
    if href is None:
        return f'<span class="st {name}" title="{title}">{_text(text)}</span>'
    return (
        f'<a class="st {name}" title="{title}" href="{_text(href)}">'
        f"{_text(text)}</a>"
    )


# -- the helpers both test suites assert on ------------------------------------


def _sorted_counts(counts: Mapping[Any, int]) -> list[tuple[Any, int]]:
    return sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))


def failure_groups(attention: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """The failing entries of an index's 'attention', grouped by what failed.

    A group is one (status, reason, backend, via), where via is the engine, or
    the requested transport for a backend that has no engine.
    """
    groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for entry in attention:
        status = entry.get("status")
        if status not in FAILING:
            continue
        engine = entry.get("engine")
        via = (
            engine if isinstance(engine, str) and engine else entry.get("transport_req")
        )
        key = (
            str(status),
            _plain(entry.get("reason")),
            _plain(entry.get("backend")),
            _plain(via),
        )
        group = groups.get(key)
        if group is None:
            group = groups[key] = {
                "status": key[0],
                "reason": key[1],
                "backend": key[2],
                "via": key[3],
                "count": 0,
                "memory": {},
                "lanes": {},
                "rocm": set(),
                "rows": [0, 0],
                "duration": [None, None],
                "excerpt": _plain(entry.get("error_excerpt")),
            }
        group["count"] += 1
        memory = (_plain(entry.get("init_mem")), _plain(entry.get("tgt_mem")))
        group["memory"][memory] = group["memory"].get(memory, 0) + 1
        lane = _plain(entry.get("lane"))
        group["lanes"][lane] = group["lanes"].get(lane, 0) + 1
        group["rocm"].add(_plain(entry.get("rocm_version")))
        for position, name in enumerate(("rows_seen", "rows_expected")):
            rows = _number(entry.get(name))
            group["rows"][position] += int(rows) if rows is not None else 0
        duration = _number(entry.get("duration_s"))
        if duration is not None:
            low, high = group["duration"]
            group["duration"] = [
                duration if low is None else min(low, duration),
                duration if high is None else max(high, duration),
            ]
    out = []
    for group in groups.values():
        group["memory"] = [
            [init, tgt, count]
            for (init, tgt), count in sorted(
                group["memory"].items(), key=lambda item: (-item[1], item[0])
            )
        ]
        group["lanes"] = [
            [lane, count] for lane, count in _sorted_counts(group["lanes"])
        ]
        group["rocm"] = sorted(group["rocm"])
        out.append(group)
    out.sort(
        key=lambda group: (
            -group["count"],
            FAILING.index(group["status"]),
            group["reason"],
            group["backend"],
            group["via"],
        )
    )
    return out


def blocked_groups(blocked: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """An index's 'blocked' merged over ROCm versions: one group per lane and
    list of blocker codes, counting distinct case keys."""
    groups: dict[tuple[str, tuple[str, ...]], dict[str, Any]] = {}
    for entry in blocked:
        codes = entry.get("blocked_by")
        codes = (
            [str(code) for code in codes] if isinstance(codes, (list, tuple)) else []
        )
        key = (_plain(entry.get("lane")), tuple(codes))
        group = groups.setdefault(
            key, {"lane": key[0], "blocked_by": codes, "keys": set(), "rocm": set()}
        )
        keys = entry.get("case_keys")
        group["keys"].update(
            str(item) for item in (keys if isinstance(keys, (list, tuple)) else [])
        )
        group["rocm"].add(_plain(entry.get("rocm_version")))
    return [
        {
            "lane": group["lane"],
            "blocked_by": group["blocked_by"],
            "cases": len(group["keys"]),
            "case_keys": sorted(group["keys"]),
            "rocm": sorted(group["rocm"]),
        }
        for _, group in sorted(groups.items())
    ]


def _plain(value: Any) -> str:
    return "" if value is None else str(value)


# -- the run page --------------------------------------------------------------


def _data_href(path: Any, prefix: str, tail: str = "") -> Optional[str]:
    """A path relative to the data root as an href, or None when it is not one."""
    safe = _relative(path)
    return None if safe is None else f"{prefix}{DATA_ROOT}/{safe}{tail}"


def _data_link(path: Any, prefix: str, label: str, tail: str = "") -> str:
    href = _data_href(path, prefix, tail)
    if href is None:
        return f"{_text(label)} {_code(_plain(path) + tail)}"
    return _link(href, _text(label))


def _run_header(shard: Mapping[str, Any], links: Mapping[str, Any]) -> list[str]:
    where = "'shard' of index.json"
    stamp = _get(shard, "stamp", where)
    reason = _get(shard, "reason", where)
    branch = _get(shard, "branch", where)
    if branch is None:
        branch = _get(shard, "slug", where)
    sha = _get(shard, "sha", where)
    commit = _github(_get(links, "commit", "'links'"))
    if commit:
        commit_text = "commit " + _link(commit, _code(str(sha)[:10]))
    elif isinstance(sha, str) and sha:
        commit_text = "commit " + _code(sha[:40]) + " (no link)"
    else:
        commit_text = "unknown commit"
    nixl = _get(shard, "nixl_version", where)
    nixlbench = _get(shard, "nixlbench_version", where)
    identity = [
        f"branch <b>{_text(branch)}</b>",
        commit_text,
        f"NIXL {_show(nixl, 'version unknown')}",
    ]
    if nixlbench != nixl:
        identity.append(f"nixlbench {_show(nixlbench, 'version unknown')}")
    identity += [
        f"tier {_show(_get(shard, 'tier', where))}",
        f"reason {_show(reason)}",
        f"started {_text(stamp_text(_get(shard, 'started_at', where)))}",
        "took "
        + _text(
            _elapsed(
                _get(shard, "started_at", where), _get(shard, "finished_at", where)
            )
        ),
    ]
    features = shard.get("features")
    if isinstance(features, (list, tuple)) and features:
        identity.append("features " + ", ".join(_text(item) for item in features))
    run_text = (
        f"Actions run {_text(_get(shard, 'bench_run_id', where))}"
        f" attempt {_text(_get(shard, 'run_attempt', where))}"
    )
    source = "nightly run" if reason == "nightly" else "source run (wheels build)"
    data = _get(links, "data", "'links'")
    places = [
        _link(_github(_get(links, "actions_run", "'links'")), run_text),
        _link(
            _github(_get(links, "source_run", "'links'")),
            source,
            f"no {source} recorded",
        ),
        _link(
            _github(_get(links, "raw_records", "'links'")),
            "raw records",
            "raw records not archived",
        ),
        _link(
            _github(_get(links, "raw_logs", "'links'")),
            "logs tarball",
            "no logs archived",
        ),
        _link(
            _github(_get(links, "raw_release", "'links'")),
            "release of the raw files",
            "no release of the raw files",
        ),
        _data_link(_get(links, "matrix", "'links'"), "../../", "matrix"),
        _data_link(data, "../../", "index.json", "/index.json"),
    ]
    return [
        '<p class="muted"><a href="../../index.html">NIXL on ROCm: nightly results'
        '</a> › <a href="../index.html">nixlbench runs</a> › this run</p>',
        f"<h1>nixlbench run of {_text(stamp_text(stamp))}</h1>",
        "<p>" + " · ".join(identity) + "</p>",
        "<p>" + " · ".join(places) + "</p>",
    ]


def _tests_block(tests_attention: Sequence[Any]) -> list[str]:
    rows = []
    for entry in tests_attention:
        where = "an entry of 'tests_attention'"
        excerpt = _get(entry, "error_excerpt", where)
        rows.append(
            "<tr>"
            + _cells(
                [
                    _badge(_get(entry, "status", where)),
                    _text(_get(entry, "suite", where)),
                    _code(_get(entry, "test_id", where)),
                    _text(_get(entry, "lane", where)),
                    _text(_get(entry, "rocm_version", where)),
                    _excerpt(excerpt) if excerpt else "no error excerpt recorded",
                ],
                ["", "", "key"],
            )
            + "</tr>"
        )
    return [
        f'<h3 id="tests">Failed probes and tests ({len(rows)})</h3>',
        '<p class="muted">A failed probe is why later cases of the lane are'
        " skipped as device-suspect.</p>",
        *_table(["Status", "Suite", "Test", "Lane", "ROCm", "Excerpt"], rows),
    ]


def _groups_block(failing: Sequence[Mapping[str, Any]]) -> list[str]:
    groups = failure_groups(failing)
    head = [
        "Count",
        "Status",
        "Reason",
        "Backend",
        "Engine / transport",
        "Memory",
        "Lanes",
        "ROCm",
        "Rows seen/expected",
        "Duration",
    ]
    rows = []
    for group in groups:
        low, high = group["duration"]
        if low is None:
            duration = "-"
        elif _fixed(low) == _fixed(high):
            duration = f"{_fixed(low)} s"
        else:
            duration = f"{_fixed(low)}-{_fixed(high)} s"
        cells = [
            str(group["count"]),
            _badge(group["status"]),
            _show(group["reason"]),
            _show(group["backend"]),
            _show(group["via"]),
            ", ".join(
                f"{_show(init)}→{_show(tgt)} ×{count}"
                for init, tgt, count in group["memory"]
            ),
            ", ".join(f"{_show(lane)} ×{count}" for lane, count in group["lanes"]),
            ", ".join(_show(rocm) for rocm in group["rocm"]),
            f"{group['rows'][0]}/{group['rows'][1]}",
            duration,
        ]
        rows.append('<tr class="main">' + _cells(cells, ["num"]) + "</tr>")
        if group["status"] == "timeout":
            label = "last stderr lines of the first case (not the cause)"
        else:
            label = "error excerpt of the first case"
        detail = (
            f'<div class="label">{label}</div>{_excerpt(group["excerpt"])}'
            if group["excerpt"]
            else '<div class="label">no error excerpt recorded</div>'
        )
        rows.append(
            f'<tr class="more"><td></td><td colspan="{len(head) - 1}">{detail}</td></tr>'
        )
    return [
        f'<h3 id="groups">By cause ({_plural(len(groups), "group", "groups")})</h3>',
        *_table(head, rows, ["num"]),
    ]


def _case_detail(entry: Mapping[str, Any], logs: Optional[str]) -> str:
    where = "an entry of 'attention'"
    status = _get(entry, "status", where)
    excerpt = _get(entry, "error_excerpt", where)
    exit_init = _get(entry, "exit_init", where)
    exit_tgt = _get(entry, "exit_tgt", where)
    parts = []
    if status == "timeout":
        parts.append(
            f"<div><b>timed out after {_fixed(_get(entry, 'duration_s', where))} s</b>"
            f" · rows {_show(_get(entry, 'rows_seen', where))}"
            f"/{_show(_get(entry, 'rows_expected', where))}</div>"
        )
        label = "last stderr lines (not the cause)"
    else:
        label = "error excerpt"
    if excerpt:
        parts.append(f'<div class="label">{label}</div>{_excerpt(excerpt)}')
    else:
        parts.append('<div class="label">no error excerpt recorded</div>')
    both = [code for code in (exit_init, exit_tgt) if _number(code) not in (None, 0.0)]
    if len(both) == 2:
        parts.append(
            "<div>Both sides exited non-zero; the record does not say which side"
            " the excerpt is from: read <code>tgt.stderr.log</code> and"
            " <code>init.stderr.log</code>.</div>"
        )
    cmdline = _get(entry, "cmdline", where)
    if cmdline:
        parts.append(
            "<details><summary>Command line</summary>"
            f"<pre>{_text(cmdline)}</pre></details>"
        )
    log_ref = _get(entry, "log_ref", where)
    if logs is None:
        tail = f" (the case logged to {_code(str(log_ref) + '/')})" if log_ref else ""
        parts.append(f"<div>Logs: no logs archived{tail}.</div>")
    elif log_ref:
        parts.append(
            f"<div>Logs: {_code(str(log_ref) + '/')} in the "
            + _link(logs, "logs tarball")
            + " (<code>init.stdout.log</code>, <code>init.stderr.log</code>;"
            " two-sided cases also <code>init.ucx.log</code> and"
            " <code>tgt.*</code>).</div>"
        )
    else:
        parts.append("<div>Logs: the case has no log directory.</div>")
    return "".join(parts)


def _case_rows(
    failing: Sequence[Mapping[str, Any]],
    lane_status: Mapping[tuple[str, str], Any],
    logs: Optional[str],
) -> tuple[list[str], int]:
    """The rows of the case table, and how many cases are not in them.

    The cases of a lane that did not report are one row, not one each.
    """
    where = "an entry of 'attention'"
    unreported: dict[tuple[str, str], int] = {}
    for entry in failing:
        lane = (
            _plain(_get(entry, "lane", where)),
            _plain(_get(entry, "rocm_version", where)),
        )
        if entry.get("status") == "not-run" and lane_status.get(lane) == "missing":
            unreported[lane] = unreported.get(lane, 0) + 1
    rows: list[str] = []
    left = 0
    anchors: set[str] = set()
    reported: set[tuple[str, str]] = set()
    for entry in failing:
        lane = (_plain(entry.get("lane")), _plain(entry.get("rocm_version")))
        status = _get(entry, "status", where)
        collapsed = status == "not-run" and lane in unreported
        if collapsed and lane in reported:
            continue
        if len(rows) >= MAX_CASE_ROWS * 2:
            left += 1
            continue
        if collapsed:
            reported.add(lane)
            rows.append(
                f'<tr class="main"><td>{_badge(status)}</td><td colspan="12">lane'
                f" did not report: {_plural(unreported[lane], 'case', 'cases')}"
                f" of {_text(lane[0])} {_text(lane[1])}</td></tr>"
            )
            rows.append(
                '<tr class="more"><td></td><td colspan="12">No record of this'
                " lane reached the collector: look at the lane's job on the"
                " Actions run.</td></tr>"
            )
            continue
        case_key = _get(entry, "case_key", where)
        variant = _get(entry, "variant", where)
        round_ = _get(entry, "round", where)
        anchor = case_anchor(lane[1], case_key, variant, round_)
        ident = ""
        if anchor not in anchors:
            anchors.add(anchor)
            ident = f' id="{anchor}"'
        case = _code(case_key)
        if (variant, round_) != ("head", 0):
            case += (
                f' <span class="muted">variant {_text(variant)},'
                f" round {_text(round_)}</span>"
            )
        if _get(entry, "required", where) is True:
            case += ' <span class="badge">required</span>'
        requested = _get(entry, "transport_req", where)
        observed = _get(entry, "transport_obs", where)
        transport = _show(requested)
        if observed is not None or requested not in ("none", "", None):
            transport += f" → {_show(observed)}"
        duration = _get(entry, "duration_s", where)
        cells = [
            _badge(status),
            case,
            _text(lane[0]),
            _text(lane[1]),
            _show(_get(entry, "backend", where)),
            _show(_get(entry, "engine", where)),
            transport,
            f"{_show(_get(entry, 'init_mem', where))}"
            f"→{_show(_get(entry, 'tgt_mem', where))}",
            _show(_get(entry, "op", where)),
            _show(_get(entry, "reason", where)),
            f"{_text(exit_text(_get(entry, 'exit_init', where)))}"
            f" / {_text(exit_text(_get(entry, 'exit_tgt', where)))}",
            f"{_show(_get(entry, 'rows_seen', where))}"
            f"/{_show(_get(entry, 'rows_expected', where))}",
            "-" if _number(duration) is None else f"{_fixed(duration)} s",
        ]
        rows.append(
            f'<tr class="main"{ident}>'
            + _cells(cells, ["", "key"] + [""] * 9 + ["num", "num"])
            + "</tr>"
        )
        rows.append(
            f'<tr class="more"><td></td><td colspan="{len(cells) - 1}">'
            f"{_case_detail(entry, logs)}</td></tr>"
        )
    return rows, left


def _failures_section(
    index: Mapping[str, Any],
    failing: Sequence[Mapping[str, Any]],
    lane_status: Mapping[tuple[str, str], Any],
) -> list[str]:
    links = _obj(index, "links", "index.json")
    tests_attention = _seq(index, "tests_attention", "index.json")
    out = [
        '<h2 id="failures">Failed, timed out, crashed or not run'
        f" ({len(failing)})</h2>"
    ]
    if tests_attention:
        out += _tests_block(tests_attention)
    if not failing:
        out.append("<p>No case failed.</p>")
        return out
    out += _groups_block(failing)
    rows, left = _case_rows(
        failing, lane_status, _github(_get(links, "raw_logs", "'links'"))
    )
    head = [
        "Status",
        "Case",
        "Lane",
        "ROCm",
        "Backend",
        "Engine",
        "Transport (asked → seen)",
        "Memory",
        "Op",
        "Reason",
        "Exit init / tgt",
        "Rows seen/expected",
        "Duration",
    ]
    classes = [""] * 11 + ["num", "num"]
    out.append(f'<h3 id="cases">Every case ({len(failing)})</h3>')
    out += _table(head, rows, classes)
    if left:
        out.append(
            f"<p>{_plural(left, 'more case is', 'more cases are')} not shown: every"
            " case record is in "
            + _data_link(
                _get(links, "data", "'links'"),
                "../../",
                "cases.ndjson.gz",
                "/cases.ndjson.gz",
            )
            + ".</p>"
        )
    return out


def _skipped_section(attention: Sequence[Mapping[str, Any]]) -> list[str]:
    where = "an entry of 'attention'"
    groups: dict[tuple[str, str, str, str], list[str]] = {}
    total = 0
    for entry in attention:
        if _get(entry, "status", where) != "skip":
            continue
        total += 1
        key = (
            _plain(_get(entry, "reason", where)),
            _plain(_get(entry, "error_excerpt", where)),
            _plain(_get(entry, "lane", where)),
            _plain(_get(entry, "rocm_version", where)),
        )
        groups.setdefault(key, []).append(_plain(_get(entry, "case_key", where)))
    out = [f'<h2 id="skipped">Skipped ({total})</h2>']
    if not groups:
        out.append("<p>No case was skipped.</p>")
        return out
    rows = [
        "<tr>"
        + _cells(
            [
                str(len(keys)),
                _badge("skip") + " " + _show(reason),
                _show(excerpt),
                _text(lane),
                _text(rocm),
                _keys_details(sorted(keys)),
            ],
            ["num"],
        )
        + "</tr>"
        for (reason, excerpt, lane, rocm), keys in sorted(groups.items())
    ]
    out += _table(
        ["Cases", "Reason", "Why", "Lane", "ROCm", "Case keys"], rows, ["num"]
    )
    return out


def _lanes_section(
    lanes: Sequence[Any], shard: Mapping[str, Any], run_link: str
) -> list[str]:
    where = "an entry of 'lanes'"
    rows = []
    notes = []
    seen: set[str] = set()
    slug = _get(shard, "slug", "'shard' of index.json")
    nightly = _get(shard, "reason", "'shard' of index.json") == "nightly"
    for lane in lanes:
        name = _get(lane, "lane", where)
        rocm = _get(lane, "rocm_version", where)
        status = _get(lane, "status", where)
        counts = _counts(lane, where)
        tests = _counts(lane, where, "tests")
        anchor = lane_anchor(name, rocm)
        ident = ""
        if anchor not in seen:
            seen.add(anchor)
            ident = f' id="{anchor}"'
        state = _badge(status)
        if _get(lane, "filtered", where):
            state += " filtered"
        waits = lane.get("lane_blocked_by")
        if isinstance(waits, (list, tuple)) and waits:
            state += " (" + ", ".join(_text(code) for code in waits) + ")"
        if lane.get("degraded"):
            state += f" degraded: {_text(lane.get('degraded'))}"
        cells = [
            _text(name),
            _text(rocm),
            _show(_get(lane, "env_class", where)),
            _show(_get(lane, "timing_trust", where)),
            state,
            *(str(count) for count in counts),
            "/".join(str(count) for count in tests),
            _fixed(_get(lane, "calib_gbps", where), 2),
            _text(_clock(_get(lane, "started_at", where))),
            _text(_elapsed(lane.get("started_at"), _get(lane, "finished_at", where))),
        ]
        classes = [""] * 5 + ["num"] * 11
        rows.append(f"<tr{ident}>" + _cells(cells, classes) + "</tr>")
        if status != "blocked":
            # A nightly names each branch's artifacts after its slug; a wheels
            # run started by hand does not, and the records do not say which
            # of the two a bench started by hand was pointed at.
            artifact = f"bench-diagnostics-{name}-{rocm}"
            note = f"artifact {_code(f'{artifact}-{slug}')}"
            if not nightly:
                note = (
                    f"artifact {_code(artifact)}, or {_code(f'{artifact}-{slug}')}"
                    " when the source run is a nightly,"
                )
            notes.append(
                f"<li>{_text(name)} {_text(rocm)}: guest and NIC diagnostics:"
                f" {note} on the {run_link}; expires with the run</li>"
            )
    head = [
        "Lane",
        "ROCm",
        "Environment",
        "Timing",
        "Run",
        *STATUSES,
        "Tests p/f/s/t",
        "Calibration GB/s",
        "Started (UTC)",
        "Took",
    ]
    out = ['<h2 id="lanes">Lanes</h2>']
    out += _table(head, rows, [""] * 5 + ["num"] * 11)
    if notes:
        out += ['<ul class="muted">', *notes, "</ul>"]
    return out


def _coverage_section(blocked: Sequence[Any], docs: Optional[str]) -> list[str]:
    where = "an entry of 'blocked'"
    records = 0
    versions = set()
    for entry in blocked:
        records += _int(_get(entry, "cases", where))
        _seq(entry, "blocked_by", where)
        _seq(entry, "case_keys", where)
        _get(entry, "lane", where)
        versions.add(_plain(_get(entry, "rocm_version", where)))
    versions.discard("none")
    groups = blocked_groups(blocked)
    cases = sum(group["cases"] for group in groups)
    out = [
        '<h2 id="coverage">Blocked: what each lane waits for'
        f" ({_plural(cases, 'case', 'cases')})</h2>"
    ]
    if not groups:
        out.append("<p>No case is blocked.</p>")
        return out
    if records > cases:
        out.append(
            f'<p class="muted">{records} records over'
            f" {_plural(len(versions), 'ROCm version', 'ROCm versions')}.</p>"
        )
    href = None if docs is None else docs.split("#", 1)[0] + "#blocker-codes"
    rows = [
        "<tr>"
        + _cells(
            [
                _text(group["lane"]),
                ", ".join(_link(href, _text(code)) for code in group["blocked_by"])
                or "-",
                str(group["cases"]),
                ", ".join(_text(rocm) for rocm in group["rocm"]),
                _keys_details(group["case_keys"]),
            ],
            ["", "", "num"],
        )
        + "</tr>"
        for group in groups
    ]
    out += _table(
        ["Lane", "Waits for", "Cases", "ROCm", "Case keys"], rows, ["", "", "num"]
    )
    return out


def _ratios_section(ratios: Sequence[Any]) -> list[str]:
    where = "an entry of 'ratios'"
    rows = []
    for ratio in ratios:
        points = _seq(ratio, "points", where)
        if _get(ratio, "status", where) == "ok" and points:
            shown = []
            for point in points:
                if not isinstance(point, (list, tuple)) or len(point) != 5:
                    raise PageError(f"{where} has a point that is not five values")
                block, batch, num, den, value = point
                shown.append(
                    f"{_text(block)}×{_text(batch)}: <b>{_fixed(value, 4)}</b>"
                    f" ({_fixed(num, 3)} / {_fixed(den, 3)})"
                )
            result = "<br>".join(shown)
        else:
            result = f"unavailable: {_show(ratio.get('why'), 'no reason given')}"
        rows.append(
            "<tr>"
            + _cells(
                [
                    _code(_get(ratio, "key", where)),
                    _show(_get(ratio, "kind", where)),
                    _text(_get(ratio, "lane", where)),
                    _text(_get(ratio, "rocm_version", where)),
                    f"{_code(_get(ratio, 'num', where))}"
                    f" over {_code(_get(ratio, 'den', where))}",
                    result,
                ],
                ["key", "", "", "", "key", "nowrap"],
            )
            + "</tr>"
        )
    out = [
        '<details class="section">',
        f'<summary><h2 id="ratios">Declared ratios ({len(rows)})</h2></summary>',
        _EMULATED,
        '<p class="muted">Each point is block size × batch size: the ratio, then'
        " the two rates in GB/s it was taken from, each the median over the"
        " rounds of that case.</p>",
    ]
    if rows:
        out += _table(
            ["Pair", "Kind", "Lane", "ROCm", "Numerator over denominator", "Points"],
            rows,
        )
    else:
        out.append("<p>The matrix declares no ratio for the lanes of this run.</p>")
    out.append("</details>")
    return out


def _notes(title: str, items: Sequence[Any]) -> list[str]:
    if not items:
        return []
    return [
        f"<p>{title}:</p>",
        "<ul>",
        *(f"<li>{_text(item)}</li>" for item in items),
        "</ul>",
    ]


def _run_footer(index: Mapping[str, Any], links: Mapping[str, Any]) -> list[str]:
    out = ["<footer>"]
    collect = _get(index, "collect", "index.json")
    if collect is None:
        out.append("<p>Collector summary not archived.</p>")
    else:
        where = "'collect' of index.json"
        rejected = _get(collect, "rejected", where)
        warnings = _seq(collect, "warnings", where)
        required = _seq(collect, "required_failures", where)
        out.append(
            f"<p>Collector: {_text(rejected)} rejected,"
            f" {_plural(len(warnings), 'warning', 'warnings')},"
            f" {_plural(len(required), 'required failure', 'required failures')}.</p>"
        )
        out += _notes("Required cases that failed", required)
        out += _notes("Collector warnings", warnings)
    out += _notes("Publisher warnings", _seq(index, "warnings", "index.json"))
    data = _get(links, "data", "'links'")
    files = [
        _data_link(data, "../../", f"{name}.ndjson.gz", f"/{name}.ndjson.gz")
        for name in ("runs", "cases", "samples", "counters", "tests")
    ]
    files.append(_data_link(data, "../../", "index.json", "/index.json"))
    files.append(_data_link(_get(links, "matrix", "'links'"), "../../", "matrix"))
    files.append(f'<a href="../../{DATA_ROOT}/manifest.json">manifest.json</a>')
    out.append("<p>Data of this run: " + " · ".join(files) + ".</p>")
    docs = _github(_get(links, "docs", "'links'"))
    out.append(
        "<p>"
        + _link(docs, "How the bench programme works", "")
        + (" · " if docs else "")
        + '<a href="../index.html">all runs</a></p>'
    )
    out.append("</footer>")
    return out


def _run(index: Mapping[str, Any]) -> str:
    shard = _obj(index, "shard", "index.json")
    links = _obj(index, "links", "index.json")
    lanes = _seq(index, "lanes", "index.json")
    attention = _seq(index, "attention", "index.json")
    counts = _totals(index, "totals", "index.json", STATUSES)
    tests = _totals(index, "tests", "index.json", TEST_STATUSES)
    failed_tests = (
        tests[TEST_STATUSES.index("fail")] + tests[TEST_STATUSES.index("timeout")]
    )
    failing = [
        entry
        for entry in attention
        if _get(entry, "status", "an entry of 'attention'") in FAILING
    ]
    lane_status = {
        (
            _plain(_get(lane, "lane", "an entry of 'lanes'")),
            _plain(_get(lane, "rocm_version", "an entry of 'lanes'")),
        ): _get(lane, "status", "an entry of 'lanes'")
        for lane in lanes
    }
    skipped = sum(entry.get("status") == "skip" for entry in attention)
    blocked = _seq(index, "blocked", "index.json")
    ratios = _seq(index, "ratios", "index.json")
    run_link = _link(_github(_get(links, "actions_run", "'links'")), "Actions run")
    branch = _get(shard, "branch", "'shard' of index.json")
    stamp = _get(shard, "stamp", "'shard' of index.json")
    title = (
        f"nixlbench run {stamp_text(stamp)}"
        f" · {branch if branch is not None else _get(shard, 'slug', 'shard')}"
    )
    jumps = [
        f'<a href="#failures">failures ({len(failing)})</a>',
        f'<a href="#skipped">skipped ({skipped})</a>',
        '<a href="#lanes">lanes</a>',
        '<a href="#coverage">blocked</a>',
        '<a href="#ratios">ratios</a>',
    ]
    body = [
        *_run_header(shard, links),
        _verdict(counts, failed_tests, "#failures"),
        "<p>Jump to: " + " · ".join(jumps) + "</p>",
        *_failures_section(index, failing, lane_status),
        *_skipped_section(attention),
        *_lanes_section(lanes, shard, run_link),
        *_coverage_section(blocked, _github(_get(links, "docs", "'links'"))),
        *_ratios_section(ratios),
        *_run_footer(index, links),
    ]
    return _page(title, body)


def render_run(index: Mapping[str, Any]) -> str:
    """The page of one run, from that shard's index.json."""
    try:
        return _run(index)
    except PageError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise PageError(f"index.json: {type(exc).__name__}: {exc}") from exc


# -- the landing page ----------------------------------------------------------


def _landing_failures(branch: Mapping[str, Any], where: str) -> list[str]:
    attention = _seq(branch, "attention", where)
    total = _int(_get(branch, "attention_total", where))
    shards = _seq(branch, "shards", where)
    first = _run_href(shards[0] if shards else None, "runs/", "failures")
    entry_where = f"an entry of 'attention' of {where}"
    # The cases of a lane that did not report are one row, as on the run page,
    # which has no row of its own for any of them.
    unreported: dict[tuple[str, str], int] = {}
    for lane in _seq(branch, "lanes", where):
        lane_where = f"an entry of 'lanes' of {where}"
        if _get(lane, "status", lane_where) == "missing":
            key = (
                _plain(_get(lane, "lane", lane_where)),
                _plain(_get(lane, "rocm_version", lane_where)),
            )
            unreported[key] = _counts(lane, lane_where)[STATUSES.index("not-run")]
    rows: list[str] = []
    shown = 0
    reported: set[tuple[str, str]] = set()
    for entry in attention:
        if len(rows) >= MAX_LANDING_FAILURES:
            break
        status = _get(entry, "status", entry_where)
        rocm = _get(entry, "rocm_version", entry_where)
        key = (_plain(_get(entry, "lane", entry_where)), _plain(rocm))
        if status == "not-run" and key in unreported:
            if key in reported:
                continue
            reported.add(key)
            shown += unreported[key]
            href = _run_href(_get(entry, "shard", entry_where), "runs/", "failures")
            cases = _plural(unreported[key], "case", "cases")
            rows.append(
                "<tr>"
                + _cells(
                    [
                        _badge(status),
                        _link(href, f"lane did not report: {cases}"),
                        _text(key[0]),
                        _text(key[1]),
                        _show(_get(entry, "reason", entry_where)),
                        "",
                    ],
                    ["", "key", "", "", "", "key"],
                )
                + "</tr>"
            )
            continue
        shown += 1
        case_key = _get(entry, "case_key", entry_where)
        variant = _get(entry, "variant", entry_where)
        round_ = _get(entry, "round", entry_where)
        href = _run_href(
            _get(entry, "shard", entry_where),
            "runs/",
            case_anchor(rocm, case_key, variant, round_),
        )
        case = _link(href, _code(case_key))
        if (variant, round_) != ("head", 0):
            case += f' <span class="muted">variant {_text(variant)}, round {_text(round_)}</span>'
        if _get(entry, "required", entry_where) is True:
            case += ' <span class="badge">required</span>'
        excerpt = _show(_get(entry, "error_excerpt", entry_where), "")
        if status == "timeout" and excerpt:
            excerpt = (
                f'<span class="muted">last stderr line, not the cause:</span> {excerpt}'
            )
        rows.append(
            "<tr>"
            + _cells(
                [
                    _badge(status),
                    case,
                    _text(_get(entry, "lane", entry_where)),
                    _text(rocm),
                    _show(_get(entry, "reason", entry_where)),
                    excerpt,
                ],
                ["", "key", "", "", "", "key"],
            )
            + "</tr>"
        )
    if not rows:
        return ["<p>No case failed.</p>"]
    more = _link(first, f"all {total} on the run page")
    return [
        f"<h3>Failed, timed out, crashed or not run ({total})</h3>",
        *_table(["Status", "Case", "Lane", "ROCm", "Reason", "Excerpt"], rows),
        f"<p>Showing {min(shown, total)} of {total}: {more}, with command lines,"
        " exit codes and log paths.</p>",
    ]


def _landing_tests(branch: Mapping[str, Any], where: str) -> list[str]:
    tests = _seq(branch, "tests_attention", where)
    total = _int(_get(branch, "tests_attention_total", where))
    if not tests:
        return []
    entry_where = f"an entry of 'tests_attention' of {where}"
    items = []
    for entry in tests:
        href = _run_href(_get(entry, "shard", entry_where), "runs/", "tests")
        name = (
            f"{_get(entry, 'suite', entry_where)}/{_get(entry, 'test_id', entry_where)}"
        )
        items.append(
            f"<li>{_badge(_get(entry, 'status', entry_where))} "
            + _link(href, _code(name))
            + f" on {_text(_get(entry, 'lane', entry_where))}"
            f" {_text(_get(entry, 'rocm_version', entry_where))}</li>"
        )
    return [
        f"<p>Failed probes and tests ({total}); a failed probe is why later cases"
        " of the lane are skipped as device-suspect:</p>",
        "<ul>",
        *items,
        "</ul>",
    ]


def _landing_lanes(branch: Mapping[str, Any], where: str) -> list[str]:
    lanes = _seq(branch, "lanes", where)
    if not lanes:
        return []
    lane_where = f"an entry of 'lanes' of {where}"
    heads = []
    cells = []
    for lane in lanes:
        name = _get(lane, "lane", lane_where)
        rocm = _get(lane, "rocm_version", lane_where)
        counts = _counts(lane, lane_where)
        text, css = _lane_cell(
            _get(lane, "status", lane_where), counts, lane.get("filtered")
        )
        href = _run_href(
            _get(lane, "shard", lane_where), "runs/", lane_anchor(name, rocm)
        )
        heads.append(f"{_text(name)}<br>{_text(rocm)}")
        cells.append(_lane_link(text, css, _counts_title(counts), href))
    return _table(heads, ["<tr>" + _cells(cells) + "</tr>"], grid=True)


def _newer(branch: Mapping[str, Any], where: str) -> str:
    """The line about a run after the headline's; empty when there is none."""
    newer = _get(branch, "newer", where)
    if newer is None:
        return ""
    newer_where = f"'newer' of {where}"
    ids = _seq(newer, "shards", newer_where)
    text = _link(
        _run_href(ids[0] if ids else None, "runs/"),
        _text(stamp_text(_get(newer, "stamp", newer_where))),
    )
    extra = "".join(
        ", " + _link(_run_href(item, "runs/"), f"run {position}")
        for position, item in enumerate(ids[1:], 2)
    )
    return (
        f"newer manual run: {text}{extra}"
        f" ({_text(_get(newer, 'failing', newer_where))} failed)"
    )


def _landing_branch(slug: str, branch: Mapping[str, Any], ident: str) -> list[str]:
    where = f"branch '{slug}' of latest.json"
    name = _get(branch, "branch", where)
    headline = _get(branch, "headline", where)
    shards = _seq(branch, "shards", where)
    counts = _totals(branch, "totals", where, STATUSES)
    tests = _totals(branch, "tests", where, TEST_STATUSES)
    failed_tests = (
        tests[TEST_STATUSES.index("fail")] + tests[TEST_STATUSES.index("timeout")]
    )
    label = (
        "latest nightly"
        if headline == "nightly"
        else "latest run, started by hand; no nightly bench published yet"
    )
    sha = _get(branch, "sha", where)
    about = [
        label,
        _text(stamp_text(_get(branch, "stamp", where))),
        f"commit {_code(str(sha)[:10])}" if sha else "unknown commit",
        f"tier {_show(_get(branch, 'tier', where))}",
    ]
    out = [
        f"<h2{ident}>" f"{_text(name if name is not None else slug)}</h2>",
        '<p class="muted">' + " · ".join(about) + "</p>",
        _verdict(
            counts,
            failed_tests,
            _run_href(shards[0] if shards else None, "runs/", "failures"),
        ),
    ]
    newer = _newer(branch, where)
    if newer:
        out.append(f"<p>{newer}</p>")
    if len(shards) > 1:
        runs = ", ".join(
            _link(_run_href(item, "runs/"), _text(item)) for item in shards
        )
        out.append(f'<p class="muted">Merged over the runs of that night: {runs}</p>')
    out += _landing_lanes(branch, where)
    out += _landing_tests(branch, where)
    out += _landing_failures(branch, where)
    return out


def _landing_stale(stale: Sequence[tuple[str, Mapping[str, Any]]]) -> list[str]:
    items = []
    for slug, branch in stale:
        where = f"branch '{slug}' of latest.json"
        name = _get(branch, "branch", where)
        counts = _totals(branch, "totals", where, STATUSES)
        shards = _seq(branch, "shards", where)
        newer = _newer(branch, where)
        items.append(
            f"<li>{_text(name if name is not None else slug)}"
            f" · {_text(stamp_text(_get(branch, 'stamp', where)))}"
            f" · {_text(_verdict_text(counts, 0))} · "
            + _link(
                _run_href(shards[0] if shards else None, "runs/"),
                "run page",
                "no run page",
            )
            + (f" · {newer}" if newer else "")
            + "</li>"
        )
    return [
        "<details>",
        f"<summary>Branches with no recent run ({len(items)})</summary>",
        "<ul>",
        *items,
        "</ul>",
        "</details>",
    ]


def _landing_runs(shards: Sequence[Any]) -> list[str]:
    where = "a shard of manifest.json"
    shown = shards[:MAX_LANDING_RUNS]
    keys = sorted({str(key) for shard in shown for key in _obj(shard, "lanes", where)})
    rows = []
    for shard in shown:
        ident = _get(shard, "id", where)
        lanes = _obj(shard, "lanes", where)
        superseded = _obj(shard, "superseded_by", where)
        counts = _counts(shard, where)
        sha = _get(shard, "sha", where)
        branch = _get(shard, "branch", where)
        cells = [
            _link(
                _run_href(ident, "runs/"),
                _text(stamp_text(_get(shard, "stamp", where))),
            ),
            _text(branch if branch is not None else _get(shard, "slug", where)),
            _code(str(sha)[:10]) if sha else "-",
            _show(_get(shard, "tier", where)),
            _show(_get(shard, "reason", where)),
        ]
        for key in keys:
            if key not in lanes:
                cells.append("-")
                continue
            lane = lanes[key]
            lane_where = f"lane '{key}' of {where}"
            lane_counts = _counts(lane, lane_where)
            title = _counts_title(lane_counts)
            name, _, rocm = key.partition("/")
            href = _run_href(ident, "runs/", lane_anchor(name, rocm))
            if key in superseded:
                cells.append(
                    _lane_link(
                        "superseded",
                        "muted",
                        f"superseded by {superseded[key]}; {title}",
                        href,
                    )
                )
                continue
            text, css = _lane_cell(
                _get(lane, "status", lane_where), lane_counts, lane.get("filtered")
            )
            cells.append(_lane_link(text, css, title, href))
        failing = _failing(counts)
        total = (
            f'<span class="st fail">{failing} failed</span>'
            if failing
            else f'<span class="st pass">ok {counts[0]}</span>'
        )
        cells.append(
            f'<span title="{_text(_counts_title(counts))}">{total}'
            f" of {sum(counts)}</span>"
        )
        classes = ["", "wrap"] + [""] * (len(cells) - 3) + ["nowrap"]
        rows.append("<tr>" + _cells(cells, classes) + "</tr>")
    head = ["Run", "Branch", "Commit", "Tier", "Reason"]
    for key in keys:
        name, _, rocm = key.partition("/")
        head.append(f"{_text(name)}<br>{_text(rocm)}")
    head.append("Cases")
    out = ['<h2 id="runs">All runs</h2>']
    out += _table(head, rows, grid=True)
    if len(shards) > len(shown):
        out.append(
            f"<p>The newest {len(shown)} of {len(shards)} runs; the rest are in"
            " manifest.json.</p>"
        )
    out += [
        '<ul class="legend">',
        f'<li>{_badge("pass", "ok N")} every case that ran passed, N of them</li>',
        f'<li>{_badge("fail", "N failed")} failed, timed out, crashed or did not'
        " run</li>",
        f'<li>{_badge("skip", "partial N")} the lane stopped early</li>',
        f'<li>{_badge("skip", "filtered N")} the run was asked for a part of the'
        " lane</li>",
        f'<li>{_badge("blocked")} the lane waits for work named on the run'
        " page</li>",
        f'<li>{_badge("missing")} the lane did not report</li>',
        '<li><span class="st muted">superseded</span> a later run of the same'
        " night replaced this lane</li>",
        "<li>- the run did not include the lane</li>",
        "<li>hover over a cell for all seven counts, skipped included</li>",
        "</ul>",
    ]
    return out


def _landing(manifest: Mapping[str, Any], latest: Mapping[str, Any]) -> str:
    shards = _seq(manifest, "shards", "manifest.json")
    docs = _github(_get(_obj(manifest, "links", "manifest.json"), "docs", "'links'"))
    branches = _obj(latest, "branches", "latest.json")
    ordered = sorted(
        ((str(slug), branch) for slug, branch in branches.items()),
        key=lambda item: item[0],
    )
    ordered.sort(
        key=lambda item: _plain(_get(item[1], "stamp", f"branch '{item[0]}'")),
        reverse=True,
    )
    nav = [
        '<a href="../index.html">Nightly results</a>',
        '<a href="../wheels/index.html">Wheels</a>',
    ]
    if docs:
        nav.append(_link(docs, "How the bench programme works"))
    body = [
        "<h1>NIXL on ROCm: nixlbench runs</h1>",
        "<p>" + " · ".join(nav) + "</p>",
        _EMULATED,
    ]
    if not shards:
        body.append("<p>No bench run has been published yet.</p>")
    stale = []
    anchors: set[str] = set()
    for slug, branch in ordered:
        if _get(branch, "stale", f"branch '{slug}' of latest.json"):
            stale.append((slug, branch))
            continue
        anchor = _ident("branch", slug)
        ident = "" if anchor in anchors else f' id="{anchor}"'
        anchors.add(anchor)
        body += _landing_branch(slug, branch, ident)
    if stale:
        body += _landing_stale(stale)
    if shards:
        body += _landing_runs(shards)
    body += [
        '<h2 id="data">Data</h2>',
        f'<p><a href="../{DATA_ROOT}/manifest.json">manifest.json</a> lists every'
        " published run and its files; "
        f'<a href="../{DATA_ROOT}/latest.json">latest.json</a> is the latest run'
        " of each branch as shown above.</p>",
    ]
    return _page("NIXL on ROCm: nixlbench runs", body)


def render_landing(manifest: Mapping[str, Any], latest: Mapping[str, Any]) -> str:
    """bench/index.html, from manifest.json and latest.json."""
    try:
        return _landing(manifest, latest)
    except PageError:
        raise
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise PageError(f"landing page: {type(exc).__name__}: {exc}") from exc
