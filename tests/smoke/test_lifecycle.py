import json
import os
import signal
import time
from pathlib import Path

import httpx

from taskloom.bootstrap.development import Topology

HEADERS = {"X-TaskLoom-Local": "1"}


def wait_for(client, path: str, predicate, seconds: float = 20):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        response = client.get(path)
        response.raise_for_status()
        body = response.json()
        if predicate(body):
            return body
        time.sleep(0.04)
    raise AssertionError(f"Condition not reached: {path}, {body}")


def wait_tool(client, task_id):
    return wait_for(
        client,
        "/api/v1/resources",
        lambda body: any(
            row["root_task_id"] == task_id
            and row["kind"] == "tool"
            and row["state"] == "RUNNING"
            and row["execution_ref"]
            for row in body["attempts"]
        ),
    )


def test_running_cancel_resume_and_input_version_guard(tmp_path: Path) -> None:
    topology = Topology(tmp_path / "lifecycle", ephemeral=True)
    topology.start()
    try:
        with httpx.Client(base_url=topology.client_url, timeout=10, trust_env=False) as client:
            response = client.post(
                "/api/v1/tasks",
                headers=HEADERS,
                json={"documents": {"a.txt": "one two three"}, "work_iterations": 5000000},
            )
            response.raise_for_status()
            task_id = response.json()["task_id"]
            wait_tool(client, task_id)
            cancelled = client.post(f"/api/v1/tasks/{task_id}/cancel", headers=HEADERS)
            cancelled.raise_for_status()
            assert cancelled.json()["outcome"] == "CANCELLED"
            assert cancelled.json()["quiescent"]
            wait_for(client, "/api/v1/resources", lambda body: body["available"]["slots"] == 2)
            input_file = topology.base / "clients/alice/tasks" / task_id / "inputs/a.txt"
            input_file.write_text("changed")
            blocked = client.post(f"/api/v1/tasks/{task_id}/resume", headers=HEADERS)
            assert blocked.status_code == 400
            assert blocked.json()["error"]["code"] == "INPUT_VERSION_MISMATCH"
            input_file.write_text("one two three")
            resumed = client.post(f"/api/v1/tasks/{task_id}/resume", headers=HEADERS)
            resumed.raise_for_status()
            done = wait_for(
                client, f"/api/v1/tasks/{task_id}", lambda body: body["outcome"] == "SUCCEEDED"
            )
            assert done["result"]["words"] == 3
            assert done["reserved_units"] == 0
            assert done["quiescent"]
    finally:
        topology.close()


def test_supervised_client_recovers_without_reexecuting_tool(tmp_path: Path) -> None:
    topology = Topology(tmp_path / "restart", ephemeral=True)
    topology.start()
    try:
        with httpx.Client(base_url=topology.client_url, timeout=10, trust_env=False) as client:
            response = client.post(
                "/api/v1/tasks",
                headers=HEADERS,
                json={"documents": {"a.txt": "one two three"}, "work_iterations": 5000000},
            )
            response.raise_for_status()
            task_id = response.json()["task_id"]
            wait_tool(client, task_id)
            manifest = topology.base / "clients/alice/supervisor.json"
            original = next(
                row
                for row in json.loads(manifest.read_text())["children"]
                if row["role"] == "client"
            )
            os.kill(original["pid"], signal.SIGKILL)
            end = time.monotonic() + 20
            while time.monotonic() < end:
                data = json.loads(manifest.read_text())
                replacement = next(row for row in data["children"] if row["role"] == "client")
                if replacement["pid"] != original["pid"]:
                    break
                time.sleep(0.05)
            assert replacement["pid"] != original["pid"]
            topology.wait(topology.client_url)
            done = wait_for(
                client, f"/api/v1/tasks/{task_id}", lambda body: body["outcome"] == "SUCCEEDED"
            )
            assert done["result"]["words"] == 3
            resources = client.get("/api/v1/resources").json()
            tools = [
                row
                for row in resources["attempts"]
                if row["root_task_id"] == task_id and row["kind"] == "tool"
            ]
            assert len(tools) == 1
            assert done["quiescent"] and done["reserved_units"] == 0
    finally:
        topology.close()
