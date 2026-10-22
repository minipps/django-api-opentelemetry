#!/usr/bin/env python3
"""Validate a release tag against project metadata and optionally git ancestry."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from packaging.version import InvalidVersion, Version

TAG_PATTERN = re.compile(r"^v(?P<version>.+)$")


class CheckError(RuntimeError):
    """A release gate check failed."""


def parse_tag_version(tag: str) -> Version:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise CheckError(f"tag {tag!r} must start with 'v'")
    try:
        version = Version(match.group("version"))
    except InvalidVersion as exc:
        raise CheckError(f"tag {tag!r} is not a valid PEP 440 version") from exc
    if str(version) != match.group("version"):
        raise CheckError(f"tag {tag!r} is not normalized PEP 440 (expected v{version})")
    if len(version.release) != 3:
        raise CheckError(f"tag {tag!r} must use vMAJOR.MINOR.PATCH")
    return version


def project_version(pyproject: Path) -> Version:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        value = data["project"]["version"]
    except (OSError, KeyError, TypeError, tomllib.TOMLDecodeError) as exc:
        raise CheckError(f"cannot read project.version from {pyproject}") from exc
    try:
        return Version(str(value))
    except InvalidVersion as exc:
        raise CheckError(f"project.version {value!r} is not PEP 440") from exc


def check_ancestor(repo: Path, tag: str, main_ref: str) -> None:
    try:
        commit = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--verify", f"{tag}^{{commit}}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, main_ref],
            check=True,
        )
    except FileNotFoundError as exc:
        raise CheckError("git is required for --check-ancestor") from exc
    except subprocess.CalledProcessError as exc:
        raise CheckError(f"tag {tag!r} is not an ancestor of {main_ref!r}") from exc


def release_metadata(tag: str, pyproject: Path) -> tuple[Version, bool]:
    tag_version = parse_tag_version(tag)
    version = project_version(pyproject)
    if tag_version != version:
        raise CheckError(
            f"tag version {tag_version} does not match project.version {version}"
        )
    return version, tag_version.is_prerelease


def self_test() -> None:
    assert parse_tag_version("v1.2.3") == Version("1.2.3")
    assert parse_tag_version("v1.2.3rc1").is_prerelease
    assert parse_tag_version("v1.2.3.dev1").is_prerelease
    assert not parse_tag_version("v1.2.3").is_prerelease
    for bad in ("1.2.3", "vnot-a-version", "v1.0", "v01.2.3"):
        try:
            parse_tag_version(bad)
        except CheckError:
            pass
        else:
            raise AssertionError(f"invalid tag accepted: {bad}")
    print("check-release self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="release tag, for example v1.2.3rc1")
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument(
        "--check-ancestor",
        action="store_true",
        help="require the tag commit to be on --main-ref",
    )
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--main-ref", default="origin/main")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0
    if not args.tag:
        parser.error("--tag is required unless --self-test is used")

    try:
        version, prerelease = release_metadata(args.tag, args.pyproject)
        if args.check_ancestor:
            check_ancestor(args.repo, args.tag, args.main_ref)
    except CheckError as exc:
        print(f"check-release: {exc}", file=sys.stderr)
        return 1

    # Machine-readable key=value lines for GitHub Actions $GITHUB_OUTPUT.
    print(f"version={version}")
    print(f"prerelease={'true' if prerelease else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
