"""Dependency-free project entry point; Windows delegates execution to WSL."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UV_VERSION = "0.13.1"
UV_SHA256 = "eadd9d27c134ab5ac1ffd4473b97a741407f3ea238288f03121fc4deddf90220"
KEY = hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]
DATA = Path.home() / ".local/share/taskloom" / KEY
VENV = Path(
    os.environ.get("UV_PROJECT_ENVIRONMENT", str(Path.home() / ".cache/taskloom/venvs" / KEY))
)
UV = ROOT / ".toolchain/uv/uv"
DEV_HELP = """usage: python3 tools/project.py dev [--scenario NAME] [--tool-slots N] [--pool-capacity N] [other taskloom dev options]

WP6 scenarios (tool slots / model pool capacity):
  unconstrained      2 / 2 (default)
  tool_constrained   1 / 2
  api_constrained    2 / 1
  both_constrained   1 / 1

--tool-slots and --pool-capacity may repeat the selected scenario's values;
mismatches and unknown scenarios are rejected by taskloom dev.
"""


def command(args: list[str], *, cwd: Path = ROOT) -> None:
    env = os.environ.copy()
    env["UV_PROJECT_ENVIRONMENT"] = str(VENV)
    env["UV_LINK_MODE"] = "copy"
    subprocess.run(args, cwd=cwd, env=env, check=True)


def run(*args: str) -> None:
    command([str(UV), "run", "--locked", *args])


def wheel_path(root: Path) -> Path:
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    return root / "dist" / f"taskloom-{version}-py3-none-any.whl"


def doctor() -> None:
    controllers = Path("/sys/fs/cgroup/cgroup.controllers")
    info = {
        "os": platform.platform(),
        "python": platform.python_version(),
        "workspace": str(ROOT),
        "environment": str(VENV),
        "state_dir": str(DATA),
        "uv_installed": UV.is_file(),
        "cgroup_v2": controllers.exists(),
        "controllers": controllers.read_text().split() if controllers.exists() else [],
        "delegated_root": os.environ.get("TASKLOOM_CGROUP_ROOT"),
    }
    print(json.dumps(info, ensure_ascii=False, indent=2))


def bootstrap() -> None:
    if sys.version_info[:2] != (3, 12):
        raise SystemExit("Use Python 3.12 in Ubuntu WSL2.")
    if not UV.exists():
        UV.parent.mkdir(parents=True, exist_ok=True)
        url = (
            f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
            "uv-x86_64-unknown-linux-gnu.tar.gz"
        )
        with urllib.request.urlopen(url, timeout=90) as response:
            data = response.read()
        if hashlib.sha256(data).hexdigest() != UV_SHA256:
            raise SystemExit("uv release checksum mismatch")
        with tempfile.TemporaryFile() as archive:
            archive.write(data)
            archive.seek(0)
            with tarfile.open(fileobj=archive, mode="r:gz") as package:
                member = package.extractfile("uv-x86_64-unknown-linux-gnu/uv")
                if member is None:
                    raise SystemExit("uv binary missing from verified archive")
                UV.write_bytes(member.read())
        UV.chmod(0o755)
        (UV.parent / "provenance.json").write_text(
            json.dumps({"version": UV_VERSION, "url": url, "sha256": UV_SHA256}, indent=2)
        )
    if not (ROOT / "uv.lock").exists():
        command([str(UV), "lock"])
    command([str(UV), "sync", "--locked"])
    doctor()


def check() -> None:
    run("ruff", "format", "--check", "src", "tests", "tools")
    run("ruff", "check", "src", "tests", "tools")
    run("mypy")
    run("lint-imports")


def verify() -> None:
    command([str(UV), "sync", "--locked"])
    check()
    run("pytest", "-q", "tests/unit", "tests/integration")
    command([str(UV), "build"])
    run("pytest", "-q", "tests/smoke")
    wheel = wheel_path(ROOT)
    import zipfile

    with zipfile.ZipFile(wheel) as package:
        names = package.namelist()
        assert any(name.endswith("web/index.html") for name in names)
        assert not any(
            part in name.split("/")
            for name in names
            for part in ("research", "backup", "tests", "__pycache__", ".env")
        )
    print(json.dumps({"verify": "passed", "wheel": wheel.name}))


def isolated() -> None:
    with tempfile.TemporaryDirectory(prefix="taskloom-isolated-") as directory:
        base = Path(directory)
        clean = base / "source"
        clean.mkdir()
        for name in (
            "src",
            "tests",
            "tools",
            "configs",
            "pyproject.toml",
            "uv.lock",
            "README.md",
            "AGENTS.md",
            ".python-version",
        ):
            original = ROOT / name
            destination = clean / name
            if original.is_dir():
                shutil.copytree(
                    original,
                    destination,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".pytest_cache"),
                )
            else:
                shutil.copy2(original, destination)
        env = os.environ.copy()
        env["TASKLOOM_UV"] = str(UV)
        env["UV_PROJECT_ENVIRONMENT"] = str(base / "source-env")
        subprocess.run(
            [sys.executable, str(clean / "tools/project.py"), "verify"],
            cwd=clean,
            env=env,
            check=True,
        )
        wheel = wheel_path(clean)
        installed = base / "installed"
        command([str(UV), "venv", str(installed), "--python", sys.executable], cwd=base)
        python = installed / "bin/python"
        requirements = base / "requirements.txt"
        command(
            [
                str(UV),
                "--quiet",
                "export",
                "--locked",
                "--no-dev",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "--output-file",
                str(requirements),
            ],
            cwd=clean,
        )
        command([str(UV), "pip", "sync", "--python", str(python), str(requirements)], cwd=base)
        command(
            [str(UV), "pip", "install", "--python", str(python), "--no-deps", str(wheel)], cwd=base
        )
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        subprocess.run(
            [str(python), "-I", "-m", "taskloom", "--version"], cwd=base, env=env, check=True
        )
        subprocess.run(
            [str(python), "-m", "taskloom", "self-test", "--state-dir", str(base / "state")],
            cwd=base,
            env=env,
            check=True,
        )
        print(json.dumps({"isolated": "passed", "wheel": wheel.name}))


def main() -> None:
    global UV
    if os.name == "nt":
        path = str(ROOT).replace("\\", "/")
        linux = "/mnt/" + path[0].lower() + path[2:]
        raise SystemExit(
            subprocess.call(
                [
                    "wsl.exe",
                    "-d",
                    "Ubuntu",
                    "--cd",
                    linux,
                    "--exec",
                    "python3",
                    linux + "/tools/project.py",
                    *sys.argv[1:],
                ]
            )
        )
    if "TASKLOOM_UV" in os.environ:
        UV = Path(os.environ["TASKLOOM_UV"])
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action",
        choices=[
            "doctor",
            "bootstrap",
            "lock",
            "check",
            "test",
            "verify",
            "verify-isolated",
            "dev",
            "client",
            "stop-client",
            "stop",
            "format",
        ],
    )
    parser.add_argument("extra", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.action == "dev" and args.extra == ["--help"]:
        print(DEV_HELP)
        return
    if args.action == "doctor":
        doctor()
    elif args.action == "bootstrap":
        bootstrap()
    elif args.action == "lock":
        command([str(UV), "lock"])
    elif args.action == "check":
        check()
    elif args.action == "format":
        run("ruff", "format", "src", "tests", "tools")
        run("ruff", "check", "--fix", "src", "tests", "tools")
    elif args.action == "test":
        paths = [str(ROOT / "tests" / name) for name in args.extra] or [
            "tests/unit",
            "tests/integration",
            "tests/smoke",
        ]
        if "linux" in args.extra and not os.environ.get("TASKLOOM_CGROUP_ROOT"):
            command(
                [
                    sys.executable,
                    str(ROOT / "tools/delegated.py"),
                    str(UV),
                    "run",
                    "--locked",
                    "pytest",
                    "-q",
                    *paths,
                ]
            )
        else:
            run("pytest", "-q", *paths)
    elif args.action == "verify":
        verify()
    elif args.action == "verify-isolated":
        isolated()
    elif args.action == "client":
        arguments = ["taskloom", "supervisor", "--state-dir", str(DATA), *args.extra]
        if os.environ.get("TASKLOOM_CGROUP_ROOT"):
            run(*arguments)
        else:
            command(
                [
                    sys.executable,
                    str(ROOT / "tools/delegated.py"),
                    str(UV),
                    "run",
                    "--locked",
                    *arguments,
                ]
            )
    elif args.action == "stop-client":
        run("taskloom", "stop-client", "--state-dir", str(DATA), *args.extra)
    elif args.action == "dev" and not os.environ.get("TASKLOOM_CGROUP_ROOT"):
        command(
            [
                sys.executable,
                str(ROOT / "tools/delegated.py"),
                str(UV),
                "run",
                "--locked",
                "taskloom",
                "dev",
                "--state-dir",
                str(DATA),
                *args.extra,
            ]
        )
    else:
        run("taskloom", args.action, "--state-dir", str(DATA), *args.extra)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
