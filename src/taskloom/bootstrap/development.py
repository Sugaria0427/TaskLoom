import json
import secrets
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx

from taskloom.adapters.linux import OwnerLock, atomic_json
from taskloom.adapters.supervisor import process_record, stop_record, write_configuration
from taskloom.contracts.models import ClientConfig, PlatformConfig, PoolConfig

WP6_CAPACITIES: dict[str, tuple[int, int]] = {
    "unconstrained": (2, 2),
    "tool_constrained": (1, 2),
    "api_constrained": (2, 1),
    "both_constrained": (1, 1),
}


def free_port() -> int:
    with socket.socket() as connection:
        connection.bind(("127.0.0.1", 0))
        return int(connection.getsockname()[1])


class Topology:
    def __init__(
        self,
        base: Path,
        *,
        ephemeral: bool = False,
        client_port: int | None = None,
        platform_port: int | None = None,
        pool_port: int | None = None,
        second_client_port: int | None = None,
        with_second_client: bool = False,
        scenario: str = "unconstrained",
        tool_slots: int | None = None,
        pool_capacity: int | None = None,
    ) -> None:
        if scenario not in WP6_CAPACITIES:
            raise ValueError(f"Unknown WP6 scenario: {scenario}")
        expected_tool_slots, expected_pool_capacity = WP6_CAPACITIES[scenario]
        if tool_slots is not None and tool_slots != expected_tool_slots:
            raise ValueError(f"{scenario} requires tool_slots={expected_tool_slots}")
        if pool_capacity is not None and pool_capacity != expected_pool_capacity:
            raise ValueError(f"{scenario} requires pool_capacity={expected_pool_capacity}")
        self.scenario = scenario
        self.tool_slots = expected_tool_slots
        self.pool_capacity = expected_pool_capacity
        self.base = base
        chosen: set[int] = set()

        def port(value: int | None, default: int) -> int:
            number = free_port() if ephemeral else value or default
            while number in chosen and ephemeral:
                number = free_port()
            chosen.add(number)
            return number

        self.client_port = port(client_port, 8795)
        self.platform_port = port(platform_port, 8796)
        self.pool_port = port(pool_port, 8797)
        self.second_client_port = port(second_client_port, 8798) if with_second_client else None
        self.client_url = f"http://127.0.0.1:{self.client_port}"
        self.platform_url = f"http://127.0.0.1:{self.platform_port}"
        self.pool_url = f"http://127.0.0.1:{self.pool_port}"
        self.second_client_url = (
            f"http://127.0.0.1:{self.second_client_port}" if self.second_client_port else None
        )
        self.pool_key = ""
        self.children: list[subprocess.Popen[bytes]] = []
        self.records: list[dict[str, Any]] = []
        self.lock: OwnerLock | None = None

    def client_config(self, profile: str, port: int) -> ClientConfig:
        return ClientConfig(
            profile=profile,
            tenant_id=profile,
            client_port=port,
            platform_url=self.platform_url,
            tool_slots=self.tool_slots,
        )

    def pool_config(self) -> PoolConfig:
        return PoolConfig(key=self.pool_key, capacity=self.pool_capacity)

    def platform_config(self) -> PlatformConfig:
        return PlatformConfig(
            pool_url=self.pool_url,
            pool_key=self.pool_key,
            pool_capacity=self.pool_capacity,
            model=self.pool_config().model,
        )

    def _spawn(
        self, role: str, configuration: Path, port: int | None = None, label: str | None = None
    ) -> None:
        args = [
            sys.executable,
            "-m",
            "taskloom",
            role,
            "--state-dir",
            str(self.base),
            "--config",
            str(configuration),
        ]
        if port is not None:
            args.extend(["--port", str(port)])
        with (self.base / ((label or role) + ".log")).open("ab") as stream:
            import os

            environment = os.environ.copy()
            for key in (
                "TASKLOOM_PLATFORM_URL",
                "TASKLOOM_API_KEY",
                "TASKLOOM_POOL_URL",
                "TASKLOOM_POOL_KEY",
                "TASKLOOM_POOL_KIND",
                "TASKLOOM_MODEL",
            ):
                environment.pop(key, None)
            process = subprocess.Popen(
                args,
                stdout=stream,
                stderr=stream,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                env=environment,
            )
        self.children.append(process)
        self.records.append(process_record(label or role, process))
        self._save()

    def _save(self) -> None:
        atomic_json(
            self.base / "dev.json",
            {
                "children": self.records,
                "client_url": self.client_url,
                "platform_url": self.platform_url,
                "second_client_url": self.second_client_url,
            },
        )

    def _provision_client(self, profile: str, port: int) -> None:
        directory = self.base / "clients" / profile
        credentials = directory / "platform-credentials.json"
        reusable = False
        with httpx.Client(base_url=self.platform_url, timeout=10, trust_env=False) as http:
            if credentials.exists():
                key = json.loads(credentials.read_text())["api_key"]
                reusable = (
                    http.get("/v1/account", headers={"Authorization": "Bearer " + key}).status_code
                    == 200
                )
            if not reusable:
                passwords = self.base / "platform/development-accounts.json"
                values = json.loads(passwords.read_text()) if passwords.exists() else {}
                if profile not in values:
                    values[profile] = secrets.token_urlsafe(24)
                    atomic_json(passwords, values)
                body = {"username": profile, "password": values[profile]}
                issued = http.post("/v1/accounts/register", json=body)
                if issued.status_code == 409:
                    issued = http.post("/v1/accounts/login", json=body)
                issued.raise_for_status()
                key = issued.json()["api_key"]
            atomic_json(credentials, {"platform_url": self.platform_url, "api_key": key})
        config = self.client_config(profile, port)
        path = directory / "client.toml"
        write_configuration(path, config)
        self._spawn(
            "supervisor", path, label="supervisor" if profile == "alice" else "supervisor-bob"
        )
        self.wait(f"http://127.0.0.1:{port}")

    def start(self) -> None:
        ports = [self.client_port, self.platform_port, self.pool_port]
        if self.second_client_port:
            ports.append(self.second_client_port)
        if len(set(ports)) != len(ports):
            raise RuntimeError("All client, platform and model-pool ports must be distinct")
        for port in ports:
            try:
                with socket.socket() as probe:
                    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    probe.bind(("127.0.0.1", port))
            except OSError as error:
                raise RuntimeError(
                    f"Port {port} is already in use; choose another explicit port"
                ) from error
        self.base.mkdir(parents=True, exist_ok=True)
        self.base.chmod(0o700)
        self.lock = OwnerLock(self.base / "dev.lock")
        try:
            pool_path = self.base / "platform/pool.toml"
            if pool_path.exists():
                import tomllib

                self.pool_key = tomllib.loads(pool_path.read_text())["key"]
            else:
                self.pool_key = secrets.token_urlsafe(32)
            pool = self.pool_config()
            write_configuration(pool_path, pool)
            self._spawn("model-pool", pool_path, self.pool_port)
            self.wait(self.pool_url)
            platform_path = self.base / "platform/platform.toml"
            write_configuration(platform_path, self.platform_config())
            self._spawn("platform", platform_path, self.platform_port)
            self.wait(self.platform_url)
            self._provision_client("alice", self.client_port)
            if self.second_client_port:
                self._provision_client("bob", self.second_client_port)
        except BaseException:
            self.close()
            raise

    def wait(self, url: str, timeout: float = 25) -> None:
        end = time.monotonic() + timeout
        with httpx.Client(timeout=0.5, trust_env=False) as client:
            while time.monotonic() < end:
                if any(process.poll() is not None for process in self.children):
                    raise RuntimeError(f"A service exited; inspect {self.base}/*.log")
                try:
                    if client.get(url + "/health/ready").status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        raise RuntimeError(f"Readiness timeout at {url}; inspect {self.base}")

    def close(self) -> None:
        for record in reversed(self.records):
            stop_record(record)
        for process in self.children:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                pass
        self.children.clear()
        self.records.clear()
        if self.base.exists():
            self._save()
        if self.lock is not None:
            self.lock.close()
            self.lock = None


