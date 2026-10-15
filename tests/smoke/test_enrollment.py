import json
import os
import signal
import time
from pathlib import Path

import httpx

from taskloom.adapters.supervisor import write_configuration
from taskloom.bootstrap.development import Topology, free_port
from taskloom.contracts.models import ClientConfig

HEADERS = {"X-TaskLoom-Local": "1"}


def wait_task(http, task_id):
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        body = http.get(f"/api/v1/tasks/{task_id}").json()
        if body["outcome"] != "PENDING":
            assert body["outcome"] == "SUCCEEDED", body
            return body
        time.sleep(0.05)
    raise AssertionError("The enrolled account task did not finish")


def test_local_registration_credentials_and_supervised_restart(tmp_path: Path) -> None:
    topology = Topology(tmp_path / "enrollment", ephemeral=True)
    topology.start()
    try:
        port = free_port()
        path = topology.base / "new-client.toml"
        write_configuration(
            path,
            ClientConfig(profile="charlie", client_port=port, platform_url=topology.platform_url),
        )
        topology._spawn("supervisor", path, label="supervisor-charlie")
        url = f"http://127.0.0.1:{port}"
        topology.wait(url)
        with httpx.Client(base_url=url, timeout=10, trust_env=False) as http:
            assert not http.get("/api/v1/platform").json()["connected"]
            response = http.post(
                "/api/v1/platform/register",
                headers=HEADERS,
                json={
                    "platform_url": topology.platform_url,
                    "username": "charlie",
                    "password": "enrollment-password-123",
                },
            )
            response.raise_for_status()
            assert response.json()["connected"]
            assert response.json()["feedback"]["account_id"] == "charlie"
            assert "api_key" not in response.text
            credential = topology.base / "clients/charlie/platform-credentials.json"
            assert credential.stat().st_mode & 0o777 == 0o600
            assert "api_key" in json.loads(credential.read_text())
            first = http.post(
                "/api/v1/tasks", headers=HEADERS, json={"documents": {"a.txt": "one two three"}}
            )
            first.raise_for_status()
            result = wait_task(http, first.json()["task_id"])
            assert result["tenant_id"] == "charlie" and result["result"]["words"] == 3
            manifest = topology.base / "clients/charlie/supervisor.json"
            previous = next(
                row
                for row in json.loads(manifest.read_text())["children"]
                if row["role"] == "client"
            )
            os.kill(previous["pid"], signal.SIGKILL)
            for _ in range(200):
                current = next(
                    row
                    for row in json.loads(manifest.read_text())["children"]
                    if row["role"] == "client"
                )
                if current["pid"] != previous["pid"]:
                    break
                time.sleep(0.05)
            else:
                raise AssertionError("The client was not supervised back online")
            topology.wait(url)
            platform = http.get("/api/v1/platform").json()
            assert platform["connected"] and platform["feedback"]["account_id"] == "charlie"
            resumed = http.post(
                "/api/v1/tasks", headers=HEADERS, json={"documents": {"b.txt": "four five"}}
            )
            resumed.raise_for_status()
            assert wait_task(http, resumed.json()["task_id"])["result"]["words"] == 2
    finally:
        topology.close()
