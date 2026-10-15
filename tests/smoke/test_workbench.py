import json
import time

import httpx

from taskloom.bootstrap.development import Topology

HEADERS = {"X-TaskLoom-Local": "1"}


def test_persistent_conversation_with_real_workflows_and_account_isolation(tmp_path) -> None:
    topology = Topology(tmp_path / "workbench", ephemeral=True, with_second_client=True)
    topology.start()
    try:
        with httpx.Client(base_url=topology.client_url, timeout=10, trust_env=False) as client:
            denied = client.post("/api/v1/conversations", json={"title": "blocked"})
            assert denied.status_code == 403
            created = client.post(
                "/api/v1/conversations", headers=HEADERS, json={"title": "学习资料"}
            )
            created.raise_for_status()
            conversation_id = created.json()["conversation_id"]
            ids = []
            for content in ("one\r\ntwo", "three\r\nfour five"):
                response = client.post(
                    f"/api/v1/conversations/{conversation_id}/messages",
                    headers=HEADERS,
                    json={"instruction": "请统计资料", "documents": {"notes.txt": content}},
                )
                response.raise_for_status()
                ids.append(response.json()["task_id"])
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                detail = client.get(f"/api/v1/conversations/{conversation_id}").json()
                if all(task["outcome"] == "SUCCEEDED" for task in detail["tasks"]):
                    break
                time.sleep(0.05)
            else:
                raise AssertionError(detail)
            assert [t["result"]["words"] for t in detail["tasks"]] == [2, 3]
            assert [t["task_id"] for t in detail["tasks"]] == ids
            assert all(t["specification"]["instruction"] == "请统计资料" for t in detail["tasks"])
            resources = client.get("/api/v1/resources").json()
            assert resources["artifact_capacity_bytes"] > resources["artifact_bytes"] > 0
            assert resources["model_capacity"]["slots"] == resources["model_available"]["slots"]
            assert resources["allocations"] == []
            feedback = client.get("/api/v1/platform").json()["feedback"]
            assert feedback["pool_kind"] == "simulated"
            assert feedback["usage_unit"] == "mock_microcredit"
            for archived in (True, False):
                update = client.post(
                    f"/api/v1/conversations/{conversation_id}/update",
                    headers=HEADERS,
                    json={"title": "已验证的统计会话", "archived": archived},
                )
                assert update.json()["archived"] == archived
                if archived:
                    rejected = client.post(
                        f"/api/v1/conversations/{conversation_id}/messages",
                        headers=HEADERS,
                        json={},
                    )
                    assert rejected.status_code == 400
                    assert rejected.json()["error"]["code"] == "CONVERSATION_ARCHIVED"
            assert len(client.get("/api/v1/tasks").json()) == 2
            response = client.get("/api/v1/conversations").json()
            assert len(response) == 1 and response[0]["task_count"] == 2
            assert response[0]["active_count"] == 0
            assert "api_key" not in json.dumps(response)
            assert client.get("/static/mark.svg").status_code == 200
        with httpx.Client(base_url=topology.second_client_url, timeout=10, trust_env=False) as bob:
            assert bob.get("/api/v1/conversations").json() == []
            assert bob.get(f"/api/v1/conversations/{conversation_id}").status_code == 404
    finally:
        topology.close()
