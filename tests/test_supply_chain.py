"""Release metadata must not turn untrusted tag text into workflow outputs."""

from __future__ import annotations

import runpy
from pathlib import Path

import pytest

RELEASE_CHECK = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts" / "check-release.py")
)


@pytest.mark.parametrize(
    "tag",
    [
        "v0.1.0\nprerelease=false",
        "v0.1.0\r\nversion=9.9.9",
        "v0.1.0; echo compromised",
        "v0.1.0$(id)",
        "v00.1.0",
    ],
)
def test_release_tag_rejects_workflow_output_and_shell_injection(tag: str) -> None:
    with pytest.raises(RELEASE_CHECK["CheckError"]):
        RELEASE_CHECK["parse_tag_version"](tag)


def test_release_metadata_rejects_tag_version_mismatch(tmp_path: Path) -> None:
    metadata = tmp_path / "pyproject.toml"
    metadata.write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")

    with pytest.raises(RELEASE_CHECK["CheckError"], match="does not match"):
        RELEASE_CHECK["release_metadata"]("v9.9.9", metadata)

    assert RELEASE_CHECK["release_metadata"]("v0.1.0", metadata)[1] is False