def stop_development(base: Path) -> None:
    manifest = base / "dev.json"
    if not manifest.exists():
        return
    data = json.loads(manifest.read_text())
    for record in reversed(data["children"]):
        stop_record(record)
    data["children"] = []
    atomic_json(manifest, data)


def develop(
    base: Path,
    client_port: int | None = None,
    platform_port: int | None = None,
    pool_port: int | None = None,
    second_client_port: int | None = None,
    with_second_client: bool = True,
    *,
    scenario: str = "unconstrained",
    tool_slots: int | None = None,
    pool_capacity: int | None = None,
) -> None:
    topology = Topology(
        base,
        client_port=client_port,
        platform_port=platform_port,
        pool_port=pool_port,
        second_client_port=second_client_port,
        with_second_client=with_second_client,
        scenario=scenario,
        tool_slots=tool_slots,
        pool_capacity=pool_capacity,
    )
    stopping = False

    def stop(_signal: int, _frame: Any) -> None:
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    topology.start()
    print(
        json.dumps(
            {
                "client": topology.client_url,
                "second_client": topology.second_client_url,
                "platform": topology.platform_url,
                "state_dir": str(base),
                "model_pool": "platform-owned fixed-capacity simulation",
                "scenario": topology.scenario,
                "tool_slots": topology.tool_slots,
                "pool_capacity": topology.pool_capacity,
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        while not stopping and all(process.poll() is None for process in topology.children):
            time.sleep(0.3)
    finally:
        topology.close()


def self_test(base: Path) -> dict[str, Any]:
    topology = Topology(base, ephemeral=True, with_second_client=True)
    topology.start()
    try:
        assert topology.second_client_url
        with httpx.Client(timeout=10, trust_env=False) as http:
            tasks = []
            for url in [topology.client_url, topology.second_client_url]:
                response = http.post(
                    url + "/api/v1/tasks",
                    headers={"X-TaskLoom-Local": "1"},
                    json={"documents": {"a.txt": "alpha beta", "b.txt": "gamma delta epsilon"}},
                )
                response.raise_for_status()
                tasks.append((url, response.json()["task_id"]))
            deadline = time.monotonic() + 20
            for url, task_id in tasks:
                while time.monotonic() < deadline:
                    response = http.get(url + f"/api/v1/tasks/{task_id}")
                    response.raise_for_status()
                    task = response.json()
                    if task["outcome"] != "PENDING":
                        break
                    time.sleep(0.05)
                assert task["outcome"] == "SUCCEEDED", task
                assert task["result"]["files"] == 2 and task["result"]["words"] == 5
                assert task["quiescent"] and task["reserved_units"] == 0
                artifact = http.get(url + f"/api/v1/artifacts/{task_id}")
                artifact.raise_for_status()
                assert artifact.json()["words"] == 5
                graph = http.get(url + f"/api/v1/tasks/{task_id}/graph").json()
                assert len(graph["agents"]) == 1 and len(graph["steps"]) == 3
                assert len(graph["attempts"]) == 3
                assert sum(bool(s["predecessors"]) for s in graph["steps"]) == 2
                feedback = http.get(url + "/api/v1/platform").json()["feedback"]
                assert feedback["account_id"] == task["tenant_id"]
            status = http.get(
                topology.pool_url + "/api/v1/status",
                headers={"Authorization": "Bearer " + topology.pool_key},
            ).json()
            assert 1 <= status["peak"] <= status["capacity"] == 2
            result = {
                "self_test": "passed",
                "clients": 2,
                "files": 2,
                "words": 5,
                "pool_peak": status["peak"],
                "pool_capacity": status["capacity"],
                "consumed_mock_units": task["consumed_units"],
                "state_dir": str(base),
            }
    finally:
        topology.close()
    return result
