import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from taskloom.adapters.linux import (
    OwnerLock,
    atomic_json,
    boot_id,
    executor_socket,
    process_ticks,
    stop_group,
)
from taskloom.contracts.models import ClientConfig, Contract


def write_configuration(path: Path, config: Contract) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            key
            + " = "
            + (
                str(value).lower()
                if isinstance(value, bool)
                else json.dumps(value, ensure_ascii=False)
            )
            for key, value in config.model_dump().items()
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def process_record(role: str, process: subprocess.Popen[bytes]) -> dict[str, Any]:
    return {
        "role": role,
        "pid": process.pid,
        "ticks": process_ticks(process.pid),
        "boot_id": boot_id(),
    }


def stop_record(record: dict[str, Any]) -> bool:
    return stop_group(record["pid"], record["ticks"], record["boot_id"], grace=4)


def stop_supervisor(directory: Path, timeout: float = 15) -> bool:
    """A foreground supervisor may not lead its process group; signal its verified PID."""
    manifest = directory / "supervisor.json"
    if not manifest.exists():
        return True
    record = json.loads(manifest.read_text())
    if record["state"] == "stopped":
        return True
    if record["boot_id"] != boot_id():
        return True
    if process_ticks(record["pid"]) != record["ticks"]:
        return all([stop_record(child) for child in record.get("children", [])])
    try:
        os.kill(record["pid"], signal.SIGTERM)
    except ProcessLookupError:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = json.loads(manifest.read_text())
        if current["state"] == "stopped":
            return True
        if process_ticks(record["pid"]) != record["ticks"]:
            return all([stop_record(child) for child in current.get("children", [])])
        time.sleep(0.05)
    return False


def daemonize(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    if os.fork() > 0:
        os._exit(0)
    os.chdir(directory)
    os.umask(0o077)
    for path in Path("/proc/self/fd").iterdir():
        try:
            descriptor = int(path.name)
            if descriptor > 2:
                os.close(descriptor)
        except OSError:
            pass
    input_fd = os.open(os.devnull, os.O_RDONLY)
    output_fd = os.open(directory / "daemon.log", os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600)
    os.dup2(input_fd, 0)
    os.dup2(output_fd, 1)
    os.dup2(output_fd, 2)
    for descriptor in (input_fd, output_fd):
        if descriptor > 2:
            os.close(descriptor)


def supervise(base: Path, config: ClientConfig, daemon: bool = False) -> None:
    directory = base / "clients" / config.profile
    directory.mkdir(parents=True, exist_ok=True)
    if daemon:
        daemonize(directory)
    owner = OwnerLock(directory / "supervisor.lock")
    configuration = directory / "active.toml"
    write_configuration(configuration, config)
    children: dict[str, subprocess.Popen[bytes]] = {}
    stopping = False

    def request_stop(_signal: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    def persist() -> None:
        atomic_json(
            directory / "supervisor.json",
            {
                "pid": os.getpid(),
                "ticks": process_ticks(os.getpid()),
                "boot_id": boot_id(),
                "children": [process_record(role, process) for role, process in children.items()],
                "state": "stopping" if stopping else "running",
            },
        )

    def spawn(role: str) -> subprocess.Popen[bytes]:
        with (directory / (role + ".log")).open("ab") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "taskloom",
                    role,
                    "--state-dir",
                    str(base),
                    "--config",
                    str(configuration),
                ],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
            )
        children[role] = process
        persist()
        return process

    def stop_child(role: str) -> None:
        process = children.get(role)
        if process is not None:
            stop_record(process_record(role, process))
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass

    try:
        failures = 0
        while not stopping:
            executor = children.get("executor")
            if executor is None or executor.poll() is not None:
                stop_child("client")
                spawn("executor")
                deadline = time.monotonic() + 10
                transport = httpx.HTTPTransport(uds=str(executor_socket(directory)))
                with httpx.Client(
                    transport=transport, base_url="http://executor", timeout=0.5
                ) as client:
                    while not stopping and time.monotonic() < deadline:
                        try:
                            if client.get("/health/ready").status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.05)
                if stopping:
                    break
                spawn("client")
            client_process = children.get("client")
            if client_process is None or client_process.poll() is not None:
                failures += 1
                time.sleep(min(0.2 * 2 ** min(failures, 5), 5))
                if not stopping:
                    spawn("client")
            else:
                failures = 0
            time.sleep(0.2)
    finally:
        stopping = True
        persist()
        stop_child("client")
        stop_child("executor")
        owner.close()
        atomic_json(
            directory / "supervisor.json",
            {
                "pid": os.getpid(),
                "ticks": process_ticks(os.getpid()),
                "boot_id": boot_id(),
                "state": "stopped",
                "children": [],
            },
        )
