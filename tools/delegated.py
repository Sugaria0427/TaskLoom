"""Run a project command in a transient, user-owned systemd cgroup scope."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> None:
    relative = Path("/proc/self/cgroup").read_text().strip().split("::", 1)[1]
    current = Path("/sys/fs/cgroup") / relative.lstrip("/")
    if not current.name.startswith("taskloom-"):
        result = subprocess.run(
            [
                "systemd-run",
                "--user",
                "--scope",
                "--quiet",
                "--collect",
                "--unit=taskloom-" + str(os.getpid()),
                "--property=Delegate=yes",
                sys.executable,
                str(Path(__file__).resolve()),
                *sys.argv[1:],
            ],
            check=False,
        )
        raise SystemExit(result.returncode)
    control = current / "control"
    control.mkdir(exist_ok=True)
    members = (current / "cgroup.procs").read_text().split()
    if any(int(pid) != os.getpid() for pid in members):
        raise SystemExit("Unexpected processes in the new TaskLoom scope")
    (control / "cgroup.procs").write_text(str(os.getpid()))
    available = set((current / "cgroup.controllers").read_text().split())
    if not {"cpu", "memory", "pids"} <= available:
        raise SystemExit("The user scope does not delegate cpu, memory and pids")
    (current / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    jobs = current / "jobs"
    jobs.mkdir(exist_ok=True)
    (jobs / "cgroup.subtree_control").write_text("+cpu +memory +pids")
    env = os.environ.copy()
    env["TASKLOOM_CGROUP_ROOT"] = str(jobs)
    try:
        raise SystemExit(subprocess.call(sys.argv[1:], env=env))
    finally:
        # The cgroup scope and its children are collected by systemd after exit.
        pass


if __name__ == "__main__":
    main()
