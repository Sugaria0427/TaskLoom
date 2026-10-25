import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from taskloom.adapters.linux import CgroupDomain, process_ticks
from taskloom.bootstrap.development import free_port, self_test
from taskloom.contracts.models import identity

pytestmark = pytest.mark.linux


def cgroup_domain() -> CgroupDomain:
    root = os.environ.get("TASKLOOM_CGROUP_ROOT")
    if not root:
        pytest.skip("Run through tools/project.py test linux to obtain a user cgroup scope")
    return CgroupDomain(Path(root))


def test_actual_memory_limit_and_group_cleanup() -> None:
    domain = cgroup_domain()
    group = domain.create(identity(), 32 * 1024 * 1024)
    assert group is not None
    code = (
        "import os,pathlib;"
        f"pathlib.Path({str(group / 'cgroup.procs')!r}).write_text(str(os.getpid()));"
        "data=bytearray(128*1024*1024)\n"
        "for i in range(0,len(data),4096): data[i]=1\n"
        "print(len(data))"
    )
    process = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        assert process.wait(timeout=10) == -signal.SIGKILL
        events = dict(line.split() for line in (group / "memory.events").read_text().splitlines())
        assert int(events["oom_kill"]) >= 1
        metrics = domain.metrics(str(group))
        assert metrics["oom_kill"] >= 1
        assert metrics["peak_memory"] > 0
        assert "cpu_usec" in metrics
        assert domain.cleanup(str(group))
        assert not group.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if group.exists():
            domain.cleanup(str(group))


def test_daemon_detaches_restarts_client_and_stops_children() -> None:
    cgroup_domain()
    with tempfile.TemporaryDirectory(prefix="tl-daemon-") as directory:
        base = Path(directory)
        port = free_port()
        config = base / "config.toml"
        config.write_text(f"client_port = {port}\n")
        subprocess.run(
            [
                sys.executable,
                "-m",
                "taskloom",
                "supervisor",
                "--state-dir",
                str(base),
                "--config",
                str(config),
                "--daemon",
            ],
            check=True,
            timeout=10,
        )
        manifest = base / "clients/alice/supervisor.json"
        supervisor = None
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if manifest.exists():
                    data = json.loads(manifest.read_text())
                    supervisor = data["pid"]
                    children = {item["role"]: item for item in data["children"]}
                    if "client" in children:
                        break
                time.sleep(0.05)
            assert supervisor is not None
            assert os.getsid(supervisor) != os.getsid(os.getpid())
            assert os.getpgrp() != os.getpgid(supervisor)
            original = children["client"]
            os.kill(original["pid"], signal.SIGKILL)
            deadline = time.monotonic() + 20
            replaced = None
            while time.monotonic() < deadline:
                data = json.loads(manifest.read_text())
                children = {item["role"]: item for item in data["children"]}
                if "client" in children and children["client"]["pid"] != original["pid"]:
                    replaced = children["client"]
                    break
                time.sleep(0.05)
            assert replaced is not None
            os.kill(supervisor, signal.SIGTERM)
            deadline = time.monotonic() + 12
            while time.monotonic() < deadline:
                if json.loads(manifest.read_text())["state"] == "stopped":
                    break
                time.sleep(0.05)
            assert json.loads(manifest.read_text())["state"] == "stopped"
            assert process_ticks(replaced["pid"]) != replaced["ticks"]
        finally:
            if supervisor and process_ticks(supervisor):
                try:
                    os.kill(supervisor, signal.SIGTERM)
                except ProcessLookupError:
                    pass


def test_entire_workflow_uses_and_reclaims_actual_cgroups() -> None:
    cgroup_domain()
    with tempfile.TemporaryDirectory(prefix="tl-cgroup-") as directory:
        base = Path(directory)
        assert self_test(base)["self_test"] == "passed"
        states = list((base / "clients/alice/executions").glob("*.json"))
        assert states
        for path in states:
            state = json.loads(path.read_text())
            assert state["cgroup_path"]
            assert state["quiescent"]
            assert not Path(state["cgroup_path"]).exists()


def test_foreground_supervisor_stops_without_owning_process_group() -> None:
    from taskloom.adapters.supervisor import stop_supervisor

    cgroup_domain()
    with tempfile.TemporaryDirectory(prefix="tl-foreground-") as directory:
        base = Path(directory)
        config = base / "client.toml"
        config.write_text(f"client_port = {free_port()}\n")
        with (base / "supervisor.log").open("wb") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "taskloom",
                    "supervisor",
                    "--state-dir",
                    str(base),
                    "--config",
                    str(config),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
            )
        try:
            manifest = base / "clients/alice/supervisor.json"
            for _ in range(200):
                if manifest.exists():
                    record = json.loads(manifest.read_text())
                    if any(child["role"] == "client" for child in record["children"]):
                        break
                time.sleep(0.05)
            else:
                raise AssertionError("Foreground supervisor did not start")
            assert os.getpgid(process.pid) != process.pid
            children = record["children"]
            assert stop_supervisor(manifest.parent)
            assert process.wait(timeout=5) == 0
            assert json.loads(manifest.read_text())["state"] == "stopped"
            assert all(process_ticks(child["pid"]) != child["ticks"] for child in children)
        finally:
            if process.poll() is None:
                stop_supervisor(base / "clients/alice")
                process.wait(timeout=5)
