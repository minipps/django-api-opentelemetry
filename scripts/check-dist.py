#!/usr/bin/env python3
"""Verify a built wheel/sdist pair without importing the source checkout.

The real check runs ``twine check``, installs the wheel into a fresh temporary
venv, imports it from outside /workspace, rebuilds a wheel from the sdist and
repeats the import smoke test. Packaging itself is delegated to ``build``,
``uv`` and ``twine``.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

SMOKE_CODE = """\
import importlib.metadata as metadata
import pathlib

import django_ninja_opentelemetry

origin = pathlib.Path(django_ninja_opentelemetry.__file__).resolve()
workspace = pathlib.Path("/workspace").resolve()
if origin.is_relative_to(workspace):
    raise SystemExit(f"imported from the source checkout: {origin}")

distribution = metadata.distribution("django-ninja-opentelemetry")
root = pathlib.Path(distribution.locate_file("")).resolve()
if not origin.is_relative_to(root):
    raise SystemExit(f"{origin} is outside installed distribution {root}")

from django.conf import settings

settings.configure(
    SECRET_KEY="check-dist-not-secret",
    DEBUG=False,
    ALLOWED_HOSTS=["*"],
    INSTALLED_APPS=["django_ninja_opentelemetry"],
    MIDDLEWARE=[],
    ROOT_URLCONF=__name__,
    DATABASES={},
    DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
    USE_TZ=True,
)

import django

django.setup()

from django.http import HttpResponse
from django.test import Client
from django.urls import path


def health(request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [path("health/", health)]
response = Client().get("/health/")
if response.status_code != 200:
    raise SystemExit(f"plain Django request returned {response.status_code}")
if response.content != b"ok":
    raise SystemExit(f"plain Django request returned {response.content!r}")
print(f"imported {origin} from {root}; plain Django request ok")
"""


class CheckError(RuntimeError):
    """A distribution check failed."""


def find_artifacts(directory: Path) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise CheckError(
            f"expected exactly one wheel and one sdist in {directory}; "
            f"found wheels={[p.name for p in wheels]} "
            f"sdists={[p.name for p in sdists]}"
        )
    return wheels[0], sdists[0]


def run(command: list[str], *, cwd: Path | None = None) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, check=True, cwd=cwd)


def clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    return env


def make_venv(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    venv = directory / "venv"
    run([sys.executable, "-m", "venv", "--without-pip", str(venv)])
    return venv


def install_and_smoke(wheel: Path, directory: Path, label: str) -> None:
    venv = make_venv(directory)
    python = venv / "bin" / "python"
    run(
        ["uv", "pip", "install", "--python", str(python), str(wheel.resolve())],
        cwd=directory,
    )
    print(f"+ smoke {label}: {wheel.name}", flush=True)
    subprocess.run(
        [str(python), "-c", SMOKE_CODE],
        check=True,
        cwd=directory,
        env=clean_environment(),
    )


def extract_sdist(sdist: Path, directory: Path) -> Path:
    destination = directory / "source"
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(sdist, "r:gz") as archive:
        archive.extractall(destination, filter="data")
    projects = sorted(
        path
        for path in destination.iterdir()
        if path.is_dir() and (path / "pyproject.toml").is_file()
    )
    if len(projects) != 1:
        raise CheckError(f"expected one project directory in {sdist}, found {projects}")
    return projects[0]


def rebuild_from_sdist(sdist: Path, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = extract_sdist(sdist, directory)
    out = directory / "rebuilt"
    run(
        [
            sys.executable,
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(out),
            str(source),
        ],
        cwd=directory,
    )
    wheels = sorted(out.glob("*.whl"))
    if len(wheels) != 1:
        raise CheckError(f"expected one rebuilt wheel in {out}, found {wheels}")
    return wheels[0]


def check(dist_dir: Path) -> None:
    wheel, sdist = find_artifacts(dist_dir)
    with tempfile.TemporaryDirectory(prefix="check-dist-", dir="/tmp") as raw:
        directory = Path(raw)
        run(
            [
                sys.executable,
                "-m",
                "twine",
                "check",
                "--strict",
                str(wheel.resolve()),
                str(sdist.resolve()),
            ],
            cwd=directory,
        )
        install_and_smoke(wheel, directory / "wheel", "wheel")
        rebuilt = rebuild_from_sdist(sdist, directory / "sdist")
        install_and_smoke(rebuilt, directory / "rebuilt", "sdist wheel")


def self_test() -> None:
    with tempfile.TemporaryDirectory(prefix="check-dist-self-", dir="/tmp") as raw:
        directory = Path(raw)
        (directory / "demo-1.0-py3-none-any.whl").touch()
        (directory / "demo-1.0.tar.gz").touch()
        wheel, sdist = find_artifacts(directory)
        assert wheel.name.endswith(".whl")
        assert sdist.name.endswith(".tar.gz")

        (directory / "other-1.0-py3-none-any.whl").touch()
        try:
            find_artifacts(directory)
        except CheckError:
            pass
        else:
            raise AssertionError("duplicate wheels were not rejected")

    print("check-dist self-test passed")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dist", nargs="?", type=Path, help="directory with one wheel/sdist"
    )
    parser.add_argument("--self-test", action="store_true")
    args = parser.parse_args()

    if args.self_test:
        self_test()
        return 0
    if args.dist is None:
        parser.error("a distribution directory is required unless --self-test is used")

    try:
        check(args.dist)
    except (CheckError, subprocess.CalledProcessError) as exc:
        print(f"check-dist: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
