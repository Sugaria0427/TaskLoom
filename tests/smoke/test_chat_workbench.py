import base64
import io
import time

import httpx
from PIL import Image

from taskloom.bootstrap.development import Topology

HEADERS = {"X-TaskLoom-Local": "1"}


def test_chat_and_multimodal_upload_use_platform_and_preserve_plain_message(tmp_path):
    topology = Topology(tmp_path / "chat", ephemeral=True)
    topology.start()
    try:
        with httpx.Client(base_url=topology.client_url, timeout=10, trust_env=False) as client:
            settings = client.get("/api/v1/agent").json()
            assert {skill["command"] for skill in settings["skills"]} == {"/notes", "/stats"}
            assert "本地智能体" in settings["system_prompt"]
            buffer = io.BytesIO()
            Image.new("RGB", (16, 16), "red").save(buffer, format="PNG")
            payloads = [
                {"mode": "chat", "message": "你好"},
                {
                    "mode": "chat",
                    "message": "统计附件",
                    "skill": "stats",
                    "documents": {"a.txt": "one two"},
                },
                {
                    "mode": "chat",
                    "images": [
                        {
                            "name": "a.png",
                            "mime_type": "image/png",
                            "data_base64": base64.b64encode(buffer.getvalue()).decode(),
                        }
                    ],
                },
            ]
            for payload in payloads:
                response = client.post("/api/v1/tasks", headers=HEADERS, json=payload)
                response.raise_for_status()
                task = response.json()
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    task = client.get(f"/api/v1/tasks/{task['task_id']}").json()
                    if task["outcome"] != "PENDING":
                        break
                    time.sleep(0.05)
                assert task["outcome"] == "SUCCEEDED", task
                assert task["result"]["content"]
                if payload.get("skill") == "stats":
                    assert task["result"]["words"] == 2
                else:
                    assert not task["specification"]["documents"]
            assert client.get("/api/v1/resources").json()["allocations"] == []
            assert not list((tmp_path / "chat").rglob("notes.txt"))
            assert not list((tmp_path / "chat").rglob("summary.json"))
            denied = client.post(
                "/api/v1/tasks/missing/approvals/none", json={"allowed": True, "generation": 0}
            )
            assert denied.status_code == 403
            page = client.get("/").text
            assert 'id="skill-menu"' in page and "image/png" in page
    finally:
        topology.close()
