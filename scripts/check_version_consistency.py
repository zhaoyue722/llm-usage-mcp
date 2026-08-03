#!/usr/bin/env python3
"""Verify the release version agrees everywhere it's written down.

The version lives in **three** places that have to stay in lockstep:

    pyproject.toml   [project].version          -> what PyPI publishes
    server.json      .version                   -> the registry entry
    server.json      .packages[N].version       -> the package the entry points at

Nothing enforces that today, and they're edited by hand. The failure is
quiet and annoying: PyPI gets 0.1.3 while the MCP Registry still claims
0.1.2, so `uvx llm-usage-mcp` installs a package whose version doesn't
match the entry that advertised it.

Two ways to run it:

    uv run python scripts/check_version_consistency.py
        Internal consistency only. Run this *before* tagging.

    uv run python scripts/check_version_consistency.py v0.1.3
        Also assert every field equals the tag (leading "v" optional).
        This is the form `release.yml` runs, so a tag that disagrees
        with the metadata fails before anything is published.

Exit code is 0 on agreement, 1 on any mismatch, with every location and
its value printed so the fix is obvious.
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def collect_versions() -> dict[str, str]:
    """Map every "where it's written" -> the version string found there.

    Keys are human-readable locations rather than paths, because they go
    straight into the error message and the reader needs to know which
    file *and* which field to edit.
    """
    pyproject = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    server = json.loads((_ROOT / "server.json").read_text())

    found = {
        "pyproject.toml  [project].version": pyproject["project"]["version"],
        "server.json     .version": server["version"],
    }
    # `packages` is a list; in practice we ship one PyPI entry, but the
    # schema allows several and each carries its own version. Check them
    # all rather than assuming index 0 is the only one.
    for index, package in enumerate(server.get("packages", [])):
        found[f"server.json     .packages[{index}].version"] = package["version"]
    return found


def check(expected: str | None = None) -> list[str]:
    """Return a list of human-readable problems; empty means all good.

    `expected` is the tag version to match against (without the leading
    "v"). When `None`, only internal agreement is checked — useful
    locally before you've decided on a tag.
    """
    found = collect_versions()
    problems: list[str] = []

    distinct = set(found.values())
    if len(distinct) > 1:
        problems.append(f"version differs across release metadata: {sorted(distinct)}")

    if expected is not None:
        mismatched = {where: value for where, value in found.items() if value != expected}
        if mismatched:
            problems.append(f"release metadata does not match the tag ({expected})")

    return problems


def main(argv: list[str]) -> int:
    expected = argv[1].lstrip("v") if len(argv) > 1 else None

    found = collect_versions()
    problems = check(expected)

    if not problems:
        version = next(iter(found.values()))
        target = f" (matches tag v{expected})" if expected else ""
        print(f"version {version} is consistent across {len(found)} locations{target}")
        return 0

    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    print("\nfound:", file=sys.stderr)
    for where, value in found.items():
        marker = "  " if expected is None or value == expected else "->"
        print(f"  {marker} {value:<12} {where}", file=sys.stderr)
    if expected is not None:
        print(f"\nexpected everywhere: {expected}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
