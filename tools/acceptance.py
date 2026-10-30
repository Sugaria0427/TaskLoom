"""One-command acceptance demonstrations; Windows uses the project's WSL environment."""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    if os.name == "nt":
        absolute = str(ROOT).replace("\\", "/")
        linux = "/mnt/" + absolute[0].lower() + absolute[2:]
        return subprocess.call(
            [
                "wsl.exe",
                "-d",
                "Ubuntu",
                "--cd",
                linux,
                "--exec",
                "python3",
                "tools/acceptance.py",
                *sys.argv[1:],
            ]
        )
    import project

    uv = Path(os.environ.get("TASKLOOM_UV", str(project.UV)))
    if not uv.is_file():
        print("请先在仓库根目录运行 python tools/project.py bootstrap。")
        return 1
    project.command(
        [str(uv), "run", "--locked", "python", "tools/acceptance/runner.py", *sys.argv[1:]]
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as error:
        raise SystemExit(error.returncode) from None
    except KeyboardInterrupt:
        raise SystemExit(130) from None
